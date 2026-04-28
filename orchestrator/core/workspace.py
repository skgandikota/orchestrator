"""Workspace abstraction for the pipeline.

A :class:`Workspace` is a thin wrapper around a directory on disk that the
pipeline reads to build a :class:`~orchestrator.core.pipeline.bundle.WorkspaceSummary`.
The wrapper exists so that file-system access is mockable in unit tests --
production code goes through :class:`Workspace`, tests can substitute a
:class:`FakeWorkspace` (or any object with a compatible ``walk_files`` and
``read_gitignore`` surface).

The wrapper deliberately does **not** read file contents -- that is the job
of the coder/executor downstream. We only expose ``(relative_path, size_bytes)``
tuples plus the optional ``.gitignore`` text.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = ["FileStat", "Workspace", "WorkspaceLike"]


@dataclass(frozen=True)
class FileStat:
    """A file's ``(relative path, size in bytes)`` tuple."""

    path: str
    size_bytes: int


class WorkspaceLike(Protocol):
    """Structural type satisfied by both :class:`Workspace` and test fakes."""

    @property
    def root(self) -> str: ...

    def walk_files(self) -> Iterator[FileStat]: ...

    def read_gitignore(self) -> str | None: ...


class Workspace:
    """Disk-backed workspace.

    Args:
        root: Directory on disk to summarise. Need not exist; a missing root
            yields an empty file list.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> str:
        return str(self._root)

    def walk_files(self) -> Iterator[FileStat]:
        """Yield every regular file beneath the root with its size in bytes.

        Symlinks and non-regular files are skipped. Files we cannot stat
        (permission errors, races) are silently skipped -- the workspace
        summary is best-effort.
        """

        root = self._root
        if not root.is_dir():
            return
        for entry in root.rglob("*"):
            try:
                if not entry.is_file() or entry.is_symlink():
                    continue
                size = entry.stat().st_size
            except OSError:
                continue
            try:
                rel = entry.relative_to(root).as_posix()
            except ValueError:
                continue
            yield FileStat(path=rel, size_bytes=size)

    def read_gitignore(self) -> str | None:
        """Return the workspace's ``.gitignore`` text, or ``None`` if absent."""

        gi = self._root / ".gitignore"
        try:
            return gi.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, OSError):
            return None
