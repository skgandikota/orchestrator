"""Tests for the pipeline consolidate step."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.core.pipeline import (
    DEFAULT_MAX_FILES,
    EXCLUDED_DIRS,
    MAX_FILE_SIZE_BYTES,
    Bundle,
    ClassifyResult,
    JobStateSnapshot,
    Message,
    WorkspaceSummary,
    consolidate,
)
from orchestrator.core.workspace import FileStat, Workspace

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeWorkspace:
    """In-memory :class:`WorkspaceLike` for tests."""

    def __init__(
        self,
        root: str = "/tmp/fake-workspace",
        files: Iterable[FileStat] = (),
        gitignore: str | None = None,
    ) -> None:
        self._root = root
        self._files = list(files)
        self._gitignore = gitignore

    @property
    def root(self) -> str:
        return self._root

    def walk_files(self) -> Iterator[FileStat]:
        yield from self._files

    def read_gitignore(self) -> str | None:
        return self._gitignore


class FakeState:
    """In-memory :class:`PipelineState`."""

    def __init__(
        self,
        messages: Iterable[Message] = (),
        job_state: JobStateSnapshot | None = None,
    ) -> None:
        self._messages = list(messages)
        self._job_state = job_state or JobStateSnapshot(job_id="job-0", status="running")
        self.events: list[dict[str, object]] = []
        self.last_recent_messages_limit: int | None = None

    def recent_messages(self, job_id: str, limit: int) -> list[Message]:
        self.last_recent_messages_limit = limit
        return list(self._messages)

    def get_job_state(self, job_id: str) -> JobStateSnapshot:
        return self._job_state

    def append_pipeline_event(self, job_id: str, *, step: str, payload: dict[str, object]) -> None:
        self.events.append({"job_id": job_id, "step": step, "payload": payload})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify() -> ClassifyResult:
    return ClassifyResult(intent="code", confidence=0.9)


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content, created_at=datetime(2024, 1, 1, tzinfo=UTC))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_consolidate_returns_bundle_and_checkpoints_before_return() -> None:
    state = FakeState(
        messages=[_msg("user", "hi"), _msg("assistant", "hey")],
        job_state=JobStateSnapshot(job_id="j1", status="running", attempt=2),
    )
    ws = FakeWorkspace(files=[FileStat("a.txt", 10), FileStat("b.txt", 20)])

    bundle = consolidate(
        "j1",
        "do the thing",
        _classify(),
        state=state,
        workspace=ws,
    )

    assert isinstance(bundle, Bundle)
    assert bundle.job_id == "j1"
    assert bundle.user_msg == "do the thing"
    assert [m.content for m in bundle.recent_messages] == ["hi", "hey"]
    assert bundle.recent_job_state.attempt == 2
    assert bundle.classification.intent == "code"
    assert [f.path for f in bundle.workspace_summary.files] == ["a.txt", "b.txt"]
    assert bundle.workspace_summary.truncated is False

    assert len(state.events) == 1
    ev = state.events[0]
    assert ev["job_id"] == "j1"
    assert ev["step"] == "consolidate"
    payload = ev["payload"]
    assert isinstance(payload, dict)
    assert payload["user_msg"] == "do the thing"


def test_bundle_is_json_round_trippable() -> None:
    state = FakeState()
    ws = FakeWorkspace(files=[FileStat("a.txt", 1)])
    bundle = consolidate("j1", "msg", _classify(), state=state, workspace=ws)

    raw = bundle.model_dump_json()
    restored = Bundle.model_validate_json(raw)
    assert restored == bundle
    assert isinstance(json.loads(raw), dict)


def test_empty_workspace_yields_empty_summary() -> None:
    state = FakeState()
    ws = FakeWorkspace(files=[])

    bundle = consolidate("j1", "msg", _classify(), state=state, workspace=ws)

    assert isinstance(bundle.workspace_summary, WorkspaceSummary)
    assert bundle.workspace_summary.files == []
    assert bundle.workspace_summary.truncated is False


def test_large_files_are_excluded() -> None:
    state = FakeState()
    ws = FakeWorkspace(
        files=[
            FileStat("small.txt", 100),
            FileStat("huge.bin", MAX_FILE_SIZE_BYTES + 1),
            FileStat("at_limit.bin", MAX_FILE_SIZE_BYTES),
        ]
    )

    bundle = consolidate("j1", "msg", _classify(), state=state, workspace=ws)
    paths = {f.path for f in bundle.workspace_summary.files}
    assert paths == {"small.txt", "at_limit.bin"}


@pytest.mark.parametrize("excluded", sorted(EXCLUDED_DIRS))
def test_excluded_directories_are_filtered(excluded: str) -> None:
    state = FakeState()
    ws = FakeWorkspace(
        files=[
            FileStat("src/main.py", 10),
            FileStat(f"{excluded}/inside.txt", 10),
            FileStat(f"nested/{excluded}/deep.txt", 10),
        ]
    )

    bundle = consolidate("j1", "msg", _classify(), state=state, workspace=ws)
    paths = {f.path for f in bundle.workspace_summary.files}
    assert paths == {"src/main.py"}


def test_gitignore_is_honoured() -> None:
    state = FakeState()
    ws = FakeWorkspace(
        files=[
            FileStat("keep.py", 10),
            FileStat("build/out.bin", 10),
            FileStat("notes.log", 10),
        ],
        gitignore="build/\n*.log\n",
    )

    bundle = consolidate("j1", "msg", _classify(), state=state, workspace=ws)
    paths = {f.path for f in bundle.workspace_summary.files}
    assert paths == {"keep.py"}


def test_empty_gitignore_string_is_treated_as_absent() -> None:
    state = FakeState()
    ws = FakeWorkspace(files=[FileStat("a.txt", 1)], gitignore="")

    bundle = consolidate("j1", "msg", _classify(), state=state, workspace=ws)
    assert [f.path for f in bundle.workspace_summary.files] == ["a.txt"]


def test_history_truncated_to_limit_oldest_first() -> None:
    msgs = [_msg("user", f"m{i}") for i in range(10)]
    state = FakeState(messages=msgs)
    ws = FakeWorkspace()

    bundle = consolidate(
        "j1",
        "msg",
        _classify(),
        state=state,
        workspace=ws,
        recent_messages_limit=3,
    )
    assert [m.content for m in bundle.recent_messages] == ["m7", "m8", "m9"]
    assert state.last_recent_messages_limit == 3


def test_history_token_budget_drops_oldest() -> None:
    msgs = [_msg("user", "x" * 400) for _ in range(5)]  # ~100 tokens each
    state = FakeState(messages=msgs)
    ws = FakeWorkspace()

    bundle = consolidate(
        "j1",
        "msg",
        _classify(),
        state=state,
        workspace=ws,
        recent_messages_limit=10,
        history_token_budget=200,  # ~800 chars -> 2 messages
    )
    assert len(bundle.recent_messages) == 2


def test_history_limit_zero_returns_no_messages() -> None:
    state = FakeState(messages=[_msg("user", "hi")])
    ws = FakeWorkspace()

    bundle = consolidate(
        "j1",
        "msg",
        _classify(),
        state=state,
        workspace=ws,
        recent_messages_limit=0,
    )
    assert bundle.recent_messages == []


def test_max_files_cap_marks_truncated() -> None:
    state = FakeState()
    ws = FakeWorkspace(files=[FileStat(f"f{i}.txt", 1) for i in range(5)])

    bundle = consolidate(
        "j1",
        "msg",
        _classify(),
        state=state,
        workspace=ws,
        max_files=3,
    )
    assert len(bundle.workspace_summary.files) == 3
    assert bundle.workspace_summary.truncated is True


def test_default_max_files_constant() -> None:
    assert DEFAULT_MAX_FILES == 500


def test_empty_job_id_rejected() -> None:
    state = FakeState()
    ws = FakeWorkspace()
    with pytest.raises(ValueError, match="job_id"):
        consolidate("", "msg", _classify(), state=state, workspace=ws)


def test_empty_user_msg_rejected() -> None:
    state = FakeState()
    ws = FakeWorkspace()
    with pytest.raises(ValueError, match="user_msg"):
        consolidate("j1", "", _classify(), state=state, workspace=ws)


# ---------------------------------------------------------------------------
# Workspace (disk-backed) tests
# ---------------------------------------------------------------------------


def test_workspace_walk_excludes_symlinks_and_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    ws = Workspace(missing)
    assert list(ws.walk_files()) == []
    assert ws.read_gitignore() is None


def test_workspace_walk_yields_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world!")

    ws = Workspace(tmp_path)
    found = {(s.path, s.size_bytes) for s in ws.walk_files()}
    assert ("a.txt", 5) in found
    assert ("sub/b.txt", 6) in found
    assert ws.root == str(tmp_path)


def test_workspace_read_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\n")
    ws = Workspace(tmp_path)
    assert ws.read_gitignore() == "*.log\n"


def test_workspace_skips_unreadable_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    ws = Workspace(tmp_path)

    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if self.name == "b.txt":
            raise OSError("nope")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    paths = {s.path for s in ws.walk_files()}
    assert paths == {"a.txt"}


def test_workspace_integration_with_consolidate(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("print('hi')")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("nope")
    excluded_dir = tmp_path / "__pycache__"
    excluded_dir.mkdir()
    (excluded_dir / "x.pyc").write_text("bytes")

    state = FakeState()
    bundle = consolidate("j1", "msg", _classify(), state=state, workspace=Workspace(tmp_path))
    assert {f.path for f in bundle.workspace_summary.files} == {".gitignore", "keep.py"}
