"""Tests for coracle.core.state."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from coracle.core.state import (
    Job,
    JobStatus,
    StateStore,
    Step,
    StepStatus,
)


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state.db")
    s.migrate()
    yield s
    s.close()


# ------------------------------------------------------------------- migrations


def test_migrate_creates_tables_and_pragmas(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    applied = s.migrate()
    assert "001_initial.sql" in applied

    fk = s.connection.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    journal = s.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal.lower() == "wal"

    tables = {
        row[0] for row in s.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"jobs", "steps", "messages", "artifacts", "schema_migrations"} <= tables

    indexes = {
        row[0] for row in s.connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"idx_jobs_status", "idx_steps_job_id", "idx_messages_job_id"} <= indexes
    s.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    s = StateStore(db)
    first = s.migrate()
    second = s.migrate()
    third = s.migrate()
    assert first  # at least one applied first time
    assert second == []
    assert third == []
    rows = s.connection.execute("SELECT version FROM schema_migrations").fetchall()
    versions = [r["version"] for r in rows]
    assert sorted(versions) == sorted(set(versions))  # no duplicates
    s.close()


def test_migrate_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    s1 = StateStore(db)
    s1.migrate()
    s1.close()

    s2 = StateStore(db)
    re_applied = s2.migrate()
    assert re_applied == []
    s2.close()


def test_db_path_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "state.db"
    s = StateStore(nested)
    s.migrate()
    assert nested.parent.is_dir()
    s.close()


# -------------------------------------------------------------------- jobs CRUD


def test_create_and_get_job_round_trip(store: StateStore) -> None:
    job_id = store.create_job("plan", {"goal": "demo", "n": 1})
    assert isinstance(job_id, str) and len(job_id) == 32

    job = store.get_job(job_id)
    assert isinstance(job, Job)
    assert job.id == job_id
    assert job.kind == "plan"
    assert job.status is JobStatus.PENDING
    assert job.request == {"goal": "demo", "n": 1}
    assert job.result is None
    assert job.error is None
    assert job.created_at == job.updated_at


def test_get_job_returns_none_for_missing(store: StateStore) -> None:
    assert store.get_job("does-not-exist") is None


def test_update_job_status_persists_and_bumps_updated_at(store: StateStore) -> None:
    job_id = store.create_job("plan", {})
    before = store.get_job(job_id)
    assert before is not None

    store.update_job_status(
        job_id,
        JobStatus.RUNNING,
    )
    mid = store.get_job(job_id)
    assert mid is not None
    assert mid.status is JobStatus.RUNNING
    assert mid.updated_at >= before.updated_at
    assert mid.created_at == before.created_at

    store.update_job_status(
        job_id,
        JobStatus.DONE,
        result={"answer": 42},
    )
    after = store.get_job(job_id)
    assert after is not None
    assert after.status is JobStatus.DONE
    assert after.result == {"answer": 42}
    assert after.error is None

    store.update_job_status(
        job_id,
        JobStatus.ERROR,
        error="boom",
    )
    err = store.get_job(job_id)
    assert err is not None
    assert err.status is JobStatus.ERROR
    assert err.error == "boom"
    # Result is preserved by COALESCE semantics.
    assert err.result == {"answer": 42}


def test_update_job_status_unknown_id_raises(store: StateStore) -> None:
    with pytest.raises(KeyError):
        store.update_job_status("missing", JobStatus.DONE)


# ------------------------------------------------------------------- steps CRUD


def test_step_lifecycle_append_finish_list_in_idx_order(store: StateStore) -> None:
    job_id = store.create_job("plan", {})
    a = store.append_step(job_id, "first", {"k": 1})
    b = store.append_step(job_id, "second", {"k": 2})
    c = store.append_step(job_id, "third", {"k": 3})
    assert (a, b, c) == (0, 1, 2)

    store.finish_step(job_id, b, StepStatus.DONE, output={"v": "B"})
    store.finish_step(job_id, a, StepStatus.ERROR, error="bad")
    # c left running.

    steps = store.list_steps(job_id)
    assert [s.idx for s in steps] == [0, 1, 2]
    assert all(isinstance(s, Step) for s in steps)

    s0, s1, s2 = steps
    assert s0.status is StepStatus.ERROR
    assert s0.error == "bad"
    assert s0.input == {"k": 1}
    assert s0.finished_at is not None

    assert s1.status is StepStatus.DONE
    assert s1.output == {"v": "B"}

    assert s2.status is StepStatus.RUNNING
    assert s2.finished_at is None


def test_finish_step_unknown_raises(store: StateStore) -> None:
    job_id = store.create_job("plan", {})
    with pytest.raises(KeyError):
        store.finish_step(job_id, 999, StepStatus.DONE)


def test_steps_unique_constraint_per_job(store: StateStore) -> None:
    job_id = store.create_job("plan", {})
    store.append_step(job_id, "s", {})
    with pytest.raises(sqlite3.IntegrityError), store.connection:
        store.connection.execute(
            "INSERT INTO steps(job_id, idx, name, status) VALUES (?, ?, ?, ?)",
            (job_id, 0, "dup", "running"),
        )


def test_list_steps_empty_for_unknown_job(store: StateStore) -> None:
    assert store.list_steps("nope") == []


# --------------------------------------------------------------- messages CRUD


def test_messages_round_trip(store: StateStore) -> None:
    job_id = store.create_job("chat", {})
    store.append_message(job_id, "user", "hi")
    store.append_message(job_id, "assistant", "hello")
    msgs = store.list_messages(job_id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["hi", "hello"]
    assert all(m.job_id == job_id for m in msgs)


# -------------------------------------------------------------- artifacts CRUD


def test_artifacts_round_trip(store: StateStore) -> None:
    job_id = store.create_job("build", {})
    store.add_artifact(job_id, "log", path="/tmp/run.log")
    store.add_artifact(job_id, "blob", content="hello-bytes")
    arts = store.list_artifacts(job_id)
    assert [a.kind for a in arts] == ["log", "blob"]
    assert arts[0].path == "/tmp/run.log"
    assert arts[0].content is None
    assert arts[1].content == "hello-bytes"
    assert arts[1].path is None


# ---------------------------------------------------- foreign-key enforcement


def test_foreign_key_blocks_orphan_step(store: StateStore) -> None:
    with pytest.raises(sqlite3.IntegrityError), store.connection:
        store.connection.execute(
            "INSERT INTO steps(job_id, idx, name, status) VALUES (?, ?, ?, ?)",
            ("ghost", 0, "orphan", "pending"),
        )


def test_foreign_key_blocks_orphan_message(store: StateStore) -> None:
    with pytest.raises(sqlite3.IntegrityError), store.connection:
        store.connection.execute(
            "INSERT INTO messages(job_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            ("ghost", "user", "hi", "2024-01-01T00:00:00+00:00"),
        )


def test_foreign_key_blocks_orphan_artifact(store: StateStore) -> None:
    with pytest.raises(sqlite3.IntegrityError), store.connection:
        store.connection.execute(
            "INSERT INTO artifacts(job_id, kind, created_at) VALUES (?, ?, ?)",
            ("ghost", "log", "2024-01-01T00:00:00+00:00"),
        )


def test_delete_job_cascades_to_children(store: StateStore) -> None:
    job_id = store.create_job("plan", {})
    store.append_step(job_id, "s", {})
    store.append_message(job_id, "user", "hi")
    store.add_artifact(job_id, "log", path="x.log")

    assert store.delete_job(job_id) is True
    assert store.get_job(job_id) is None
    assert store.list_steps(job_id) == []
    assert store.list_messages(job_id) == []
    assert store.list_artifacts(job_id) == []


def test_delete_job_returns_false_for_missing(store: StateStore) -> None:
    assert store.delete_job("nope") is False


# -------------------------------------------------------------- concurrency


def test_concurrent_writers_two_threads(tmp_path: Path) -> None:
    db = tmp_path / "concurrent.db"
    s = StateStore(db)
    s.migrate()
    job_id = s.create_job("plan", {})

    errors: list[BaseException] = []

    def worker(role: str, n: int) -> None:
        try:
            local = StateStore(db)
            for i in range(n):
                local.append_message(job_id, role, f"{role}-{i}")
            local.close()
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("a", 25))
    t2 = threading.Thread(target=worker, args=("b", 25))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    msgs = s.list_messages(job_id)
    assert len(msgs) == 50
    assert sum(1 for m in msgs if m.role == "a") == 25
    assert sum(1 for m in msgs if m.role == "b") == 25
    s.close()


# ------------------------------------------------------------- corrupt DB


def test_corrupt_db_raises(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is not a sqlite database, not even close" * 8)
    with pytest.raises(sqlite3.DatabaseError):
        s = StateStore(bad)
        try:
            s.migrate()
        finally:
            s.close()


# ----------------------------------------------------------- context manager


def test_context_manager_closes(tmp_path: Path) -> None:
    with StateStore(tmp_path / "ctx.db") as s:
        s.migrate()
        s.create_job("plan", {})
    with pytest.raises(sqlite3.ProgrammingError):
        s.connection.execute("SELECT 1")


def test_db_path_property(tmp_path: Path) -> None:
    p = tmp_path / "p.db"
    s = StateStore(p)
    assert s.db_path == p
    s.close()


# ------------------------------------------------------------ settings glue


def test_settings_exposes_state_section() -> None:
    from coracle.config.settings import Settings, StateSettings, load_settings

    s = load_settings()
    assert isinstance(s, Settings)
    assert isinstance(s.state, StateSettings)
    assert Path(s.state.db_path).name == "coracle.db"
