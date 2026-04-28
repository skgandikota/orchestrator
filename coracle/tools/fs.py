"""Sandboxed filesystem tool.

All operations resolve against ``settings.tools.fs.workspace_root`` and refuse
any path that escapes the workspace (including via symlinks).
"""

from __future__ import annotations

from pathlib import Path

from coracle.config.settings import Settings, load_settings

from ._sandbox import WorkspaceEscapeError, resolve_in_workspace

__all__ = [
    "WorkspaceEscapeError",
    "delete_file",
    "list_dir",
    "read_file",
    "write_file",
]


def _workspace_root(settings: Settings | None) -> Path:
    s = settings if settings is not None else load_settings()
    return Path(s.tools.fs.workspace_root).expanduser().resolve(strict=False)


def read_file(path: str | Path, *, settings: Settings | None = None) -> str:
    """Return the UTF-8 contents of ``path`` inside the workspace."""
    root = _workspace_root(settings)
    target = resolve_in_workspace(path, root)
    if not target.is_file():
        raise FileNotFoundError(f"No such file: {target!s}")
    return target.read_text(encoding="utf-8")


def write_file(path: str | Path, content: str, *, settings: Settings | None = None) -> None:
    """Write ``content`` (UTF-8) to ``path`` inside the workspace.

    Parent directories are created as needed; existing files are overwritten.
    """
    root = _workspace_root(settings)
    target = resolve_in_workspace(path, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def list_dir(path: str | Path, *, settings: Settings | None = None) -> list[str]:
    """Return a sorted list of entry names directly inside ``path``."""
    root = _workspace_root(settings)
    target = resolve_in_workspace(path, root)
    if not target.is_dir():
        raise NotADirectoryError(f"Not a directory: {target!s}")
    return sorted(p.name for p in target.iterdir())


def delete_file(path: str | Path, *, settings: Settings | None = None) -> None:
    """Delete the file at ``path`` inside the workspace."""
    root = _workspace_root(settings)
    target = resolve_in_workspace(path, root)
    if target == root:
        raise WorkspaceEscapeError("Refusing to delete workspace root")
    if not target.exists():
        raise FileNotFoundError(f"No such file: {target!s}")
    if target.is_dir():
        raise IsADirectoryError(f"Refusing to delete directory via delete_file: {target!s}")
    target.unlink()
