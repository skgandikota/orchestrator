"""Tests for dynamic MCP attachment in /v1/chat/completions (issue #56)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coracle.api import create_app, openai_compat
from coracle.api._mcp_attach import (
    MCPServerRequest,
    attach_mcp_tools,
    set_session_factory,
)
from coracle.api.openai_compat import (
    Message,
    PipelineEvent,
    set_backend,
)
from coracle.tools.mcp_client import ServerSpec

# ---------------------------------------------------------------------------
# MCP test doubles
# ---------------------------------------------------------------------------


class _Tool:
    def __init__(self, name: str, description: str = "", schema: dict | None = None) -> None:
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object"}


class _ToolList:
    def __init__(self, tools: list[_Tool]) -> None:
        self.tools = tools


class _CallResult:
    def __init__(self, text: str = "", is_error: bool = False) -> None:
        class _Block:
            def __init__(self, t: str) -> None:
                self.text = t

        self.content = [_Block(text)] if text else []
        self.isError = is_error


class FakeSession:
    def __init__(
        self,
        tools: list[_Tool],
        *,
        results: dict[str, _CallResult] | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        self._tools = tools
        self._results = results or {}
        self._raise = raise_on_call
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> _ToolList:
        return _ToolList(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _CallResult:
        self.calls.append((name, arguments))
        if self._raise is not None:
            raise self._raise
        return self._results.get(name, _CallResult(text=f"ran {name}"))


def make_factory(
    sessions_by_name: dict[str, FakeSession | Exception],
) -> Any:
    async def factory(spec: ServerSpec, stack: AsyncExitStack) -> Any:
        outcome = sessions_by_name[spec.name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return factory


# ---------------------------------------------------------------------------
# Direct attach() unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_lists_tools_with_default_prefix() -> None:
    fake = FakeSession([_Tool("ping", "p", {"type": "object"})])
    factory = make_factory({"orc": fake})

    attached = await attach_mcp_tools(
        [MCPServerRequest(name="orc", url="http://x", transport="http")],
        session_factory=factory,
    )
    try:
        assert attached.servers == ["orc"]
        assert attached.tool_names == ["orc__ping"]
        tool = attached.tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "orc__ping"
        assert tool["function"]["description"] == "p"
        assert tool["function"]["parameters"] == {"type": "object"}
        assert tool["server"] == "orc"
    finally:
        await attached.aclose()


@pytest.mark.asyncio
async def test_attach_honours_custom_prefix_and_dispatch() -> None:
    fake = FakeSession(
        [_Tool("search"), _Tool("fetch")],
        results={"search": _CallResult(text="hit"), "fetch": _CallResult(text="bytes")},
    )
    factory = make_factory({"web": fake})

    attached = await attach_mcp_tools(
        [MCPServerRequest(name="web", url="http://x", transport="http", tool_prefix="w_")],
        session_factory=factory,
    )
    try:
        assert sorted(attached.tool_names) == ["w_fetch", "w_search"]
        result = await attached.call("w_search", {"q": "hi"})
        assert result == {"ok": True, "content": "hit", "server": "web"}
        assert fake.calls == [("search", {"q": "hi"})]
    finally:
        await attached.aclose()


@pytest.mark.asyncio
async def test_attach_skips_unreachable_server() -> None:
    fake = FakeSession([_Tool("ok")])
    factory = make_factory(
        {"good": fake, "bad": ConnectionError("boom")},
    )
    attached = await attach_mcp_tools(
        [
            MCPServerRequest(name="good", url="http://g", transport="http"),
            MCPServerRequest(name="bad", url="http://b", transport="http"),
        ],
        session_factory=factory,
    )
    try:
        assert attached.servers == ["good"]
        assert attached.tool_names == ["good__ok"]
        assert "bad" in attached.errors
        assert "boom" in attached.errors["bad"]
    finally:
        await attached.aclose()


@pytest.mark.asyncio
async def test_attach_dispatch_unknown_tool_returns_error() -> None:
    fake = FakeSession([_Tool("a")])
    factory = make_factory({"s": fake})
    attached = await attach_mcp_tools(
        [MCPServerRequest(name="s", url="http://x", transport="http")],
        session_factory=factory,
    )
    try:
        result = await attached.call("nope", {})
        assert result["ok"] is False
        assert "unknown" in result["error"]
    finally:
        await attached.aclose()


@pytest.mark.asyncio
async def test_attach_dispatch_session_raises() -> None:
    fake = FakeSession([_Tool("boom")], raise_on_call=RuntimeError("nope"))
    factory = make_factory({"s": fake})
    attached = await attach_mcp_tools(
        [MCPServerRequest(name="s", url="http://x", transport="http")],
        session_factory=factory,
    )
    try:
        result = await attached.call("s__boom", {})
        assert result == {"ok": False, "error": "nope", "server": "s"}
    finally:
        await attached.aclose()


@pytest.mark.asyncio
async def test_attach_dispatch_tool_reports_error() -> None:
    fake = FakeSession(
        [_Tool("bad")],
        results={"bad": _CallResult(text="kapow", is_error=True)},
    )
    factory = make_factory({"s": fake})
    attached = await attach_mcp_tools(
        [MCPServerRequest(name="s", url="http://x", transport="http")],
        session_factory=factory,
    )
    try:
        result = await attached.call("s__bad", {})
        assert result == {"ok": False, "error": "kapow", "server": "s"}
    finally:
        await attached.aclose()


@pytest.mark.asyncio
async def test_attach_handles_dict_tool_descriptors_and_empty_content() -> None:
    class _Sess:
        async def list_tools(self) -> Any:
            return {
                "tools": [
                    {"name": "d1", "description": "x", "inputSchema": {"type": "object"}},
                    {"name": "", "description": "skip-me"},
                ]
            }

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            class _R:
                content = None
                isError = False

            return _R()

    async def factory(spec: ServerSpec, stack: AsyncExitStack) -> Any:
        return _Sess()

    attached = await attach_mcp_tools(
        [MCPServerRequest(name="srv", url="http://x", transport="http")],
        session_factory=factory,
    )
    try:
        assert attached.tool_names == ["srv__d1"]
        result = await attached.call("srv__d1", {})
        assert result == {"ok": True, "content": "", "server": "srv"}
    finally:
        await attached.aclose()


@pytest.mark.asyncio
async def test_attached_aclose_is_idempotent() -> None:
    fake = FakeSession([_Tool("x")])
    factory = make_factory({"s": fake})
    attached = await attach_mcp_tools(
        [MCPServerRequest(name="s", url="http://x", transport="http")],
        session_factory=factory,
    )
    await attached.aclose()
    await attached.aclose()  # second close is a no-op


def test_set_session_factory_falls_back_to_default() -> None:
    from coracle.api import _mcp_attach

    set_session_factory(None)
    assert _mcp_attach._SESSION_FACTORY is _mcp_attach.default_session_factory


def test_to_server_spec_stdio_round_trip() -> None:
    spec = MCPServerRequest(
        name="local",
        transport="stdio",
        command=("python", "-m", "srv"),
    ).to_server_spec()
    assert spec.transport == "stdio"
    assert spec.command == ("python", "-m", "srv")
    assert spec.tool_prefix == "local__"


# ---------------------------------------------------------------------------
# Endpoint integration: /v1/chat/completions with mcp_servers
# ---------------------------------------------------------------------------


class _Backend:
    """Minimal backend that emits a single token, reused by route tests."""

    async def stream(
        self,
        *,
        job_id: str,
        model: str,
        messages: list[Message],
        temperature: float | None,
        max_tokens: int | None,
    ) -> AsyncIterator[PipelineEvent]:
        yield PipelineEvent(type="token", text="ok")
        yield PipelineEvent(type="final", text="")


@pytest.fixture
def client_with_mcp() -> Iterator[tuple[TestClient, dict[str, FakeSession | Exception]]]:
    backend = _Backend()
    set_backend(backend)
    sessions: dict[str, FakeSession | Exception] = {}
    set_session_factory(make_factory(sessions))
    try:
        yield TestClient(create_app()), sessions
    finally:
        set_backend(openai_compat._StubBackend())
        set_session_factory(None)


def test_chat_completions_attaches_mcp_tools(
    client_with_mcp: tuple[TestClient, dict[str, FakeSession | Exception]],
) -> None:
    client, sessions = client_with_mcp
    sessions["orc"] = FakeSession(
        [_Tool("ping", "ping desc", {"type": "object"})],
    )
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "coracle",
            "messages": [{"role": "user", "content": "hi"}],
            "mcp_servers": [{"name": "orc", "url": "http://localhost:9", "transport": "http"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mcp"]["servers"] == ["orc"]
    assert body["mcp"]["tools"] == ["orc__ping"]
    assert body["mcp"]["errors"] == {}


def test_chat_completions_without_mcp_omits_block(
    client_with_mcp: tuple[TestClient, dict[str, FakeSession | Exception]],
) -> None:
    client, _ = client_with_mcp
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "coracle",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "mcp" not in body


def test_chat_completions_reports_unreachable_server(
    client_with_mcp: tuple[TestClient, dict[str, FakeSession | Exception]],
) -> None:
    client, sessions = client_with_mcp
    sessions["good"] = FakeSession([_Tool("a")])
    sessions["bad"] = ConnectionError("nope")
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "coracle",
            "messages": [{"role": "user", "content": "hi"}],
            "mcp_servers": [
                {"name": "good", "url": "http://g", "transport": "http"},
                {"name": "bad", "url": "http://b", "transport": "http"},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mcp"]["servers"] == ["good"]
    assert "bad" in body["mcp"]["errors"]


def test_chat_completions_stream_emits_mcp_chunk(
    client_with_mcp: tuple[TestClient, dict[str, FakeSession | Exception]],
) -> None:
    client, sessions = client_with_mcp
    sessions["orc"] = FakeSession([_Tool("ping")])
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "coracle",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "mcp_servers": [{"name": "orc", "url": "http://localhost:9", "transport": "http"}],
        },
    ) as resp:
        chunks = [line for line in resp.iter_lines() if line.startswith("data: ")]

    payloads: list[dict[str, Any]] = []
    for line in chunks:
        body = line.removeprefix("data: ").strip()
        if body == "[DONE]":
            continue
        payloads.append(json.loads(body))

    mcp_chunk = next(
        (p for p in payloads if "mcp" in p["choices"][0]["delta"]),
        None,
    )
    assert mcp_chunk is not None
    assert mcp_chunk["choices"][0]["delta"]["mcp"]["tools"] == ["orc__ping"]


def test_chat_completions_invalid_model_still_closes_attached(
    client_with_mcp: tuple[TestClient, dict[str, FakeSession | Exception]],
) -> None:
    """A bad model id is rejected before attach, so no session is opened."""
    client, sessions = client_with_mcp
    sessions["orc"] = FakeSession([_Tool("ping")])
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "not-a-model",
            "messages": [{"role": "user", "content": "hi"}],
            "mcp_servers": [{"name": "orc", "url": "http://x", "transport": "http"}],
        },
    )
    assert resp.status_code == 400
    # session_factory was never invoked since validation came first
    assert sessions["orc"].calls == []
