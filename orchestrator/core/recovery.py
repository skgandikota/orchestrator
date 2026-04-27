"""Crash recovery for in-flight jobs.

On startup, the orchestrator scans the durable job store for jobs that were
``running`` or ``pending_continue`` when the previous process exited. Each such
job is marked ``recoverable`` with an ``interrupted_at`` timestamp and a single
``RecoveryEvent`` is emitted so operators can choose to ``resume`` or
``cancel`` it.

This module deliberately depends only on a ``StateStore`` :class:`Protocol`
rather than the concrete SQLite-backed implementation in ``core.state`` (which
lands with #32 / #67). The full integration is wired up in a follow-up; the
contract here is what that integration must satisfy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

__all__ = [
    "IN_FLIGHT_STATUSES",
    "Job",
    "RecoveryEvent",
    "RecoveryReport",
    "StateStore",
    "cancel",
    "recover_jobs",
    "resume",
]

IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"running", "pending_continue"})


@dataclass(frozen=True)
class Job:
    """Minimal job projection consumed by the recovery pass."""

    id: str
    status: str
    interrupted_at: datetime | None = None


@dataclass(frozen=True)
class RecoveryEvent:
    """Structured event emitted once per job marked ``recoverable``."""

    job_id: str
    previous_status: str
    interrupted_at: datetime
    kind: str = "job.recoverable"


@dataclass
class RecoveryReport:
    scanned: int = 0
    recovered: list[str] = field(default_factory=list)
    events: list[RecoveryEvent] = field(default_factory=list)


class StateStore(Protocol):
    """Contract the persistent state layer (#32) must satisfy."""

    def list_in_flight(self) -> Iterable[Job]: ...

    def mark_interrupted(self, job_id: str) -> None: ...

    def list_recoverable(self) -> Iterable[Job]: ...

    def resume(self, job_id: str) -> None: ...

    def cancel(self, job_id: str) -> None: ...


EmitFn = Callable[[RecoveryEvent], None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def recover_jobs(
    state: StateStore,
    emit: EmitFn | None = None,
    *,
    now: Callable[[], datetime] = _utcnow,
) -> RecoveryReport:
    """Mark every in-flight job as ``recoverable`` and emit one event each.

    The pass is idempotent: ``list_in_flight`` must only return jobs whose
    status is in :data:`IN_FLIGHT_STATUSES`, so a second invocation -- after
    the first transitioned them to ``recoverable`` -- finds nothing to do and
    emits no further events.
    """

    report = RecoveryReport()
    for job in state.list_in_flight():
        if job.status not in IN_FLIGHT_STATUSES:
            continue
        report.scanned += 1
        state.mark_interrupted(job.id)
        event = RecoveryEvent(
            job_id=job.id,
            previous_status=job.status,
            interrupted_at=now(),
        )
        report.recovered.append(job.id)
        report.events.append(event)
        if emit is not None:
            emit(event)
    return report


def resume(state: StateStore, job_id: str) -> None:
    """Resume a previously-recoverable job via the state store."""

    state.resume(job_id)


def cancel(state: StateStore, job_id: str) -> None:
    """Cancel a previously-recoverable job via the state store."""

    state.cancel(job_id)
