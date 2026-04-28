"""MCP stdio server exposing the orchestrator job manager.

The server registers four tools that mirror the native HTTP API:

* ``submit_job`` - enqueue a job and return its id immediately.
* ``get_status`` - return a status payload in mode ``a``, ``b`` or ``c``.
* ``stream_job`` - drain pipeline events for a job until terminal.
* ``cancel_job`` - cooperatively cancel a running job.

Every tool implementation delegates to
:class:`orchestrator.api.tasks.JobManager` so the MCP and HTTP
interfaces share a single source of truth.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp.types as mt
from mcp.server import Server
from mcp.server.stdio import stdio_server

from orchestrator.api.tasks import JobManager, get_job_manager

__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOL_DEFINITIONS",
    "build_server",
    "main",
    "run",
]

SERVER_NAME = "orchestrator"
SERVER_VERSION = "0.1.0"

TOOL_DEFINITIONS: list[mt.Tool] = [
    mt.Tool(
        name="submit_job",
        description="Submit a new orchestrator job; returns the job_id immediately.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_msg": {
                    "type": "string",
                    "description": "Goal/message for the job.",
                },
                "model": {
                    "type": ["string", "null"],
                    "description": "Optional model id; defaults to the orchestrator default.",
                },
            },
            "required": ["user_msg"],
            "additionalProperties": False,
        },
    ),
    mt.Tool(
        name="get_status",
        description="Return a status payload for a job in mode 'a', 'b' or 'c'.",
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["a", "b", "c"],
                    "default": "a",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    ),
    mt.Tool(
        name="stream_job",
        description=(
            "Drain pipeline events for a job until it reaches a terminal "
            "state and return them as a list of JSON content blocks."
        ),
        inputSchema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    ),
    mt.Tool(
        name="cancel_job",
        description="Cooperatively cancel a running job.",
        inputSchema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    ),
]


def _text(payload: Any) -> mt.TextContent:
    return mt.TextContent(type="text", text=json.dumps(payload, default=str))


async def _submit_job(mgr: JobManager, args: dict[str, Any]) -> list[mt.ContentBlock]:
    job = mgr.submit(args["user_msg"], args.get("model"))
    return [_text({"job_id": job.id})]


async def _get_status(mgr: JobManager, args: dict[str, Any]) -> list[mt.ContentBlock]:
    job = mgr.get(args["job_id"])
    mode = args.get("mode", "a")
    return [_text(mgr.status_payload(job, mode))]


async def _stream_job(mgr: JobManager, args: dict[str, Any]) -> list[mt.ContentBlock]:
    job = mgr.get(args["job_id"])
    blocks: list[mt.ContentBlock] = []
    async for ev in mgr.stream(job):
        blocks.append(_text({"kind": ev.kind, "data": ev.data, "ts": ev.ts}))
    return blocks


async def _cancel_job(mgr: JobManager, args: dict[str, Any]) -> list[mt.ContentBlock]:
    job = mgr.get(args["job_id"])
    await mgr.cancel(job)
    return [_text({"ok": True, "job_id": job.id, "status": job.status.value})]


HANDLERS: dict[str, Any] = {
    "submit_job": _submit_job,
    "get_status": _get_status,
    "stream_job": _stream_job,
    "cancel_job": _cancel_job,
}


def build_server(mgr: JobManager | None = None) -> Server:
    """Build a configured MCP :class:`~mcp.server.Server`.

    Parameters
    ----------
    mgr:
        Job manager to delegate to. Defaults to the process-wide manager
        returned by :func:`orchestrator.api.tasks.get_job_manager`.
    """
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)
    manager = mgr if mgr is not None else get_job_manager()

    @server.list_tools()
    async def _list_tools() -> list[mt.Tool]:
        return list(TOOL_DEFINITIONS)

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[mt.ContentBlock]:
        handler = HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        return await handler(manager, arguments or {})

    return server


async def main() -> None:
    """Run the stdio loop until the client disconnects (EOF)."""
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def run() -> None:
    """Synchronous entry point used by the CLI and ``python -m``."""
    asyncio.run(main())
