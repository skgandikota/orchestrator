"""``python -m coracle.mcp`` entrypoint."""

from __future__ import annotations

from coracle.mcp.server import run

if __name__ == "__main__":  # pragma: no cover - CLI shim
    run()
