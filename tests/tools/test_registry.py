"""Tests for the minimal in-process tool registry."""

from __future__ import annotations

import pytest

from orchestrator.tools.registry import Registry, Tool, ToolResult, default_registry


def _tool(name: str, source: str = "builtin") -> Tool:
    return Tool(name=name, description="", parameters_schema={}, source=source)


def test_register_and_get_roundtrip() -> None:
    reg = Registry()
    t = _tool("a")
    reg.register(t)
    assert "a" in reg
    assert reg.get("a") is t
    assert reg.names() == ["a"]
    assert len(reg) == 1


def test_register_duplicate_raises_unless_replace() -> None:
    reg = Registry()
    reg.register(_tool("a"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_tool("a"))
    new = _tool("a")
    reg.register(new, replace=True)
    assert reg.get("a") is new


def test_unregister_is_idempotent() -> None:
    reg = Registry()
    reg.register(_tool("a"))
    reg.unregister("a")
    reg.unregister("a")
    assert "a" not in reg


def test_by_source_and_clear() -> None:
    reg = Registry()
    reg.register(_tool("a", source="builtin"))
    reg.register(_tool("b", source="mcp"))
    reg.register(_tool("c", source="mcp"))
    assert sorted(t.name for t in reg.by_source("mcp")) == ["b", "c"]
    reg.clear()
    assert len(reg) == 0


def test_tool_result_envelope_defaults() -> None:
    ok = ToolResult(ok=True, content={"x": 1})
    assert ok.error is None
    err = ToolResult(ok=False, error="bad")
    assert err.content is None


def test_default_registry_is_module_level_singleton() -> None:
    assert isinstance(default_registry, Registry)
