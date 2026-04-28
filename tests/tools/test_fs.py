"""Tests for coracle.tools.fs."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coracle.config.settings import Settings
from coracle.tools import fs
from coracle.tools._sandbox import WorkspaceEscapeError


def _settings(root: Path) -> Settings:
    return Settings.model_validate({"tools": {"fs": {"workspace_root": str(root)}}})


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    fs.write_file("hello.txt", "hi there", settings=s)
    assert fs.read_file("hello.txt", settings=s) == "hi there"


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    fs.write_file("a/b/c/file.txt", "deep", settings=s)
    assert (tmp_path / "a" / "b" / "c" / "file.txt").read_text(encoding="utf-8") == "deep"


def test_list_dir_returns_sorted_names(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    fs.write_file("b.txt", "", settings=s)
    fs.write_file("a.txt", "", settings=s)
    fs.write_file("sub/inner.txt", "", settings=s)
    assert fs.list_dir(".", settings=s) == ["a.txt", "b.txt", "sub"]


def test_traversal_is_rejected(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(WorkspaceEscapeError):
        fs.read_file("../../../etc/passwd", settings=s)


def test_absolute_outside_workspace_rejected(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    with pytest.raises(WorkspaceEscapeError):
        fs.read_file(str(outside), settings=s)


def test_write_outside_workspace_rejected(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    target = tmp_path.parent / "evil.txt"
    with pytest.raises(WorkspaceEscapeError):
        fs.write_file(str(target), "x", settings=s)
    assert not target.exists()


@pytest.mark.skipif(
    sys.platform.startswith("win") and sys.version_info < (3, 12),
    reason="symlink creation requires admin/dev-mode on older Windows",
)
def test_symlink_escape_rejected(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("classified", encoding="utf-8")
    link = tmp_path / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    with pytest.raises(WorkspaceEscapeError):
        fs.read_file("escape/secret.txt", settings=s)


def test_read_missing_file(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(FileNotFoundError):
        fs.read_file("nope.txt", settings=s)


def test_delete_file(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    fs.write_file("gone.txt", "bye", settings=s)
    fs.delete_file("gone.txt", settings=s)
    assert not (tmp_path / "gone.txt").exists()


def test_delete_missing_file_raises(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(FileNotFoundError):
        fs.delete_file("ghost.txt", settings=s)


def test_delete_directory_refused(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    (tmp_path / "d").mkdir()
    with pytest.raises(IsADirectoryError):
        fs.delete_file("d", settings=s)


def test_list_dir_on_file_raises(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    fs.write_file("f.txt", "x", settings=s)
    with pytest.raises(NotADirectoryError):
        fs.list_dir("f.txt", settings=s)


def test_delete_workspace_root_refused(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(WorkspaceEscapeError, match="workspace root"):
        fs.delete_file(".", settings=s)
