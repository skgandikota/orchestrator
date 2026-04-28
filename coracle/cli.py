"""Console entry point for the coracle.

Currently exposes the ``mcp`` subcommands needed by issue #45:

* ``coracle mcp list`` — print every configured MCP server, its
  transport, status, and the number of tools it exposes.
* ``coracle mcp reload`` — re-read the MCP config file without
  restarting any longer-running process (in single-shot CLI mode this
  simply demonstrates the reload codepath against a fresh manager).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from coracle.core.logging import configure_logging
from coracle.tools.mcp_client import MCPClientError, MCPManager, SessionFactory

__all__ = ["build_parser", "main", "run_mcp_command"]

DEFAULT_CONFIG = Path("config/mcp_servers.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coracle")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    mcp = sub.add_parser("mcp", help="Manage MCP server connections.")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)

    for name, help_text in (
        ("list", "List configured MCP servers and their tool counts."),
        ("reload", "Re-read the MCP config and reconnect changed servers."),
    ):
        sp = mcp_sub.add_parser(name, help=help_text)
        sp.add_argument(
            "--config",
            type=Path,
            default=DEFAULT_CONFIG,
            help=f"Path to MCP config YAML (default: {DEFAULT_CONFIG}).",
        )
    return parser


async def run_mcp_command(
    action: str,
    config_path: Path,
    *,
    session_factory: SessionFactory | None = None,
    out: TextIO | None = None,
) -> int:
    """Execute ``mcp list`` or ``mcp reload``; return process exit code."""
    stream = out if out is not None else sys.stdout
    manager = MCPManager(config_path, session_factory=session_factory)
    try:
        await manager.start()
        if action == "reload":
            await manager.reload()
        statuses = manager.list_status()
        if not statuses:
            print("(no MCP servers configured)", file=stream)
        else:
            for status in statuses:
                state = "ok" if status.connected else (status.error or "down")
                print(
                    f"{status.name}\t{status.transport}\t{state}\t"
                    f"tools={status.tool_count}\tprefix={status.tool_prefix!r}",
                    file=stream,
                )
        return 0
    except MCPClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await manager.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(level=args.log_level)
    if args.command == "mcp":
        return asyncio.run(run_mcp_command(args.mcp_command, args.config))
    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
