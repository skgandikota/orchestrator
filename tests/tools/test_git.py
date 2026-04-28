"""Tests for ``coracle.tools.git``.

Each test creates a fresh temp git repo with an isolated local config so
the user's real repos and global git config are never touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coracle.tools import git as git_tool
from coracle.tools.git import GitError


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Initialise a fresh repo in *tmp_path* and pin the tool to it."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "--local", "user.email", "tester@example.com")
    _git(tmp_path, "config", "--local", "user.name", "Tester")
    _git(tmp_path, "config", "--local", "commit.gpgsign", "false")
    monkeypatch.setattr(git_tool, "_workspace_root", lambda: tmp_path)
    return tmp_path


def _seed_initial_commit(repo: Path) -> None:
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")


def test_status_clean(repo: Path) -> None:
    _seed_initial_commit(repo)
    st = git_tool.status()
    assert st.clean is True
    assert st.branch == "main"
    assert st.staged == st.unstaged == st.untracked == []


def test_status_dirty_tracks_staged_unstaged_untracked(repo: Path) -> None:
    _seed_initial_commit(repo)
    (repo / "README.md").write_text("hello world\n", encoding="utf-8")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")

    st = git_tool.status()
    assert st.clean is False
    assert "a.txt" in st.staged
    assert "README.md" in st.unstaged
    assert "b.txt" in st.untracked


def test_diff_staged_vs_unstaged(repo: Path) -> None:
    _seed_initial_commit(repo)
    (repo / "README.md").write_text("hello world\n", encoding="utf-8")
    unstaged = git_tool.diff()
    assert "hello world" in unstaged
    assert git_tool.diff(staged=True) == ""

    _git(repo, "add", "README.md")
    assert git_tool.diff() == ""
    staged = git_tool.diff(staged=True)
    assert "hello world" in staged


def test_commit_happy_path_returns_sha(repo: Path) -> None:
    _seed_initial_commit(repo)
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    sha = git_tool.commit("add new", add_all=True)
    assert len(sha) == 40
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert sha == head


def test_commit_empty_message_rejected(repo: Path) -> None:
    _seed_initial_commit(repo)
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x.txt")
    with pytest.raises(GitError, match="empty"):
        git_tool.commit("   ")


def test_commit_nothing_staged_rejected(repo: Path) -> None:
    _seed_initial_commit(repo)
    with pytest.raises(GitError, match="nothing staged"):
        git_tool.commit("noop")


@pytest.mark.parametrize("bad", ["bad name", "-leading", "with..dots", "weird?char"])
def test_branch_name_validation(repo: Path, bad: str) -> None:
    _seed_initial_commit(repo)
    with pytest.raises(GitError, match="invalid branch name"):
        git_tool.branch(bad)


def test_branch_create_and_current_branch(repo: Path) -> None:
    _seed_initial_commit(repo)
    git_tool.branch("feature/x")
    assert git_tool.current_branch() == "main"
    git_tool.checkout("feature/x")
    assert git_tool.current_branch() == "feature/x"


def test_checkout_nonexistent_ref_raises(repo: Path) -> None:
    _seed_initial_commit(repo)
    with pytest.raises(GitError):
        git_tool.checkout("does-not-exist")


def test_checkout_dirty_tree_refused(repo: Path) -> None:
    _seed_initial_commit(repo)
    git_tool.branch("feature/y")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitError, match="dirty"):
        git_tool.checkout("feature/y")


def test_log_limit_honored(repo: Path) -> None:
    _seed_initial_commit(repo)
    for i in range(4):
        (repo / f"f{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"c{i}")

    commits = git_tool.log(n=3)
    assert len(commits) == 3
    assert commits[0].subject == "c3"
    assert all(len(c.sha) == 40 for c in commits)
    assert commits[0].author == "Tester"


def test_log_zero_returns_empty(repo: Path) -> None:
    _seed_initial_commit(repo)
    assert git_tool.log(n=0) == []
    assert git_tool.log(n=-3) == []


def test_diff_with_path_argument(repo: Path) -> None:
    _seed_initial_commit(repo)
    (repo / "a.txt").write_text("aaa\n", encoding="utf-8")
    (repo / "b.txt").write_text("bbb\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add a/b")
    (repo / "a.txt").write_text("aaa changed\n", encoding="utf-8")
    (repo / "b.txt").write_text("bbb changed\n", encoding="utf-8")
    out = git_tool.diff(path="a.txt")
    assert "a.txt" in out
    assert "b.txt" not in out


def test_checkout_create_new_branch(repo: Path) -> None:
    _seed_initial_commit(repo)
    git_tool.checkout("feature/new", create=True)
    assert git_tool.current_branch() == "feature/new"


def test_checkout_invalid_ref_with_whitespace(repo: Path) -> None:
    _seed_initial_commit(repo)
    with pytest.raises(GitError, match="invalid ref"):
        git_tool.checkout("bad ref")


def test_checkout_create_validates_branch_name(repo: Path) -> None:
    _seed_initial_commit(repo)
    with pytest.raises(GitError, match="invalid branch name"):
        git_tool.checkout("weird?char", create=True)


def test_settings_and_workspace_root_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover ``_settings`` / ``_workspace_root`` (default code path)."""
    from coracle.config.settings import FsToolSettings, Settings, ToolsSettings

    fake = Settings(tools=ToolsSettings(fs=FsToolSettings(workspace_root="/tmp/ws-x")))
    monkeypatch.setattr(git_tool, "load_settings", lambda: fake)
    assert git_tool._settings().tools.fs.workspace_root == "/tmp/ws-x"
    assert git_tool._workspace_root() == Path("/tmp/ws-x")


def test_porcelain_parser_branch_ab_and_renames() -> None:
    """Hand-crafted porcelain v2 to exercise branch.ab + rename + short tokens."""
    sample = (
        "\n"
        "# branch.head main\n"
        "# branch.ab +3 -2\n"
        "1 M. N... 100644 100644 100644 abc abc README.md\n"
        "1 .M N... 100644 100644 100644 abc abc src/app.py\n"
        "1 short tokens\n"
        "? untracked.txt\n"
    )
    st = git_tool._parse_porcelain_v2(sample)
    assert st.branch == "main"
    assert st.ahead == 3
    assert st.behind == 2
    assert "README.md" in st.staged
    assert "src/app.py" in st.unstaged
    assert "untracked.txt" in st.untracked


def test_log_skips_blank_and_malformed_lines(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sep = "\x1f"
    real_run = git_tool._run

    def fake_run(args: list[str], *, cwd=None) -> str:
        if args and args[0] == "log":
            return f"\nshasha{sep}only{sep}two\n" + sep.join(["a", "b", "c", "d"]) + "\n"
        return real_run(args, cwd=cwd)

    monkeypatch.setattr(git_tool, "_run", fake_run)
    out = git_tool.log(n=5)
    assert len(out) == 1
    assert out[0].sha == "a"


def test_has_staged_changes_unexpected_exit_raises(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeProc:
        returncode = 2
        stdout = ""
        stderr = "explosion"

    def fake_run(*_a, **_kw):
        return FakeProc()

    _seed_initial_commit(repo)
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(git_tool.subprocess, "run", fake_run)
    with pytest.raises(GitError, match="diff --cached"):
        git_tool.commit("msg", add_all=False)
