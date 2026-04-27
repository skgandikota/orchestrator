"""Tests for orchestrator.tools.shell."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from orchestrator.config.settings import Settings
from orchestrator.tools._sandbox import WorkspaceEscapeError
from orchestrator.tools.shell import CommandResult, DeniedCommandError, run_command

PY = sys.executable


def _settings(
    root: Path,
    *,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
) -> Settings:
    return Settings.model_validate(
        {
            "tools": {
                "fs": {"workspace_root": str(root)},
                "shell": {
                    "allow": allow if allow is not None else [],
                    "deny": deny if deny is not None else ["rm", "sudo", "mv", "dd"],
                },
            }
        }
    )


def test_allowed_command_success(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    res = run_command([PY, "-c", "print('ok')"], settings=s)
    assert isinstance(res, CommandResult)
    assert res.exit_code == 0
    assert res.stdout.strip() == "ok"
    assert res.stderr == ""
    assert res.timed_out is False
    assert res.duration_ms >= 0


def test_non_zero_exit_captured(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    res = run_command(
        [PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        settings=s,
    )
    assert res.exit_code == 3
    assert "boom" in res.stderr
    assert res.timed_out is False


def test_denied_command_rejected(tmp_path: Path) -> None:
    s = _settings(tmp_path, deny=["rm", "python", "python3", "python.exe"])
    with pytest.raises(DeniedCommandError):
        run_command([PY, "-c", "print(1)"], settings=s)


def test_allow_list_excludes_unlisted(tmp_path: Path) -> None:
    s = _settings(tmp_path, allow=["echo"])
    with pytest.raises(DeniedCommandError):
        run_command([PY, "-c", "print(1)"], settings=s)


def test_deny_overrides_allow(tmp_path: Path) -> None:
    exe = Path(PY).name
    base = exe.rsplit(".", 1)[0] if "." in exe else exe
    s = _settings(tmp_path, allow=[base], deny=[base])
    with pytest.raises(DeniedCommandError):
        run_command([PY, "-c", "print(1)"], settings=s)


def test_timeout_kills_process(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    res = run_command(
        [PY, "-c", "import time; time.sleep(30)"],
        timeout=0.5,
        settings=s,
    )
    assert res.timed_out is True
    assert res.exit_code == -1
    assert res.duration_ms < 10_000


def test_cwd_defaults_to_workspace_root(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    res = run_command(
        [PY, "-c", "import os; print(os.getcwd())"],
        settings=s,
    )
    assert res.exit_code == 0
    assert Path(res.stdout.strip()).resolve() == tmp_path.resolve()


def test_cwd_inside_workspace_ok(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    res = run_command(
        [PY, "-c", "import os; print(os.getcwd())"],
        cwd=str(sub),
        settings=s,
    )
    assert res.exit_code == 0
    assert Path(res.stdout.strip()).resolve() == sub.resolve()


def test_cwd_escape_rejected(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(WorkspaceEscapeError):
        run_command([PY, "-c", "print(1)"], cwd=str(tmp_path.parent), settings=s)


def test_empty_cmd_rejected(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(ValueError):
        run_command([], settings=s)


def test_zero_timeout_rejected(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(ValueError):
        run_command([PY, "-c", "pass"], timeout=0, settings=s)


def test_missing_executable_returns_127(tmp_path: Path) -> None:
    s = _settings(tmp_path, deny=[])
    res = run_command(["definitely-not-a-real-binary-xyz"], settings=s)
    assert res.exit_code == 127
    assert res.timed_out is False
    assert "not found" in res.stderr.lower()
