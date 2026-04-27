"""Tests for the config-driven MCP client (issue #45).

A tiny in-process fake MCP session is injected via ``session_factory``
so tests never spawn subprocesses or touch the network.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from orchestrator.tools.mcp_client import (
    MCPClientError,
    MCPManager,
    ServerSpec,
    expand_env,
    load_config,
)
from orchestrator.tools.registry import Registry

# --------------------------------------------------------------------------- #
# Fake MCP session
# --------------------------------------------------------------------------- #


class _FakeTool:
    def __init__(self, name: str, description: str = "", schema: dict | None = None) -> None:
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object", "properties": {}}


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text
        self.type = "text"


class _FakeResult:
    def __init__(self, text: str = "", *, is_error: bool = False) -> None:
        self.content = [_FakeContent(text)] if text else []
        self.isError = is_error


class _FakeListToolsResp:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self.tools = tools


class FakeSession:
    """A tiny stand-in for ``mcp.ClientSession``.

    Behaviour can be tuned per-instance: tool listing, per-tool replies,
    delays, and exceptions all configurable.
    """

    def __init__(
        self,
        tools: list[_FakeTool] | None = None,
        *,
        replies: dict[str, Any] | None = None,
        list_error: Exception | None = None,
        call_delays: dict[str, float] | None = None,
    ) -> None:
        self._tools = tools or []
        self._replies = replies or {}
        self._list_error = list_error
        self._call_delays = call_delays or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def list_tools(self) -> _FakeListToolsResp:
        if self._list_error is not None:
            raise self._list_error
        return _FakeListToolsResp(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        delay = self._call_delays.get(name, 0)
        if delay:
            await asyncio.sleep(delay)
        reply = self._replies.get(name)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, _FakeResult):
            return reply
        return _FakeResult(text=str(reply) if reply is not None else "ok")


def _spec_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "demo",
        "transport": "stdio",
        "command": ["echo", "hi"],
        "tool_prefix": "demo_",
    }
    base.update(overrides)
    return base


def _make_factory(sessions: dict[str, FakeSession]):
    async def _factory(spec: ServerSpec, stack: AsyncExitStack) -> FakeSession:
        session = sessions[spec.name]

        async def _on_close() -> None:
            session.closed = True

        stack.push_async_callback(_on_close)
        return session

    return _factory


def _failing_factory(error: Exception):
    async def _factory(spec: ServerSpec, stack: AsyncExitStack) -> FakeSession:
        raise error

    return _factory


# --------------------------------------------------------------------------- #
# expand_env
# --------------------------------------------------------------------------- #


def test_expand_env_handles_strings_lists_dicts_and_tuples() -> None:
    env = {"FOO": "x", "BAR": "y"}
    out = expand_env(
        {
            "a": "${FOO}-${BAR}",
            "b": ["${FOO}", 1, ("${BAR}",)],
            "c": {"nested": "${FOO}${MISSING}"},
            "d": 42,
        },
        env,
    )
    assert out == {
        "a": "x-y",
        "b": ["x", 1, ("y",)],
        "c": {"nested": "x"},
        "d": 42,
    }


def test_expand_env_uses_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TEST_VAR", "hello")
    assert expand_env("${MCP_TEST_VAR}!") == "hello!"


# --------------------------------------------------------------------------- #
# ServerSpec validation
# --------------------------------------------------------------------------- #


def test_serverspec_rejects_stdio_without_command() -> None:
    with pytest.raises(ValueError, match="command"):
        ServerSpec(name="x", transport="stdio")


def test_serverspec_rejects_stdio_with_url() -> None:
    with pytest.raises(ValueError, match="url"):
        ServerSpec(name="x", transport="stdio", command=["x"], url="http://x")


def test_serverspec_rejects_http_without_url() -> None:
    with pytest.raises(ValueError, match="url"):
        ServerSpec(name="x", transport="http")


def test_serverspec_rejects_http_with_command() -> None:
    with pytest.raises(ValueError, match="command"):
        ServerSpec(name="x", transport="http", url="http://x", command=["nope"])


def test_serverspec_rejects_sse_without_url() -> None:
    with pytest.raises(ValueError, match="url"):
        ServerSpec(name="x", transport="sse")


# --------------------------------------------------------------------------- #
# load_config
# --------------------------------------------------------------------------- #


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(MCPClientError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_load_config_invalid_yaml_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "servers: [unclosed\n")
    with pytest.raises(MCPClientError, match="Invalid YAML"):
        load_config(p)


def test_load_config_top_level_must_be_mapping(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "- 1\n- 2\n")
    with pytest.raises(MCPClientError, match="mapping"):
        load_config(p)


def test_load_config_servers_must_be_list(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "servers: 'oops'\n")
    with pytest.raises(MCPClientError, match="list"):
        load_config(p)


def test_load_config_each_entry_must_be_mapping(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "servers:\n  - 'oops'\n")
    with pytest.raises(MCPClientError, match="mapping"):
        load_config(p)


def test_load_config_invalid_spec_wrapped(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "servers:\n  - name: x\n    transport: stdio\n")
    with pytest.raises(MCPClientError, match="invalid server spec"):
        load_config(p)


def test_load_config_rejects_duplicate_names(tmp_path: Path) -> None:
    body = (
        "servers:\n"
        "  - name: a\n    transport: stdio\n    command: [echo]\n"
        "  - name: a\n    transport: stdio\n    command: [echo]\n"
    )
    p = _write(tmp_path / "c.yaml", body)
    with pytest.raises(MCPClientError, match="duplicate"):
        load_config(p)


def test_load_config_env_substitution(tmp_path: Path) -> None:
    body = (
        "servers:\n"
        "  - name: a\n"
        "    transport: stdio\n"
        "    command: [echo, '${GREETING}']\n"
        "    env:\n      TOKEN: ${SECRET}\n"
    )
    p = _write(tmp_path / "c.yaml", body)
    specs = load_config(p, environ={"GREETING": "hi", "SECRET": "s3cr3t"})
    assert specs[0].command == ("echo", "hi")
    assert specs[0].env == {"TOKEN": "s3cr3t"}


def test_load_config_empty_yaml_returns_no_servers(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "")
    assert load_config(p) == []


# --------------------------------------------------------------------------- #
# MCPManager.start / dispatch / aclose
# --------------------------------------------------------------------------- #


def _config_with(*entries: str) -> str:
    return "servers:\n" + "".join(entries)


def _entry(name: str, *, prefix: str = "", enabled: bool = True, command: str = "echo") -> str:
    return (
        f"  - name: {name}\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    transport: stdio\n"
        f"    command: [{command}]\n"
        f"    tool_prefix: {prefix!r}\n"
        f"    timeout_s: 1.0\n"
    )


def test_start_registers_prefixed_tools_and_dispatch_works(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "c.yaml", _config_with(_entry("alpha", prefix="a_")))
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    sess = FakeSession(
        tools=[_FakeTool("search", "find things", schema)],
        replies={"search": _FakeResult("found-it")},
    )
    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=_make_factory({"alpha": sess}))

    asyncio.run(_run_dispatch(mgr, reg, sess))

    statuses = mgr.list_status()
    assert len(statuses) == 1
    assert statuses[0].connected is True
    assert statuses[0].tool_count == 1
    assert statuses[0].tools == ["a_search"]


async def _run_dispatch(mgr: MCPManager, reg: Registry, sess: FakeSession) -> None:
    await mgr.start()
    try:
        tool = reg.get("a_search")
        # Schema preserved verbatim from the remote tool.
        assert tool.parameters_schema == {
            "type": "object",
            "properties": {"q": {"type": "string"}},
        }
        assert tool.description == "find things"
        assert tool.source == "mcp"
        result = await tool.fn(q="hello")
        assert result.ok is True
        assert result.content == "found-it"
        assert sess.calls == [("search", {"q": "hello"})]
    finally:
        await mgr.aclose()
    assert sess.closed is True


def test_disabled_servers_are_skipped(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "c.yaml",
        _config_with(_entry("on_srv", prefix="o_"), _entry("off_srv", enabled=False)),
    )
    sess_on = FakeSession(tools=[_FakeTool("ping")])
    factory = _make_factory({"on_srv": sess_on})
    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=factory)

    async def go() -> None:
        await mgr.start()
        statuses = {s.name: s for s in mgr.list_status()}
        assert statuses["off_srv"].connected is False
        assert statuses["off_srv"].error == "disabled"
        assert statuses["on_srv"].connected is True
        assert "o_ping" in reg
        await mgr.aclose()

    asyncio.run(go())


def test_failing_server_isolated_from_others(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "c.yaml",
        _config_with(_entry("good", prefix="g_"), _entry("bad", prefix="b_")),
    )
    good = FakeSession(tools=[_FakeTool("ok")])
    bad = FakeSession(list_error=RuntimeError("nope"))

    async def factory(spec: ServerSpec, stack: AsyncExitStack) -> FakeSession:
        sess = good if spec.name == "good" else bad

        async def _on_close() -> None:
            sess.closed = True

        stack.push_async_callback(_on_close)
        if spec.name == "bad":
            # Simulate connect-time failure by raising during list_tools.
            return bad
        return good

    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=factory)

    async def go() -> None:
        await mgr.start()
        statuses = {s.name: s for s in mgr.list_status()}
        assert statuses["good"].connected is True
        assert statuses["bad"].connected is False
        assert "nope" in (statuses["bad"].error or "")
        assert "g_ok" in reg
        assert not any(name.startswith("b_") for name in reg.names())
        await mgr.aclose()

    asyncio.run(go())


def test_unreachable_server_at_factory_time(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "c.yaml", _config_with(_entry("dead")))
    reg = Registry()
    mgr = MCPManager(
        cfg,
        registry=reg,
        session_factory=_failing_factory(ConnectionError("boom")),
    )

    async def go() -> None:
        await mgr.start()
        statuses = mgr.list_status()
        assert statuses[0].connected is False
        assert "boom" in (statuses[0].error or "")
        await mgr.aclose()

    asyncio.run(go())


def test_dispatch_call_error_returns_tool_result(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "c.yaml", _config_with(_entry("s", prefix="s_")))
    sess = FakeSession(
        tools=[_FakeTool("act")],
        replies={"act": RuntimeError("explode")},
    )
    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=_make_factory({"s": sess}))

    async def go() -> None:
        await mgr.start()
        result = await reg.get("s_act").fn()
        assert result.ok is False
        assert "explode" in (result.error or "")
        await mgr.aclose()

    asyncio.run(go())


def test_dispatch_tool_reports_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "c.yaml", _config_with(_entry("s", prefix="s_")))
    sess = FakeSession(
        tools=[_FakeTool("act")],
        replies={"act": _FakeResult("bad-input", is_error=True)},
    )
    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=_make_factory({"s": sess}))

    async def go() -> None:
        await mgr.start()
        result = await reg.get("s_act").fn()
        assert result.ok is False
        assert result.error == "bad-input"
        # Empty error text falls back to a default message.
        sess._replies["act"] = _FakeResult("", is_error=True)
        result = await reg.get("s_act").fn()
        assert result.ok is False
        assert "tool reported error" in (result.error or "")
        await mgr.aclose()

    asyncio.run(go())


def test_dispatch_timeout_returns_error(tmp_path: Path) -> None:
    body = (
        "servers:\n"
        "  - name: slow\n    enabled: true\n    transport: stdio\n"
        "    command: [echo]\n    tool_prefix: 's_'\n    timeout_s: 0.05\n"
    )
    cfg = _write(tmp_path / "c.yaml", body)
    sess = FakeSession(tools=[_FakeTool("nap")], call_delays={"nap": 1.0})
    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=_make_factory({"slow": sess}))

    async def go() -> None:
        await mgr.start()
        result = await reg.get("s_nap").fn()
        assert result.ok is False
        assert "timeout" in (result.error or "")
        await mgr.aclose()

    asyncio.run(go())


def test_prefix_collisions_keep_each_server_namespaced(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "c.yaml",
        _config_with(_entry("a", prefix="a_"), _entry("b", prefix="b_")),
    )
    sa = FakeSession(tools=[_FakeTool("dup")], replies={"dup": _FakeResult("from-a")})
    sb = FakeSession(tools=[_FakeTool("dup")], replies={"dup": _FakeResult("from-b")})
    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=_make_factory({"a": sa, "b": sb}))

    async def go() -> None:
        await mgr.start()
        assert reg.names() == ["a_dup", "b_dup"]
        ra = await reg.get("a_dup").fn()
        rb = await reg.get("b_dup").fn()
        assert ra.content == "from-a"
        assert rb.content == "from-b"
        await mgr.aclose()

    asyncio.run(go())


def test_reload_adds_removes_and_keeps_unchanged(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "c.yaml",
        _config_with(_entry("keep", prefix="k_"), _entry("drop", prefix="d_")),
    )
    keep = FakeSession(tools=[_FakeTool("t")])
    drop = FakeSession(tools=[_FakeTool("t")])
    add = FakeSession(tools=[_FakeTool("t")])
    sessions = {"keep": keep, "drop": drop, "add": add}
    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=_make_factory(sessions))

    async def go() -> None:
        await mgr.start()
        assert {"k_t", "d_t"} <= set(reg.names())

        # Rewrite the config: drop "drop", add "add".
        cfg.write_text(
            _config_with(_entry("keep", prefix="k_"), _entry("add", prefix="a_")),
            encoding="utf-8",
        )
        await mgr.reload()
        assert set(reg.names()) == {"k_t", "a_t"}
        assert drop.closed is True
        # The 'keep' session was reused, not closed and re-opened.
        assert keep.closed is False
        names = {s.name for s in mgr.list_status()}
        assert names == {"keep", "add"}
        await mgr.aclose()
        assert keep.closed is True
        assert add.closed is True

    asyncio.run(go())


def test_reload_reopens_when_spec_changes(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "c.yaml", _config_with(_entry("s", prefix="s_")))
    s1 = FakeSession(tools=[_FakeTool("v1")])
    s2 = FakeSession(tools=[_FakeTool("v2")])
    seq = iter([s1, s2])

    async def factory(spec: ServerSpec, stack: AsyncExitStack) -> FakeSession:
        sess = next(seq)

        async def _on_close() -> None:
            sess.closed = True

        stack.push_async_callback(_on_close)
        return sess

    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=factory)

    async def go() -> None:
        await mgr.start()
        assert "s_v1" in reg
        # Change the timeout_s -> spec inequality -> reconnect.
        body = (
            "servers:\n"
            "  - name: s\n    enabled: true\n    transport: stdio\n"
            "    command: [echo]\n    tool_prefix: 's_'\n    timeout_s: 5.0\n"
        )
        cfg.write_text(body, encoding="utf-8")
        await mgr.reload()
        assert "s_v2" in reg
        assert "s_v1" not in reg
        assert s1.closed is True
        await mgr.aclose()

    asyncio.run(go())


def test_disconnect_swallows_close_errors(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "c.yaml", _config_with(_entry("x", prefix="x_")))
    sess = FakeSession(tools=[_FakeTool("t")])

    async def factory(spec: ServerSpec, stack: AsyncExitStack) -> FakeSession:
        async def _on_close() -> None:
            raise RuntimeError("close-fail")

        stack.push_async_callback(_on_close)
        return sess

    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=factory)

    async def go() -> None:
        await mgr.start()
        # Should NOT raise even though the close callback errors.
        await mgr.aclose()

    asyncio.run(go())


def test_tool_listing_supports_dict_entries(tmp_path: Path) -> None:
    """The fake's content extractor handles dict-shaped tools and content."""
    from orchestrator.tools.mcp_client import _content_to_text

    assert _content_to_text(None) == ""
    assert _content_to_text("plain") == "plain"
    assert _content_to_text([{"text": "a"}, {"other": 1}]) == "a\n{'other': 1}"
    assert _content_to_text(123) == "123"

    # Manager should also accept dict-shaped remote tools.
    cfg = _write(tmp_path / "c.yaml", _config_with(_entry("d", prefix="d_")))

    class DictSession(FakeSession):
        async def list_tools(self) -> Any:
            return _FakeListToolsResp(
                [
                    {  # type: ignore[list-item]
                        "name": "raw",
                        "description": "dict-shaped",
                        "inputSchema": {"type": "object"},
                    }
                ]
            )

    sess = DictSession(replies={"raw": _FakeResult("ok")})
    reg = Registry()
    mgr = MCPManager(cfg, registry=reg, session_factory=_make_factory({"d": sess}))

    async def go() -> None:
        await mgr.start()
        tool = reg.get("d_raw")
        assert tool.description == "dict-shaped"
        assert tool.parameters_schema == {"type": "object"}
        await mgr.aclose()

    asyncio.run(go())


def test_default_registry_is_used_when_unspecified(tmp_path: Path) -> None:
    from orchestrator.tools.registry import default_registry

    default_registry.clear()
    cfg = _write(tmp_path / "c.yaml", _config_with(_entry("z", prefix="z_")))
    sess = FakeSession(tools=[_FakeTool("ping")])
    mgr = MCPManager(cfg, session_factory=_make_factory({"z": sess}))

    async def go() -> None:
        await mgr.start()
        assert "z_ping" in default_registry
        await mgr.aclose()
        assert "z_ping" not in default_registry

    asyncio.run(go())


def test_config_path_property(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "c.yaml", _config_with(_entry("a")))
    mgr = MCPManager(cfg)
    assert mgr.config_path == cfg
