"""Compatibility shim for the ``coracle mcp`` CLI subcommand.

The MCP stdio server lives in :mod:`coracle.mcp.server`; this
module re-exports the public entry points so the existing CLI wiring
(``coracle.interfaces.cli:mcp``) continues to work without
duplication.
"""

from __future__ import annotations

from coracle.mcp.server import build_server, main, run

__all__ = ["build_server", "main", "run"]
