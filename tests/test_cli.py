"""Tests for the coracle CLI."""

from __future__ import annotations

import io
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from coracle import cli
from coracle.tools.mcp_client import ServerSpec
from tests.tools.test_mcp_client import FakeSession, _FakeTool


def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "mcp.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _factory(sessions: dict[str, FakeSession]):
    async def _f(spec: ServerSpec, stack: AsyncExitStack) -> FakeSession:
        sess = sessions[spec.name]

        async def _close() -> None:
            sess.closed = True

        stack.push_async_callback(_close)
        return sess

    return _f


def test_build_parser_requires_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    captured = capsys.readouterr()
    assert "command" in captured.err.lower() or "usage" in captured.err.lower()


def test_run_mcp_list_prints_status(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        "servers:\n"
        "  - name: a\n    enabled: true\n    transport: stdio\n"
        "    command: [echo]\n    tool_prefix: 'a_'\n    timeout_s: 1\n"
        "  - name: b\n    enabled: false\n    transport: stdio\n"
        "    command: [echo]\n    tool_prefix: ''\n    timeout_s: 1\n",
    )
    sessions = {"a": FakeSession(tools=[_FakeTool("ping")])}
    buf = io.StringIO()
    import asyncio

    rc = asyncio.run(cli.run_mcp_command("list", cfg, session_factory=_factory(sessions), out=buf))
    assert rc == 0
    output = buf.getvalue()
    assert "a\tstdio\tok\ttools=1" in output
    assert "b\tstdio\tdisabled\ttools=0" in output


def test_run_mcp_reload_prints_status(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        "servers:\n"
        "  - name: a\n    enabled: true\n    transport: stdio\n"
        "    command: [echo]\n    tool_prefix: 'a_'\n    timeout_s: 1\n",
    )
    sessions = {"a": FakeSession(tools=[_FakeTool("ping")])}
    buf = io.StringIO()
    import asyncio

    rc = asyncio.run(
        cli.run_mcp_command("reload", cfg, session_factory=_factory(sessions), out=buf)
    )
    assert rc == 0
    assert "a\tstdio\tok" in buf.getvalue()


def test_run_mcp_list_handles_missing_config(tmp_path: Path) -> None:
    import asyncio

    rc = asyncio.run(cli.run_mcp_command("list", tmp_path / "nope.yaml"))
    assert rc == 2


def test_run_mcp_list_handles_empty_config(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path, "servers: []\n")
    buf = io.StringIO()
    import asyncio

    rc = asyncio.run(cli.run_mcp_command("list", cfg, out=buf))
    assert rc == 0
    assert "no MCP servers" in buf.getvalue()


def test_main_dispatches_to_mcp_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_cfg(tmp_path, "servers: []\n")

    captured: dict[str, Any] = {}

    async def fake_run(action: str, path: Path) -> int:
        captured["action"] = action
        captured["path"] = path
        return 0

    monkeypatch.setattr(cli, "run_mcp_command", fake_run)
    rc = cli.main(["mcp", "list", "--config", str(cfg)])
    assert rc == 0
    assert captured == {"action": "list", "path": cfg}
