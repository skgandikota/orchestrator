"""Tests for crash recovery of in-flight jobs."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from orchestrator.core.recovery import (
    IN_FLIGHT_STATUSES,
    Job,
    RecoveryEvent,
    StateStore,
    cancel,
    recover_jobs,
    resume,
)


class FakeStateStore:
    """In-memory fake satisfying the :class:`StateStore` Protocol."""

    def __init__(self, jobs: Iterable[Job] = ()) -> None:
        self._jobs: dict[str, Job] = {j.id: j for j in jobs}
        self.resumed: list[str] = []
        self.cancelled: list[str] = []

    def list_in_flight(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status in IN_FLIGHT_STATUSES]

    def list_recoverable(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status == "recoverable"]

    def mark_interrupted(self, job_id: str) -> None:
        old = self._jobs[job_id]
        self._jobs[job_id] = Job(
            id=old.id,
            status="recoverable",
            interrupted_at=datetime.now(UTC),
        )

    def resume(self, job_id: str) -> None:
        self.resumed.append(job_id)
        old = self._jobs[job_id]
        self._jobs[job_id] = Job(id=old.id, status="running")

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)
        old = self._jobs[job_id]
        self._jobs[job_id] = Job(id=old.id, status="cancelled")


def _store_satisfies_protocol(store: object) -> StateStore:
    # Static-style assertion: assigning to the Protocol type proves the fake
    # implements every required method.
    s: StateStore = store  # type: ignore[assignment]
    return s


def test_empty_store_is_a_noop() -> None:
    store = FakeStateStore()
    events: list[RecoveryEvent] = []

    report = recover_jobs(_store_satisfies_protocol(store), events.append)

    assert report.scanned == 0
    assert report.recovered == []
    assert report.events == []
    assert events == []


def test_marks_every_in_flight_job_and_emits_one_event_each() -> None:
    jobs = [
        Job(id="a", status="running"),
        Job(id="b", status="pending_continue"),
        Job(id="c", status="done"),
        Job(id="d", status="running"),
    ]
    store = FakeStateStore(jobs)
    events: list[RecoveryEvent] = []

    report = recover_jobs(store, events.append)

    assert report.scanned == 3
    assert sorted(report.recovered) == ["a", "b", "d"]
    assert {e.job_id for e in events} == {"a", "b", "d"}
    assert len(events) == 3
    # exactly-one-event-per-job invariant
    assert len({e.job_id for e in events}) == len(events)
    assert {e.previous_status for e in events} == {"running", "pending_continue"}
    assert all(e.kind == "job.recoverable" for e in events)
    assert all(j.status == "recoverable" for j in store.list_recoverable())


def test_recovery_is_idempotent() -> None:
    store = FakeStateStore([Job(id="a", status="running")])
    events: list[RecoveryEvent] = []

    first = recover_jobs(store, events.append)
    second = recover_jobs(store, events.append)

    assert first.scanned == 1
    assert second.scanned == 0
    assert second.recovered == []
    assert second.events == []
    assert len(events) == 1


def test_recover_jobs_works_without_emit_callback() -> None:
    store = FakeStateStore([Job(id="x", status="running")])

    report = recover_jobs(store)

    assert report.recovered == ["x"]
    assert len(report.events) == 1


def test_recover_jobs_uses_injected_clock() -> None:
    fixed = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    store = FakeStateStore([Job(id="x", status="running")])

    report = recover_jobs(store, now=lambda: fixed)

    assert report.events[0].interrupted_at == fixed


def test_recover_jobs_skips_unexpected_statuses_defensively() -> None:
    # Guard against a future StateStore impl returning rows outside the
    # in-flight set: the recovery pass must filter them out and not mark them.
    class LeakyStore(FakeStateStore):
        def list_in_flight(self) -> list[Job]:
            return list(self._jobs.values())

    store = LeakyStore([Job(id="ok", status="running"), Job(id="other", status="done")])

    report = recover_jobs(store)

    assert report.recovered == ["ok"]


def test_resume_and_cancel_delegate_to_state_store() -> None:
    store = FakeStateStore(
        [
            Job(id="a", status="running"),
            Job(id="b", status="running"),
        ]
    )
    recover_jobs(store)

    resume(store, "a")
    cancel(store, "b")

    assert store.resumed == ["a"]
    assert store.cancelled == ["b"]


def test_in_flight_statuses_are_the_documented_set() -> None:
    assert frozenset({"running", "pending_continue"}) == IN_FLIGHT_STATUSES


def test_recovery_event_has_expected_fields() -> None:
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    event = RecoveryEvent(job_id="j", previous_status="running", interrupted_at=ts)
    assert event.kind == "job.recoverable"
    assert event.job_id == "j"
    assert event.previous_status == "running"
    assert event.interrupted_at == ts
    with pytest.raises(AttributeError):
        event.job_id = "other"  # type: ignore[misc]
