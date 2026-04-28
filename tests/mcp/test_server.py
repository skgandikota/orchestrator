"""Tests for the coracle MCP stdio server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mt
import pytest
from mcp.server import Server

from coracle.api.tasks import JobStatus, PipelineEvent
from coracle.mcp import server as mcp_server
from coracle.mcp.server import (
    HANDLERS,
    SERVER_NAME,
    SERVER_VERSION,
    TOOL_DEFINITIONS,
    build_server,
    main,
    run,
)


class _FakeJob:
    def __init__(self, job_id: str = "job-123") -> None:
        self.id = job_id
        self.status = JobStatus.RUNNING


class _FakeManager:
    """Minimal JobManager double exercising the MCP handler surface."""

    def __init__(self, events: list[PipelineEvent] | None = None) -> None:
        self._events = events or []
        self.submitted: list[tuple[str, str | None]] = []
        self.cancelled: list[str] = []
        self.status_calls: list[tuple[str, str]] = []
        self.get_calls: list[str] = []

    def submit(self, user_msg: str, model: str | None) -> _FakeJob:
        self.submitted.append((user_msg, model))
        return _FakeJob()

    def get(self, job_id: str) -> _FakeJob:
        self.get_calls.append(job_id)
        return _FakeJob(job_id)

    def status_payload(self, job: _FakeJob, mode: str) -> dict[str, Any]:
        self.status_calls.append((job.id, mode))
        return {"mode": mode, "status": job.status.value, "id": job.id}

    async def stream(self, job: _FakeJob) -> AsyncIterator[PipelineEvent]:
        for ev in self._events:
            yield ev

    async def cancel(self, job: _FakeJob) -> None:
        self.cancelled.append(job.id)
        job.status = JobStatus.CANCELLED


def _decode(blocks: list[mt.ContentBlock]) -> list[Any]:
    out: list[Any] = []
    for blk in blocks:
        assert isinstance(blk, mt.TextContent)
        out.append(json.loads(blk.text))
    return out


# --------------------------------------------------------------------------- #
# Tool definitions                                                            #
# --------------------------------------------------------------------------- #
def test_tool_definitions_cover_required_surface() -> None:
    names = [t.name for t in TOOL_DEFINITIONS]
    assert names == ["submit_job", "get_status", "stream_job", "cancel_job"]
    for tool in TOOL_DEFINITIONS:
        schema = tool.inputSchema
        assert schema["type"] == "object"
        assert "properties" in schema
        assert tool.description


def test_get_status_schema_constrains_mode() -> None:
    [tool] = [t for t in TOOL_DEFINITIONS if t.name == "get_status"]
    mode = tool.inputSchema["properties"]["mode"]
    assert mode["enum"] == ["a", "b", "c"]
    assert mode["default"] == "a"


# --------------------------------------------------------------------------- #
# Direct handler routing                                                      #
# --------------------------------------------------------------------------- #
def test_submit_job_handler_delegates_to_manager() -> None:
    mgr = _FakeManager()
    blocks = asyncio.run(HANDLERS["submit_job"](mgr, {"user_msg": "hi", "model": "m"}))
    assert mgr.submitted == [("hi", "m")]
    assert _decode(blocks) == [{"job_id": "job-123"}]


def test_submit_job_handler_defaults_model_to_none() -> None:
    mgr = _FakeManager()
    asyncio.run(HANDLERS["submit_job"](mgr, {"user_msg": "go"}))
    assert mgr.submitted == [("go", None)]


def test_get_status_handler_uses_default_mode() -> None:
    mgr = _FakeManager()
    blocks = asyncio.run(HANDLERS["get_status"](mgr, {"job_id": "abc"}))
    assert mgr.status_calls == [("abc", "a")]
    assert _decode(blocks) == [{"mode": "a", "status": "running", "id": "abc"}]


def test_get_status_handler_passes_mode_through() -> None:
    mgr = _FakeManager()
    blocks = asyncio.run(HANDLERS["get_status"](mgr, {"job_id": "x", "mode": "c"}))
    assert mgr.status_calls == [("x", "c")]
    assert _decode(blocks)[0]["mode"] == "c"


def test_stream_job_handler_drains_events() -> None:
    events = [
        PipelineEvent(kind="started", data={"k": 1}, ts=1.0),
        PipelineEvent(kind="completed", data={}, ts=2.0),
    ]
    mgr = _FakeManager(events=events)
    blocks = asyncio.run(HANDLERS["stream_job"](mgr, {"job_id": "j"}))
    decoded = _decode(blocks)
    assert [d["kind"] for d in decoded] == ["started", "completed"]
    assert decoded[0]["data"] == {"k": 1}
    assert decoded[0]["ts"] == 1.0


def test_cancel_job_handler_marks_cancelled() -> None:
    mgr = _FakeManager()
    blocks = asyncio.run(HANDLERS["cancel_job"](mgr, {"job_id": "z"}))
    assert mgr.cancelled == ["z"]
    payload = _decode(blocks)[0]
    assert payload == {"ok": True, "job_id": "z", "status": "cancelled"}


# --------------------------------------------------------------------------- #
# Server wiring                                                               #
# --------------------------------------------------------------------------- #
def test_build_server_uses_default_manager_when_omitted() -> None:
    sentinel = _FakeManager()
    with patch.object(mcp_server, "get_job_manager", return_value=sentinel) as gm:
        server = build_server()
    assert gm.called
    assert isinstance(server, Server)
    assert server.name == SERVER_NAME
    assert server.version == SERVER_VERSION


def test_build_server_registers_list_and_call_handlers() -> None:
    server = build_server(mgr=_FakeManager())
    assert mt.ListToolsRequest in server.request_handlers
    assert mt.CallToolRequest in server.request_handlers


def test_list_tools_request_returns_full_definitions() -> None:
    server = build_server(mgr=_FakeManager())
    handler = server.request_handlers[mt.ListToolsRequest]
    req = mt.ListToolsRequest(method="tools/list")
    result = asyncio.run(handler(req))
    payload = result.root
    assert isinstance(payload, mt.ListToolsResult)
    assert [t.name for t in payload.tools] == [
        "submit_job",
        "get_status",
        "stream_job",
        "cancel_job",
    ]


def test_call_tool_request_routes_to_submit_handler() -> None:
    mgr = _FakeManager()
    server = build_server(mgr=mgr)
    handler = server.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(
            name="submit_job",
            arguments={"user_msg": "hello", "model": None},
        ),
    )
    result = asyncio.run(handler(req))
    payload = result.root
    assert isinstance(payload, mt.CallToolResult)
    assert not payload.isError
    [block] = payload.content
    assert isinstance(block, mt.TextContent)
    assert json.loads(block.text) == {"job_id": "job-123"}
    assert mgr.submitted == [("hello", None)]


def test_call_tool_request_unknown_tool_marks_error() -> None:
    server = build_server(mgr=_FakeManager())
    handler = server.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="bogus", arguments={}),
    )
    result = asyncio.run(handler(req))
    payload = result.root
    assert isinstance(payload, mt.CallToolResult)
    assert payload.isError is True


def test_call_tool_request_normalises_missing_arguments() -> None:
    """Routing must coerce ``None`` arguments to an empty mapping before
    dispatch so handlers can rely on a real dict."""

    captured: dict[str, Any] = {}

    async def fake_handler(_mgr: Any, args: dict[str, Any]) -> list[mt.ContentBlock]:
        captured["args"] = args
        return [mt.TextContent(type="text", text="{}")]

    with patch.dict(HANDLERS, {"probe": fake_handler}, clear=False):
        # Inject a synthetic tool entry so build_server's routing path runs.
        server = build_server(mgr=_FakeManager())
        handler = server.request_handlers[mt.CallToolRequest]
        req = mt.CallToolRequest(
            method="tools/call",
            params=mt.CallToolRequestParams(name="probe", arguments=None),
        )
        result = asyncio.run(handler(req))
        assert not result.root.isError
    assert captured["args"] == {}


# --------------------------------------------------------------------------- #
# stdio entrypoint                                                            #
# --------------------------------------------------------------------------- #
def test_main_runs_server_over_stdio_streams() -> None:
    fake_server = MagicMock(spec=Server)
    fake_server.run = AsyncMock()
    fake_server.create_initialization_options = MagicMock(return_value="init-opts")

    class _StdioCM:
        async def __aenter__(self) -> tuple[str, str]:
            return ("read", "write")

        async def __aexit__(self, *_exc: object) -> None:
            return None

    with (
        patch.object(mcp_server, "build_server", return_value=fake_server) as build,
        patch.object(mcp_server, "stdio_server", return_value=_StdioCM()) as stdio,
    ):
        asyncio.run(main())

    build.assert_called_once_with()
    stdio.assert_called_once_with()
    fake_server.run.assert_awaited_once_with("read", "write", "init-opts")


def test_run_invokes_asyncio_run_with_main_coroutine() -> None:
    captured: list[Any] = []

    def fake_asyncio_run(coro: Any) -> None:
        captured.append(coro)
        coro.close()

    with (
        patch.object(mcp_server.asyncio, "run", side_effect=fake_asyncio_run),
        patch.object(mcp_server, "main", wraps=main) as main_spy,
    ):
        run()

    main_spy.assert_called_once_with()
    assert captured and asyncio.iscoroutine(captured[0])


# --------------------------------------------------------------------------- #
# Package surface                                                             #
# --------------------------------------------------------------------------- #
def test_package_reexports_public_api() -> None:
    from coracle import mcp as pkg

    assert pkg.build_server is build_server
    assert pkg.main is main
    assert pkg.run is run


def test_dunder_main_module_imports_run() -> None:
    import importlib

    mod = importlib.import_module("coracle.mcp.__main__")
    assert mod.run is run


def test_interfaces_shim_reexports_public_api() -> None:
    from coracle.interfaces import mcp_server as shim

    assert shim.build_server is build_server
    assert shim.main is main
    assert shim.run is run


@pytest.mark.parametrize("tool_name", ["submit_job", "get_status", "stream_job", "cancel_job"])
def test_each_handler_registered(tool_name: str) -> None:
    assert tool_name in HANDLERS
