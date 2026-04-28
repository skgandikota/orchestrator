"""``python -m orchestrator.mcp`` entrypoint."""

from __future__ import annotations

from orchestrator.mcp.server import run

if __name__ == "__main__":  # pragma: no cover - CLI shim
    run()
