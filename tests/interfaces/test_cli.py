"""Tests for the operator-facing recoverable-job CLI surface."""

from __future__ import annotations

from orchestrator.core.recovery import Job, recover_jobs
from orchestrator.interfaces.cli import cancel_job, list_recoverable, resume_job
from tests.core.test_recovery import FakeStateStore


def _seeded_store() -> FakeStateStore:
    store = FakeStateStore(
        [
            Job(id="a", status="running"),
            Job(id="b", status="pending_continue"),
        ]
    )
    recover_jobs(store)
    return store


def test_list_recoverable_returns_all_recoverable_jobs() -> None:
    store = _seeded_store()
    jobs = list_recoverable(store)
    assert sorted(j.id for j in jobs) == ["a", "b"]
    assert all(j.status == "recoverable" for j in jobs)


def test_resume_job_delegates_to_state_store() -> None:
    store = _seeded_store()
    resume_job(store, "a")
    assert store.resumed == ["a"]


def test_cancel_job_delegates_to_state_store() -> None:
    store = _seeded_store()
    cancel_job(store, "b")
    assert store.cancelled == ["b"]
