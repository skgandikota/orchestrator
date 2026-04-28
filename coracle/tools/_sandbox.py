"""Shared sandbox helpers for tool implementations.

Every filesystem-touching tool resolves user-supplied paths through
:func:`resolve_in_workspace` so that traversal (``../../etc/passwd``) and
symlink-escape attacks are rejected before any I/O happens.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["WorkspaceEscapeError", "resolve_in_workspace"]


class WorkspaceEscapeError(PermissionError):
    """Raised when a path resolves to a location outside the workspace root."""


def _is_within(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_in_workspace(path: str | Path, root: str | Path) -> Path:
    """Resolve ``path`` and ensure it lives inside ``root``.

    The returned path is fully resolved (symlinks followed) so callers receive
    a canonical location safe to hand to :mod:`os` / :mod:`pathlib`.

    Args:
        path: User-supplied path; may be relative (resolved against ``root``)
            or absolute.
        root: Workspace root. Must exist on disk so that :meth:`Path.resolve`
            can canonicalise it.

    Raises:
        WorkspaceEscapeError: If the resolved path is not equal to or a
            descendant of the resolved ``root`` -- including the case where a
            symlink target lies outside the workspace.
    """
    root_resolved = Path(root).expanduser().resolve(strict=False)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve(strict=False)
    if resolved != root_resolved and not _is_within(resolved, root_resolved):
        raise WorkspaceEscapeError(
            f"Path {path!r} resolves to {resolved!s} which is outside workspace {root_resolved!s}"
        )
    return resolved
