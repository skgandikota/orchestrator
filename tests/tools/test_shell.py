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


def test_non_string_cmd_entries_rejected(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(TypeError):
        run_command([PY, 123], settings=s)  # type: ignore[list-item]


def test_cwd_not_a_directory(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        run_command([PY, "-c", "pass"], cwd=str(f), settings=s)


def test_truncate_oversize_output_helper() -> None:
    """``_truncate`` clips at MAX_OUTPUT_BYTES (line 73)."""
    from orchestrator.tools.shell import MAX_OUTPUT_BYTES, _truncate

    big = b"x" * (MAX_OUTPUT_BYTES + 50)
    out = _truncate(big)
    assert "[truncated]" in out
    assert len(out.encode("utf-8")) <= MAX_OUTPUT_BYTES + 50


def test_kill_short_circuits_when_already_exited() -> None:
    """``_kill`` returns immediately when ``poll`` reports a finished proc."""
    from orchestrator.tools.shell import _kill

    class FakeProc:
        pid = 99999

        def poll(self) -> int:
            return 0

        def kill(self) -> None:  # pragma: no cover - must NOT be called
            raise AssertionError("kill should not be invoked")

    _kill(FakeProc())  # type: ignore[arg-type]


def test_kill_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors raised by the OS during kill are silently absorbed (lines 85-86)."""
    from orchestrator.tools import shell as shell_mod

    class FakeProc:
        pid = 99999

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            raise OSError("nope")

    # On posix os.killpg is invoked; on windows proc.kill is invoked. Monkey-patch
    # both so the test is platform-agnostic.
    monkeypatch.setattr(shell_mod.os, "name", "posix", raising=False)

    def boom(*_a, **_kw) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(shell_mod.os, "killpg", boom, raising=False)
    monkeypatch.setattr(shell_mod.os, "getpgid", lambda _pid: 1, raising=False)
    monkeypatch.setattr(shell_mod.signal, "SIGKILL", 9, raising=False)
    shell_mod._kill(FakeProc())  # type: ignore[arg-type]


def test_timeout_cleanup_secondary_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the cleanup ``communicate`` after kill also times out, we still return (lines 155-156)."""
    import subprocess as _sp

    from orchestrator.tools import shell as shell_mod

    class FakeProc:
        pid = 12345
        returncode = 0

        def __init__(self) -> None:
            self._calls = 0

        def communicate(self, timeout: float | None = None):
            self._calls += 1
            raise _sp.TimeoutExpired(cmd="x", timeout=timeout or 0)

        def poll(self) -> None:
            return None

        def kill(self) -> None:  # pragma: no cover - swallowed
            return None

    def fake_popen(*_a, **_kw) -> FakeProc:
        return FakeProc()

    monkeypatch.setattr(shell_mod.subprocess, "Popen", fake_popen)
    s = _settings(tmp_path)
    res = run_command([PY, "-c", "pass"], timeout=0.1, settings=s)
    assert res.timed_out is True
    assert res.exit_code == -1
    assert res.stdout == ""
    assert res.stderr == ""
