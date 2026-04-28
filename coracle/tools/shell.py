"""Sandboxed shell tool.

Executes external commands in list form (``shell=False``) constrained to the
configured workspace root, with allow/deny lists, output size caps, and a
hard timeout that kills the process group.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from pydantic import BaseModel, Field

from coracle.config.settings import Settings, load_settings

from ._sandbox import WorkspaceEscapeError, resolve_in_workspace

__all__ = [
    "CommandResult",
    "DeniedCommandError",
    "WorkspaceEscapeError",
    "run_command",
]

MAX_OUTPUT_BYTES = 1_048_576  # 1 MiB cap per stream


class DeniedCommandError(PermissionError):
    """Raised when a command is rejected by allow/deny policy."""


class CommandResult(BaseModel):
    """Outcome of a single :func:`run_command` invocation."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = Field(ge=0)
    timed_out: bool = False


def _executable_basename(arg: str) -> str:
    name = Path(arg).name
    # Strip Windows-style extensions so allow/deny lists stay portable.
    for ext in (".exe", ".bat", ".cmd", ".ps1", ".com"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    return name


def _check_policy(cmd: list[str], settings: Settings) -> None:
    if not cmd:
        raise ValueError("cmd must be a non-empty list of strings")
    if not all(isinstance(part, str) for part in cmd):
        raise TypeError("cmd must contain only str entries")
    exe = _executable_basename(cmd[0])
    deny = {d.lower() for d in settings.tools.shell.deny}
    allow = {a.lower() for a in settings.tools.shell.allow}
    if exe.lower() in deny:
        raise DeniedCommandError(f"Command {exe!r} is in deny-list")
    if allow and exe.lower() not in allow:
        raise DeniedCommandError(f"Command {exe!r} is not in allow-list")


def _truncate(buf: bytes) -> str:
    if len(buf) > MAX_OUTPUT_BYTES:
        buf = buf[:MAX_OUTPUT_BYTES] + b"\n[truncated]\n"
    return buf.decode("utf-8", errors="replace")


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_command(
    cmd: list[str],
    timeout: float = 30,
    cwd: str | None = None,
    *,
    settings: Settings | None = None,
) -> CommandResult:
    """Execute ``cmd`` and return a :class:`CommandResult`.

    Args:
        cmd: Argv list (``shell=False`` is enforced).
        timeout: Seconds before the process group is killed.
        cwd: Working directory; resolved against and constrained to
            ``settings.tools.fs.workspace_root``. Defaults to the workspace
            root when ``None``.
        settings: Optional pre-loaded settings (mainly for testing).

    Raises:
        DeniedCommandError: Command rejected by allow/deny policy.
        WorkspaceEscapeError: ``cwd`` resolves outside the workspace root.
        ValueError: ``cmd`` is empty or ``timeout`` is non-positive.
    """
    if timeout <= 0:
        raise ValueError("timeout must be > 0")

    s = settings if settings is not None else load_settings()
    _check_policy(cmd, s)

    workspace = Path(s.tools.fs.workspace_root).expanduser().resolve(strict=False)
    cwd_path = resolve_in_workspace(cwd if cwd is not None else workspace, workspace)
    if not cwd_path.is_dir():
        raise NotADirectoryError(f"cwd is not a directory: {cwd_path!s}")

    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": str(cwd_path),
        "shell": False,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[arg-type]
    except FileNotFoundError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResult(
            exit_code=127,
            stdout="",
            stderr=f"executable not found: {exc}",
            duration_ms=duration_ms,
            timed_out=False,
        )

    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
        try:
            stdout_b, stderr_b = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout_b, stderr_b = b"", b""
        exit_code = -1

    duration_ms = int((time.monotonic() - start) * 1000)
    return CommandResult(
        exit_code=exit_code,
        stdout=_truncate(stdout_b or b""),
        stderr=_truncate(stderr_b or b""),
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


# Re-export for callers that want the executable used to launch Python tests.
PYTHON_EXECUTABLE = sys.executable
