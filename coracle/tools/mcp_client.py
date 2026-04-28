"""Config-driven MCP client.

Reads a YAML config of MCP servers, opens a :class:`mcp.ClientSession`
for each enabled entry, and registers the remote tools as local
:class:`~coracle.tools.registry.Tool` entries.

The default session factory is a thin wrapper over the official ``mcp``
SDK; tests inject a fake factory so no real subprocess or HTTP traffic
is ever generated.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .registry import Registry, Tool, ToolResult, default_registry

__all__ = [
    "MCPClientError",
    "MCPManager",
    "ServerSpec",
    "ServerStatus",
    "SessionFactory",
    "default_session_factory",
    "expand_env",
    "load_config",
]

log = structlog.get_logger("coracle.tools.mcp_client")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
TOOL_SOURCE = "mcp"


class MCPClientError(RuntimeError):
    """Raised for invalid MCP client configuration."""


class ServerSpec(BaseModel):
    """Configuration for a single MCP server entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    enabled: bool = True
    transport: Literal["stdio", "http", "sse"]
    command: tuple[str, ...] | None = None
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=30.0, gt=0)
    tool_prefix: str = ""

    @model_validator(mode="after")
    def _check_transport_args(self) -> ServerSpec:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio transport requires a non-empty 'command'")
            if self.url is not None:
                raise ValueError("stdio transport must not set 'url'")
        else:
            if not self.url:
                raise ValueError(f"{self.transport} transport requires 'url'")
            if self.command is not None:
                raise ValueError(f"{self.transport} transport must not set 'command'")
        return self


def expand_env(value: Any, environ: dict[str, str] | None = None) -> Any:
    """Recursively substitute ``${VAR}`` tokens using ``environ``.

    Strings, lists, tuples and dicts are walked. Missing variables expand
    to the empty string (matching common shell semantics) so a partially
    configured environment still produces a structurally valid config.
    """
    env = environ if environ is not None else os.environ

    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: env.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    if isinstance(value, tuple):
        return tuple(expand_env(v, env) for v in value)
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    return value


def load_config(path: str | Path, environ: dict[str, str] | None = None) -> list[ServerSpec]:
    """Parse ``path`` and return the validated list of :class:`ServerSpec`.

    Raises:
        MCPClientError: File missing, invalid YAML, or schema violation.
    """
    p = Path(path)
    if not p.exists():
        raise MCPClientError(f"MCP config not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise MCPClientError(f"Invalid YAML in {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise MCPClientError(f"Top-level YAML in {p} must be a mapping")
    servers_raw = raw.get("servers", [])
    if not isinstance(servers_raw, list):
        raise MCPClientError("'servers' must be a list")

    expanded = expand_env(servers_raw, environ)
    specs: list[ServerSpec] = []
    seen: set[str] = set()
    for entry in expanded:
        if not isinstance(entry, dict):
            raise MCPClientError("each server entry must be a mapping")
        try:
            spec = ServerSpec.model_validate(entry)
        except ValidationError as exc:
            raise MCPClientError(f"invalid server spec: {exc}") from exc
        if spec.name in seen:
            raise MCPClientError(f"duplicate server name: {spec.name}")
        seen.add(spec.name)
        specs.append(spec)
    return specs


class SessionLike(Protocol):
    """Minimal subset of ``mcp.ClientSession`` we depend on."""

    async def list_tools(self) -> Any: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


SessionFactory = Callable[
    [ServerSpec, AsyncExitStack],
    Awaitable[SessionLike],
]


async def default_session_factory(
    spec: ServerSpec,
    stack: AsyncExitStack,
) -> SessionLike:  # pragma: no cover - exercised only against real MCP servers
    """Build a real :class:`mcp.ClientSession` for ``spec``.

    This is intentionally excluded from coverage: tests inject a fake
    session factory so no subprocesses or network connections are made.
    """
    from mcp import ClientSession

    if spec.transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not spec.command:
            raise MCPClientError(f"stdio server {spec.name!r} has no command")
        params = StdioServerParameters(
            command=spec.command[0],
            args=list(spec.command[1:]),
            env=dict(spec.env) or None,
        )
        read, write = await stack.enter_async_context(stdio_client(params))
    elif spec.transport == "sse":
        from mcp.client.sse import sse_client

        read, write = await stack.enter_async_context(
            sse_client(spec.url, headers=dict(spec.headers) or None)
        )
    else:  # http (streamable HTTP)
        from mcp.client.streamable_http import streamablehttp_client

        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(spec.url, headers=dict(spec.headers) or None)
        )

    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


@dataclass(slots=True)
class ServerStatus:
    """Runtime view of a connected (or skipped) MCP server."""

    name: str
    transport: str
    connected: bool
    tool_count: int
    tool_prefix: str
    error: str | None = None
    tools: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Connection:
    spec: ServerSpec
    session: SessionLike
    stack: AsyncExitStack
    tool_names: list[str]


def _content_to_text(content: Any) -> str:
    """Best-effort extraction of plain text from MCP content blocks."""
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
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


class MCPManager:
    """Owns the lifecycle of all configured MCP server sessions."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        registry: Registry | None = None,
        session_factory: SessionFactory | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._path = Path(config_path)
        self._registry = registry if registry is not None else default_registry
        self._session_factory = session_factory or default_session_factory
        self._environ = environ
        self._connections: dict[str, _Connection] = {}
        self._statuses: dict[str, ServerStatus] = {}
        self._lock = asyncio.Lock()

    @property
    def config_path(self) -> Path:
        return self._path

    async def start(self) -> None:
        """Load config and connect every enabled server."""
        async with self._lock:
            specs = load_config(self._path, self._environ)
            self._statuses = {}
            for spec in specs:
                if not spec.enabled:
                    self._statuses[spec.name] = ServerStatus(
                        name=spec.name,
                        transport=spec.transport,
                        connected=False,
                        tool_count=0,
                        tool_prefix=spec.tool_prefix,
                        error="disabled",
                    )
                    continue
                await self._connect_locked(spec)

    async def reload(self) -> None:
        """Re-read the config; close removed servers, open new ones."""
        async with self._lock:
            new_specs = load_config(self._path, self._environ)
            new_by_name = {s.name: s for s in new_specs}

            for name in list(self._connections):
                spec = new_by_name.get(name)
                if spec is None or not spec.enabled or spec != self._connections[name].spec:
                    await self._disconnect_locked(name)

            new_statuses: dict[str, ServerStatus] = {}
            for spec in new_specs:
                if not spec.enabled:
                    new_statuses[spec.name] = ServerStatus(
                        name=spec.name,
                        transport=spec.transport,
                        connected=False,
                        tool_count=0,
                        tool_prefix=spec.tool_prefix,
                        error="disabled",
                    )
                    continue
                if spec.name in self._connections:
                    new_statuses[spec.name] = self._statuses[spec.name]
                    continue
                await self._connect_locked(spec)
                new_statuses[spec.name] = self._statuses[spec.name]
            self._statuses = new_statuses

    async def aclose(self) -> None:
        """Tear down every active connection."""
        async with self._lock:
            for name in list(self._connections):
                await self._disconnect_locked(name)

    def list_status(self) -> list[ServerStatus]:
        """Snapshot of every configured server."""
        return list(self._statuses.values())

    async def _connect_locked(self, spec: ServerSpec) -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            session = await self._session_factory(spec, stack)
            tools_resp = await session.list_tools()
            remote_tools = list(getattr(tools_resp, "tools", tools_resp) or [])
        except Exception as exc:
            await stack.aclose()
            log.warning(
                "mcp.server.unreachable",
                server=spec.name,
                transport=spec.transport,
                error=str(exc),
            )
            self._statuses[spec.name] = ServerStatus(
                name=spec.name,
                transport=spec.transport,
                connected=False,
                tool_count=0,
                tool_prefix=spec.tool_prefix,
                error=str(exc),
            )
            return

        registered: list[str] = []
        for remote in remote_tools:
            remote_name = getattr(remote, "name", None) or remote["name"]
            description = getattr(remote, "description", None)
            if description is None and isinstance(remote, dict):
                description = remote.get("description")
            input_schema = getattr(remote, "inputSchema", None)
            if input_schema is None and isinstance(remote, dict):
                input_schema = remote.get("inputSchema")
            local_name = f"{spec.tool_prefix}{remote_name}"
            tool = Tool(
                name=local_name,
                description=description or "",
                parameters_schema=dict(input_schema or {}),
                fn=self._make_dispatcher(spec, session, remote_name),
                source=TOOL_SOURCE,
            )
            self._registry.register(tool, replace=True)
            registered.append(local_name)

        self._connections[spec.name] = _Connection(
            spec=spec, session=session, stack=stack, tool_names=registered
        )
        self._statuses[spec.name] = ServerStatus(
            name=spec.name,
            transport=spec.transport,
            connected=True,
            tool_count=len(registered),
            tool_prefix=spec.tool_prefix,
            tools=list(registered),
        )
        log.info(
            "mcp.server.connected",
            server=spec.name,
            transport=spec.transport,
            tools=len(registered),
        )

    async def _disconnect_locked(self, name: str) -> None:
        conn = self._connections.pop(name, None)
        if conn is None:
            return
        for tool_name in conn.tool_names:
            self._registry.unregister(tool_name)
        try:
            await conn.stack.aclose()
        except Exception as exc:
            log.warning("mcp.server.close_error", server=name, error=str(exc))
        log.info("mcp.server.disconnected", server=name)

    def _make_dispatcher(
        self,
        spec: ServerSpec,
        session: SessionLike,
        remote_name: str,
    ) -> Callable[..., Awaitable[ToolResult]]:
        async def _dispatch(**args: Any) -> ToolResult:
            t0 = time.monotonic()
            log.debug(
                "mcp.tool.args",
                server=spec.name,
                tool=remote_name,
                args=args,
            )
            try:
                result = await asyncio.wait_for(
                    session.call_tool(remote_name, args),
                    timeout=spec.timeout_s,
                )
            except TimeoutError:
                duration = time.monotonic() - t0
                log.info(
                    "mcp.tool.call",
                    server=spec.name,
                    tool=remote_name,
                    ok=False,
                    duration=duration,
                    error="timeout",
                )
                return ToolResult(
                    ok=False,
                    error=f"timeout after {spec.timeout_s}s",
                )
            except Exception as exc:
                duration = time.monotonic() - t0
                log.info(
                    "mcp.tool.call",
                    server=spec.name,
                    tool=remote_name,
                    ok=False,
                    duration=duration,
                    error=str(exc),
                )
                return ToolResult(ok=False, error=str(exc))

            duration = time.monotonic() - t0
            content = getattr(result, "content", None)
            is_error = bool(getattr(result, "isError", False))
            text = _content_to_text(content)
            log.info(
                "mcp.tool.call",
                server=spec.name,
                tool=remote_name,
                ok=not is_error,
                duration=duration,
            )
            if is_error:
                return ToolResult(ok=False, error=text or "tool reported error")
            return ToolResult(ok=True, content=text)

        return _dispatch
