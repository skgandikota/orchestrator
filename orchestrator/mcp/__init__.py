"""MCP server package for the orchestrator.

Exposes the orchestrator's job-manager surface (``submit_job``,
``get_status``, ``stream_job``, ``cancel_job``) over the Model Context
Protocol stdio transport so MCP-aware clients (Claude Desktop, IDEs,
etc.) can drive the orchestrator without an HTTP server.
"""

from __future__ import annotations

from orchestrator.mcp.server import build_server, main, run

__all__ = ["build_server", "main", "run"]
