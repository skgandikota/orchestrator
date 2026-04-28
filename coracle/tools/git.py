"""Git tool — thin subprocess wrapper around the system ``git`` binary.

All commands run with ``cwd`` pinned to ``settings.tools.fs.workspace_root``
to inherit the shell tool's sandboxing posture. We deliberately avoid
``GitPython`` / ``pygit2`` to keep the dependency surface small and the
behaviour boringly predictable.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from coracle.config.settings import Settings, load_settings

__all__ = [
    "GitCommit",
    "GitError",
    "GitStatus",
    "branch",
    "checkout",
    "commit",
    "current_branch",
    "diff",
    "log",
    "status",
]


class GitError(RuntimeError):
    """Raised when a ``git`` invocation exits non-zero or input is rejected."""


class GitStatus(BaseModel):
    branch: str
    ahead: int = 0
    behind: int = 0
    staged: list[str] = Field(default_factory=list)
    unstaged: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    clean: bool = True


class GitCommit(BaseModel):
    sha: str
    author: str
    date: str
    subject: str


_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _settings() -> Settings:
    return load_settings()


def _workspace_root() -> Path:
    return Path(_settings().tools.fs.workspace_root)


def _run(args: list[str], *, cwd: Path | None = None) -> str:
    """Run ``git <args>`` and return stdout. Raise :class:`GitError` on failure."""
    root = cwd if cwd is not None else _workspace_root()
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        raise GitError(f"git {' '.join(args)} failed (exit {proc.returncode}): {stderr}")
    return proc.stdout


def _parse_porcelain_v2(text: str) -> GitStatus:
    branch_name = ""
    ahead = 0
    behind = 0
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    for raw in text.splitlines():
        if not raw:
            continue
        if raw.startswith("# branch.head "):
            branch_name = raw[len("# branch.head ") :].strip()
        elif raw.startswith("# branch.ab "):
            # Format: "# branch.ab +N -M"
            parts = raw[len("# branch.ab ") :].split()
            for p in parts:
                if p.startswith("+"):
                    ahead = int(p[1:])
                elif p.startswith("-"):
                    behind = int(p[1:])
        elif raw.startswith("1 ") or raw.startswith("2 "):
            # Tracked entries: "1 XY ... <path>" or rename "2 XY ... <path>\t<orig>"
            tokens = raw.split(maxsplit=8)
            if len(tokens) < 9:
                continue
            xy = tokens[1]
            path = tokens[8].split("\t", 1)[0]
            if xy[0] != ".":
                staged.append(path)
            if xy[1] != ".":
                unstaged.append(path)
        elif raw.startswith("? "):
            untracked.append(raw[2:])

    clean = not (staged or unstaged or untracked)
    return GitStatus(
        branch=branch_name,
        ahead=ahead,
        behind=behind,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        clean=clean,
    )


def status() -> GitStatus:
    """Return parsed ``git status --porcelain=v2 --branch``."""
    out = _run(["status", "--porcelain=v2", "--branch"])
    return _parse_porcelain_v2(out)


def diff(staged: bool = False, path: str | None = None) -> str:
    """Return ``git diff`` output. If *staged* is true, diffs the index."""
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path is not None:
        args.extend(["--", path])
    return _run(args)


def _has_staged_changes() -> bool:
    root = _workspace_root()
    proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(root),
        check=False,
        capture_output=True,
        text=True,
    )
    # exit 1 => differences present; 0 => none; anything else => error
    if proc.returncode not in (0, 1):
        stderr = (proc.stderr or "").strip()
        raise GitError(f"git diff --cached --quiet failed: {stderr}")
    return proc.returncode == 1


def commit(message: str, add_all: bool = False) -> str:
    """Create a commit and return its SHA.

    Refuses empty messages and refuses when nothing is staged (after the
    optional ``git add -A`` pass when *add_all* is true).
    """
    if not message or not message.strip():
        raise GitError("commit message must not be empty")
    if add_all:
        _run(["add", "-A"])
    if not _has_staged_changes():
        raise GitError("nothing staged to commit")
    _run(["commit", "-m", message])
    return _run(["rev-parse", "HEAD"]).strip()


def _validate_branch_name(name: str) -> None:
    if not name or any(c.isspace() for c in name):
        raise GitError(f"invalid branch name: {name!r}")
    if name.startswith("-") or ".." in name:
        raise GitError(f"invalid branch name: {name!r}")
    if not _BRANCH_RE.match(name):
        raise GitError(f"invalid branch name: {name!r}")


def branch(name: str) -> None:
    """Create a new branch (no checkout). Refuses unsafe names."""
    _validate_branch_name(name)
    _run(["branch", name])


def checkout(ref: str, create: bool = False) -> None:
    """Switch HEAD to *ref*, or create+switch when *create* is true.

    Refuses to operate on a dirty tree (no force flag in v1).
    """
    if not ref or any(c.isspace() for c in ref) or ref.startswith("-"):
        raise GitError(f"invalid ref: {ref!r}")
    if create:
        _validate_branch_name(ref)
    st = status()
    if not st.clean:
        raise GitError("refusing to checkout: working tree is dirty")
    args = ["checkout"]
    if create:
        args.append("-b")
    args.append(ref)
    _run(args)


def log(n: int = 10) -> list[GitCommit]:
    """Return the most recent *n* commits on HEAD."""
    if n <= 0:
        return []
    sep = "\x1f"
    fmt = f"%H{sep}%an{sep}%aI{sep}%s"
    out = _run(["log", f"-n{n}", f"--pretty=format:{fmt}"])
    commits: list[GitCommit] = []
    for line in out.splitlines():
        if not line:
            continue
        parts = line.split(sep)
        if len(parts) != 4:
            continue
        sha, author, date, subject = parts
        commits.append(GitCommit(sha=sha, author=author, date=date, subject=subject))
    return commits


def current_branch() -> str:
    """Return the symbolic name of the current branch (HEAD detached → ``HEAD``)."""
    return _run(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
