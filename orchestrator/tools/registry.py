"""Tool registry for the orchestrator's coder model.

This module unifies two API surfaces that landed in parallel:

* The richer OpenAI-style registry from issue #30 (this PR): it validates
  arguments against a JSON Schema before invoking the tool callable, times
  every dispatch, and emits the ``tools=[...]`` payload expected by
  ``openai.chat.completions.create``.
* The minimal in-process registry that ``orchestrator.tools.mcp_client``
  depends on (landed via PR #75 on ``main``): it adds a ``source`` field on
  ``Tool`` (``builtin`` vs ``mcp``), thread-safe access, and helpers like
  ``by_source`` / ``clear`` / ``__contains__`` / ``__len__`` plus a
  ``register(..., replace=True)`` keyword for refreshing remote tools.

Both surfaces are kept here as a union so the MCP client and the coder
model can share a single :data:`default_registry`. ``ToolResult`` exposes
``data`` and ``content`` as aliases for the same payload so callers using
either name keep working.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "Registry",
    "Tool",
    "ToolResult",
    "default_registry",
]


class Tool(BaseModel):
    """A single registered tool callable."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str = Field(min_length=1)
    description: str = ""
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    fn: Callable[..., Any] | Callable[..., Awaitable[Any]] | None = None
    permissions: dict[str, bool] = Field(default_factory=dict)
    source: str = "builtin"


class ToolResult(BaseModel):
    """Uniform envelope for every dispatch outcome.

    ``data`` and ``content`` are aliases for the same payload: callers from
    the coder dispatch path use ``data`` while the MCP client uses
    ``content``. Both attributes are populated and stay in sync.
    """

    model_config = ConfigDict(populate_by_name=True)

    ok: bool
    data: Any | None = None
    content: Any | None = None
    error: str | None = None
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _sync_data_content(self) -> ToolResult:
        # Mirror data <-> content so either accessor returns the payload.
        if self.data is None and self.content is not None:
            object.__setattr__(self, "data", self.content)
        elif self.content is None and self.data is not None:
            object.__setattr__(self, "content", self.data)
        return self


class Registry:
    """Holds tools by unique name and dispatches calls into them.

    Thread-safe. Supports both the duplicate-rejecting ``register(tool)``
    style from the coder dispatch path and ``register(tool, replace=True)``
    from the MCP client (which refreshes remote tools on reconnect).
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        """Register *tool*. Rejects duplicate names unless ``replace=True``.

        Also validates ``parameters_schema`` against JSON Schema 2020-12
        (skipped when the schema is empty, e.g. for tools registered by
        ``orchestrator.tools.mcp_client`` before the remote schema arrives).
        """
        with self._lock:
            if tool.name in self._tools and not replace:
                raise ValueError(f"tool already registered: {tool.name!r}")
            if tool.parameters_schema:
                try:
                    Draft202012Validator.check_schema(tool.parameters_schema)
                except SchemaError as exc:
                    raise ValueError(
                        f"invalid parameters_schema for tool {tool.name!r}: {exc.message}"
                    ) from exc
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove *name* if present (no-op otherwise)."""
        with self._lock:
            self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Return the tool registered under *name* or ``None``."""
        with self._lock:
            return self._tools.get(name)

    def names(self) -> list[str]:
        """Return registered tool names in stable (sorted) order."""
        with self._lock:
            return sorted(self._tools)

    def by_source(self, source: str) -> list[Tool]:
        """Return tools whose ``source`` matches *source*."""
        with self._lock:
            return [t for t in self._tools.values() if t.source == source]

    def clear(self) -> None:
        """Remove every registered tool."""
        with self._lock:
            self._tools.clear()

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._tools

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Validate *args* and invoke the tool *name*.

        Always returns a :class:`ToolResult`; exceptions raised by the tool
        callable are captured into ``error`` with ``ok=False``. Async
        callables (used by the MCP client) are not awaited here -- callers
        that register async tools should use the async dispatcher provided
        by :mod:`orchestrator.tools.mcp_client` instead.
        """
        start = perf_counter()
        with self._lock:
            tool = self._tools.get(name)

        def _ms() -> int:
            return int((perf_counter() - start) * 1000)

        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool: {name!r}", duration_ms=_ms())

        if tool.parameters_schema:
            validator = Draft202012Validator(tool.parameters_schema)
            try:
                validator.validate(args)
            except ValidationError as exc:
                return ToolResult(
                    ok=False,
                    error=f"ValidationError: {exc.message}",
                    duration_ms=_ms(),
                )

        if tool.fn is None:
            return ToolResult(
                ok=False, error=f"tool {name!r} has no callable bound", duration_ms=_ms()
            )

        try:
            result = tool.fn(**args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", duration_ms=_ms())

        if inspect.isawaitable(result):
            return ToolResult(
                ok=False,
                error=(
                    f"tool {name!r} is async; use the async dispatcher "
                    "(orchestrator.tools.mcp_client)"
                ),
                duration_ms=_ms(),
            )

        return ToolResult(ok=True, data=result, duration_ms=_ms())

    def openai_tools_spec(self) -> list[dict[str, Any]]:
        """Return ``tools=[...]`` payload for ``chat.completions.create``."""
        with self._lock:
            items = sorted(self._tools.items())
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema,
                },
            }
            for _, tool in items
        ]


default_registry = Registry()
