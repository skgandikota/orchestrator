"""Compatibility shim for the ``orchestrator mcp`` CLI subcommand.

The MCP stdio server lives in :mod:`orchestrator.mcp.server`; this
module re-exports the public entry points so the existing CLI wiring
(``orchestrator.interfaces.cli:mcp``) continues to work without
duplication.
"""

from __future__ import annotations

from orchestrator.mcp.server import build_server, main, run

__all__ = ["build_server", "main", "run"]
