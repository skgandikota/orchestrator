"""Tests for the tool registry."""

from __future__ import annotations

import pytest

from coracle.tools import default_registry as _default_registry  # noqa: F401
from coracle.tools.registry import Registry, Tool, ToolResult, default_registry


def _add(a: int, b: int) -> int:
    return a + b


def _add_schema() -> dict:
    return {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    }


def _make_tool(**overrides) -> Tool:
    base = {
        "name": "math.add",
        "description": "Return a + b.",
        "parameters_schema": _add_schema(),
        "fn": _add,
    }
    base.update(overrides)
    return Tool(**base)


# -- Registration ----------------------------------------------------------


def test_register_and_lookup() -> None:
    reg = Registry()
    tool = _make_tool()
    reg.register(tool)
    assert reg.get("math.add") is tool
    assert reg.names() == ["math.add"]


def test_register_rejects_duplicate_name() -> None:
    reg = Registry()
    reg.register(_make_tool())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_make_tool())


def test_register_rejects_invalid_schema() -> None:
    reg = Registry()
    bad = _make_tool(parameters_schema={"type": "not-a-type"})
    with pytest.raises(ValueError, match="invalid parameters_schema"):
        reg.register(bad)


def test_unregister_is_noop_for_unknown() -> None:
    reg = Registry()
    reg.unregister("nope")
    assert reg.names() == []


def test_get_missing_returns_none() -> None:
    assert Registry().get("missing") is None


def test_names_returns_stable_sorted_order() -> None:
    reg = Registry()
    reg.register(_make_tool(name="zeta"))
    reg.register(_make_tool(name="alpha"))
    reg.register(_make_tool(name="mu"))
    assert reg.names() == ["alpha", "mu", "zeta"]


def test_tool_is_frozen() -> None:
    from pydantic import ValidationError

    tool = _make_tool()
    with pytest.raises(ValidationError):
        tool.name = "other"  # type: ignore[misc]


# -- Dispatch --------------------------------------------------------------


def test_dispatch_happy_path() -> None:
    reg = Registry()
    reg.register(_make_tool())
    result = reg.dispatch("math.add", {"a": 2, "b": 3})
    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.data == 5
    assert result.error is None
    assert result.duration_ms >= 0


def test_dispatch_unknown_tool_returns_error_envelope() -> None:
    reg = Registry()
    result = reg.dispatch("nope", {})
    assert result.ok is False
    assert result.data is None
    assert "unknown tool" in (result.error or "")
    assert result.duration_ms >= 0


def test_dispatch_invalid_args_does_not_call_fn() -> None:
    calls: list[dict] = []

    def fn(**kwargs: object) -> object:
        calls.append(kwargs)
        return "ran"

    reg = Registry()
    reg.register(_make_tool(fn=fn))
    result = reg.dispatch("math.add", {"a": "not-an-int", "b": 3})
    assert result.ok is False
    assert result.error is not None and result.error.startswith("ValidationError")
    assert calls == []


def test_dispatch_missing_required_is_validation_error() -> None:
    reg = Registry()
    reg.register(_make_tool())
    result = reg.dispatch("math.add", {"a": 1})
    assert result.ok is False
    assert result.error is not None and result.error.startswith("ValidationError")


def test_dispatch_captures_fn_exception() -> None:
    def boom(**_: object) -> None:
        raise RuntimeError("kaboom")

    reg = Registry()
    reg.register(_make_tool(fn=boom))
    result = reg.dispatch("math.add", {"a": 1, "b": 2})
    assert result.ok is False
    assert result.error == "RuntimeError: kaboom"
    assert result.data is None
    assert result.duration_ms >= 0


def test_dispatch_timing_populated_even_on_failure() -> None:
    reg = Registry()
    result = reg.dispatch("missing", {})
    assert result.duration_ms >= 0


# -- OpenAI spec -----------------------------------------------------------


def test_openai_tools_spec_shape() -> None:
    reg = Registry()
    reg.register(_make_tool())
    spec = reg.openai_tools_spec()
    assert isinstance(spec, list)
    assert spec == [
        {
            "type": "function",
            "function": {
                "name": "math.add",
                "description": "Return a + b.",
                "parameters": _add_schema(),
            },
        }
    ]


def test_openai_tools_spec_sorted_by_name() -> None:
    reg = Registry()
    reg.register(_make_tool(name="zeta"))
    reg.register(_make_tool(name="alpha"))
    spec = reg.openai_tools_spec()
    assert [entry["function"]["name"] for entry in spec] == ["alpha", "zeta"]


# -- Default registry / phase-4 wiring ------------------------------------


def test_default_registry_is_singleton_module_export() -> None:
    from coracle.tools import registry as registry_mod

    assert default_registry is registry_mod.default_registry


def test_phase4_tools_registered_on_default_registry() -> None:
    import coracle.tools  # noqa: F401  -- ensure registrations ran
    from coracle.tools._registrations import register_default_tools

    # Other test modules (notably tests/tools/test_mcp_client.py) call
    # ``default_registry.clear()`` and rely on import-time side effects to
    # have happened. Re-run registrations to keep this test order-agnostic.
    register_default_tools()

    names = set(default_registry.names())
    expected = {
        "fs.read_file",
        "fs.write_file",
        "fs.list_dir",
        "fs.delete_file",
        "shell.run_command",
        "web.fetch",
        "web.search",
        "git.status",
        "git.diff",
        "git.commit",
        "git.branch",
        "git.checkout",
        "git.log",
        "git.current_branch",
        "browser.browse",
        "browser.extract",
        "browser.click",
        "browser.fill",
    }
    missing = expected - names
    assert not missing, f"missing default tools: {sorted(missing)}"


def test_default_registry_openai_spec_matches_openai_shape() -> None:
    from coracle.tools._registrations import register_default_tools

    register_default_tools()
    spec = default_registry.openai_tools_spec()
    assert spec, "default registry should have tools registered"
    for entry in spec:
        assert entry["type"] == "function"
        fn = entry["function"]
        assert set(fn) == {"name", "description", "parameters"}
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str)
        params = fn["parameters"]
        assert isinstance(params, dict)
        assert params.get("type") == "object"
        assert "properties" in params


def test_register_default_tools_idempotent() -> None:
    from coracle.tools._registrations import register_default_tools

    register_default_tools()
    before = set(default_registry.names())
    register_default_tools()
    register_default_tools()
    assert set(default_registry.names()) == before


def test_permissions_field_defaults_and_round_trips() -> None:
    plain = _make_tool()
    assert plain.permissions == {}
    perm = _make_tool(name="other.add", permissions={"network": True})
    assert perm.permissions == {"network": True}


def test_dispatch_uses_kwargs() -> None:
    captured: dict = {}

    def fn(**kwargs: object) -> dict:
        captured.update(kwargs)
        return kwargs

    reg = Registry()
    reg.register(_make_tool(fn=fn))
    reg.dispatch("math.add", {"a": 7, "b": 9})
    assert captured == {"a": 7, "b": 9}


# -- Browser wrappers (stubbed) -------------------------------------------


class _StubBrowser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def browse(self, url):
        self.calls.append(("browse", (url,), {}))
        return {"url": url, "title": "t", "text": "x", "html_truncated": "", "status": 200}

    def extract(self, url, selector):
        self.calls.append(("extract", (url, selector), {}))
        return ["a", "b"]

    def click(self, url, selector):
        self.calls.append(("click", (url, selector), {}))
        return {"url": url, "title": "", "text": "", "html_truncated": "", "status": 200}

    def fill(self, url, selector, value):
        self.calls.append(("fill", (url, selector, value), {}))
        return {"url": url, "title": "", "text": "", "html_truncated": "", "status": 200}


def _install_stub_browser(monkeypatch: pytest.MonkeyPatch) -> _StubBrowser:
    from coracle.tools import _registrations

    stub = _StubBrowser()
    monkeypatch.setattr(_registrations, "_browser_singleton", stub, raising=True)
    return stub


def test_browser_browse_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub_browser(monkeypatch)
    result = default_registry.dispatch("browser.browse", {"url": "https://example.com"})
    assert result.ok is True
    assert stub.calls[0][0] == "browse"


def test_browser_extract_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub_browser(monkeypatch)
    result = default_registry.dispatch(
        "browser.extract", {"url": "https://example.com", "selector": "h1"}
    )
    assert result.ok is True
    assert result.data == ["a", "b"]
    assert stub.calls[0][0] == "extract"


def test_browser_click_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub_browser(monkeypatch)
    result = default_registry.dispatch(
        "browser.click", {"url": "https://example.com", "selector": "button"}
    )
    assert result.ok is True
    assert stub.calls[0][0] == "click"


def test_browser_fill_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub_browser(monkeypatch)
    result = default_registry.dispatch(
        "browser.fill",
        {"url": "https://example.com", "selector": "#q", "value": "hi"},
    )
    assert result.ok is True
    assert stub.calls[0][0] == "fill"


def test_get_browser_lazy_instantiates(monkeypatch: pytest.MonkeyPatch) -> None:
    from coracle.tools import _registrations
    from coracle.tools import browser as browser_mod

    monkeypatch.setattr(_registrations, "_browser_singleton", None, raising=True)
    instances: list[object] = []

    class _Fake:
        def __init__(self) -> None:
            instances.append(self)

    monkeypatch.setattr(browser_mod, "BrowserTool", _Fake)
    inst = _registrations._get_browser()
    again = _registrations._get_browser()
    assert inst is again
    assert len(instances) == 1


# --- Tests merged from main (PR #75 minimal-registry surface) ---
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
