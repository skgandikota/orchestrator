"""Dynamic MCP attachment for the OpenAI-compatible chat surface.

LiteLLM-style clients can attach MCP servers per request via an
``mcp_servers=[{name, url, transport, headers?}, ...]`` field on the
chat-completions body. This module owns the heavy lifting required by
:mod:`coracle.api.openai_compat` so the route handler stays small.

Responsibilities
----------------
* Validate the per-request MCP server specs.
* Open transient :class:`~coracle.tools.mcp_client.SessionLike`
  sessions against each server (via an injectable session factory so
  tests never touch a real subprocess or network).
* List each server's tools, prefix them with the server label, and
  expose them as OpenAI ``function`` tool descriptors that the upstream
  LLM can be told about.
* Provide a single :meth:`AttachedTools.call` entry-point that routes a
  prefixed tool name back to the originating session.
* Capture per-server errors so an unreachable server only skips that
  entry instead of failing the whole request.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from coracle.tools.mcp_client import (
    ServerSpec,
    SessionFactory,
    SessionLike,
    default_session_factory,
)

__all__ = [
    "AttachedTools",
    "MCPServerRequest",
    "attach_mcp_tools",
    "default_session_factory",
    "set_session_factory",
]


_SESSION_FACTORY: SessionFactory = default_session_factory


def set_session_factory(factory: SessionFactory | None) -> None:
    """Inject a session factory (used by application bootstrap and tests)."""
    global _SESSION_FACTORY
    _SESSION_FACTORY = factory if factory is not None else default_session_factory


class MCPServerRequest(BaseModel):
    """LiteLLM-style per-request MCP server entry."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    transport: Literal["stdio", "http", "sse"] = "http"
    url: str | None = None
    command: tuple[str, ...] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    tool_prefix: str | None = None
    timeout_s: float = Field(default=30.0, gt=0)

    def to_server_spec(self) -> ServerSpec:
        """Project the request entry onto a :class:`ServerSpec`."""
        prefix = self.tool_prefix if self.tool_prefix is not None else f"{self.name}__"
        return ServerSpec(
            name=self.name,
            transport=self.transport,
            command=self.command,
            url=self.url,
            env=dict(self.env),
            headers=dict(self.headers),
            timeout_s=self.timeout_s,
            tool_prefix=prefix,
        )


@dataclass(slots=True)
class _Session:
    spec: ServerSpec
    session: SessionLike
    remote_by_local: dict[str, str] = field(default_factory=dict)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            parts.append(str(text) if text is not None else str(item))
        return "\n".join(parts)
    return str(content)


@dataclass(slots=True)
class AttachedTools:
    """Result of attaching one request's worth of MCP servers.

    ``tools`` is the OpenAI-style ``function`` tool list to merge into
    the upstream LLM request. ``call`` dispatches a tool by its prefixed
    local name back to the originating MCP session. ``errors`` records
    per-server failures so callers can surface them without aborting.
    """

    tools: list[dict[str, Any]] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    _sessions: dict[str, _Session] = field(default_factory=dict)
    _stack: AsyncExitStack | None = None

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.tools]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a previously-attached tool by its prefixed name."""
        for sess in self._sessions.values():
            remote = sess.remote_by_local.get(name)
            if remote is None:
                continue
            try:
                result = await sess.session.call_tool(remote, arguments)
            except Exception as exc:
                return {"ok": False, "error": str(exc), "server": sess.spec.name}
            content = getattr(result, "content", None)
            is_error = bool(getattr(result, "isError", False))
            text = _content_to_text(content)
            if is_error:
                return {
                    "ok": False,
                    "error": text or "tool reported error",
                    "server": sess.spec.name,
                }
            return {"ok": True, "content": text, "server": sess.spec.name}
        return {"ok": False, "error": f"unknown attached tool {name!r}"}

    async def aclose(self) -> None:
        """Tear down every open session opened by :func:`attach_mcp_tools`."""
        if self._stack is None:
            return
        try:
            await self._stack.aclose()
        finally:
            self._stack = None
            self._sessions.clear()


async def attach_mcp_tools(
    requests: list[MCPServerRequest],
    *,
    session_factory: SessionFactory | None = None,
) -> AttachedTools:
    """Open sessions for ``requests`` and gather their tool descriptors.

    A failure to connect or list tools for any individual server is
    captured in :attr:`AttachedTools.errors` and that server is skipped;
    the other servers still attach successfully. Callers must
    ``await attached.aclose()`` (typically inside ``try/finally``) to
    release the underlying transports.
    """
    factory = session_factory if session_factory is not None else _SESSION_FACTORY
    stack = AsyncExitStack()
    await stack.__aenter__()
    attached = AttachedTools(_stack=stack)

    for req in requests:
        spec = req.to_server_spec()
        try:
            session = await factory(spec, stack)
            tools_resp = await session.list_tools()
        except Exception as exc:
            attached.errors[spec.name] = str(exc)
            continue

        remote_tools_raw: Any = getattr(tools_resp, "tools", None)
        if remote_tools_raw is None and isinstance(tools_resp, dict):
            remote_tools_raw = tools_resp.get("tools", [])
        if remote_tools_raw is None:
            remote_tools_raw = tools_resp
        remote_tools = list(remote_tools_raw or [])
        sess = _Session(spec=spec, session=session)
        attached.servers.append(spec.name)

        for remote in remote_tools:
            remote_name = getattr(remote, "name", None)
            if remote_name is None and isinstance(remote, dict):
                remote_name = remote.get("name")
            if not remote_name:
                continue
            description = getattr(remote, "description", None)
            if description is None and isinstance(remote, dict):
                description = remote.get("description")
            input_schema = getattr(remote, "inputSchema", None)
            if input_schema is None and isinstance(remote, dict):
                input_schema = remote.get("inputSchema")
            local_name = f"{spec.tool_prefix}{remote_name}"
            sess.remote_by_local[local_name] = remote_name
            attached.tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": local_name,
                        "description": description or "",
                        "parameters": dict(input_schema or {}),
                    },
                    "server": spec.name,
                }
            )

        attached._sessions[spec.name] = sess

    return attached
