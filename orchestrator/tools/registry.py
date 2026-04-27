"""Minimal in-process tool registry.

Provides the ``Tool`` / ``ToolResult`` data types and a thread-safe
``Registry`` that maps tool names to async callables. The MCP client
(:mod:`orchestrator.tools.mcp_client`) uses this registry to surface
remote MCP tools alongside built-in tools.

This is a lightweight stand-in for the richer registry tracked in
issue #30; the public surface (``register`` / ``unregister`` / ``get``
/ ``names``) is kept small and stable so the larger registry can drop
in without breaking callers.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Registry",
    "Tool",
    "ToolResult",
    "default_registry",
]


@dataclass(slots=True)
class ToolResult:
    """Standard envelope returned by every tool dispatch."""

    ok: bool
    content: Any = None
    error: str | None = None


@dataclass(slots=True)
class Tool:
    """A registered tool callable by the orchestrator."""

    name: str
    description: str
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    fn: Callable[..., Awaitable[ToolResult]] | None = None
    source: str = "builtin"


class Registry:
    """In-process map of tool name → :class:`Tool`."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        """Add ``tool`` to the registry.

        Raises:
            ValueError: When a tool with the same name already exists and
                ``replace`` is ``False``.
        """
        with self._lock:
            if tool.name in self._tools and not replace:
                raise ValueError(f"tool {tool.name!r} already registered")
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        with self._lock:
            return self._tools[name]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._tools)

    def by_source(self, source: str) -> list[Tool]:
        with self._lock:
            return [t for t in self._tools.values() if t.source == source]

    def clear(self) -> None:
        with self._lock:
            self._tools.clear()

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._tools

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)


default_registry = Registry()
