"""Native FastAPI router for the orchestrator (issue #15).

Exposes explicit job IDs, mid-flight status queries with three modes,
SSE streams, and cooperative cancellation. Job submission is
non-blocking: the handler enqueues the job and returns its ID before
any pipeline work happens.

The module ships an in-process :class:`JobManager` that owns job
lifecycle, event fan-out, and cancellation. Production deployments
inject a manager wired to the real scheduler / state-store / recovery
modules via :func:`set_job_manager`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from orchestrator.runtime.status import snapshot as build_snapshot

__all__ = [
    "Job",
    "JobManager",
    "JobStatus",
    "PipelineEvent",
    "PipelineRunner",
    "get_job_manager",
    "router",
    "set_job_manager",
]


class JobStatus(str, Enum):  # noqa: UP042 - explicit str inheritance for FastAPI/JSON
    """Lifecycle states for a submitted job."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)


@dataclass
class PipelineEvent:
    """A single event emitted by the pipeline for a job."""

    kind: str
    data: dict[str, Any]
    ts: float = field(default_factory=time.time)


@dataclass
class Job:
    """In-memory job state. Mirrors what ``GET /jobs/{id}`` returns."""

    id: str
    user_msg: str
    model: str | None
    status: JobStatus = JobStatus.QUEUED
    job_class: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    final_output: str | None = None
    error: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    events: list[PipelineEvent] = field(default_factory=list)
    subscribers: list[asyncio.Queue[PipelineEvent | None]] = field(default_factory=list)

    def to_state(self) -> dict[str, Any]:
        """Serialise the public job state for ``GET /jobs/{id}``."""
        return {
            "id": self.id,
            "status": self.status.value,
            "class": self.job_class,
            "steps": list(self.steps),
            "artifacts": list(self.artifacts),
            "final_output": self.final_output,
            "error": self.error,
        }


PipelineRunner = Callable[[Job, "JobManager"], Awaitable[None]]


async def _default_runner(job: Job, mgr: JobManager) -> None:
    """Trivial pipeline used when no runner is injected.

    Emits ``started`` -> one ``step`` -> ``completed``. Real deployments
    inject a runner that drives the scheduler, state-store and recovery
    modules.
    """
    job.job_class = "default"
    await mgr.emit(job, "started", {"user_msg": job.user_msg, "model": job.model})
    if job.cancel_event.is_set():  # pragma: no cover - cooperative early exit
        return
    job.steps.append({"name": "echo", "ok": True})
    await mgr.emit(job, "step", {"name": "echo"})
    job.final_output = job.user_msg
    await mgr.emit(job, "completed", {})


class JobManager:
    """Owns job lifecycle, event fan-out, and cancellation."""

    def __init__(self, runner: PipelineRunner | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._runner: PipelineRunner = runner or _default_runner
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def get(self, job_id: str) -> Job:
        """Look up a job, raising 404 if unknown."""
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown job_id") from exc

    def submit(self, user_msg: str, model: str | None) -> Job:
        """Enqueue a new job and schedule its runner. Non-blocking."""
        job = Job(id=uuid.uuid4().hex, user_msg=user_msg, model=model)
        self._jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(self._run(job))
        return job

    async def _run(self, job: Job) -> None:
        job.status = JobStatus.RUNNING
        try:
            await self._runner(job, self)
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            await self.emit(job, "cancelled", {}, mark_terminal=True)
            raise
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = repr(exc)
            await self.emit(job, "failed", {"error": repr(exc)}, mark_terminal=True)
            return
        if job.status == JobStatus.RUNNING:
            job.status = JobStatus.COMPLETED
        await self._close_subscribers(job)

    async def emit(
        self,
        job: Job,
        kind: str,
        data: dict[str, Any],
        *,
        mark_terminal: bool = False,
    ) -> None:
        """Append a pipeline event and fan it out to live subscribers."""
        ev = PipelineEvent(kind=kind, data=data)
        job.events.append(ev)
        for q in list(job.subscribers):
            await q.put(ev)
        if mark_terminal:
            await self._close_subscribers(job)

    async def _close_subscribers(self, job: Job) -> None:
        for q in list(job.subscribers):
            await q.put(None)
        job.subscribers.clear()

    async def cancel(self, job: Job) -> None:
        """Cooperatively cancel a job. No-op if already terminal."""
        if job.status in _TERMINAL:
            return
        job.cancel_event.set()
        task = self._tasks.get(job.id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if job.status not in _TERMINAL:
            job.status = JobStatus.CANCELLED
            await self._close_subscribers(job)

    def status_payload(self, job: Job, mode: str) -> dict[str, Any]:
        """Return the payload for ``POST /jobs/{id}/status``.

        Modes mirror the three orchestrator status modes:

        * ``a`` - instant DB-template snapshot (cheapest).
        * ``b`` - 1.5B narrator gloss over current state.
        * ``c`` - full reasoning synthesis (heaviest).
        """
        if mode == "a":
            return {
                "mode": "a",
                "status": job.status.value,
                "steps_done": len(job.steps),
                "class": job.job_class,
            }
        if mode == "b":
            last = job.events[-1].kind if job.events else "idle"
            return {
                "mode": "b",
                "status": job.status.value,
                "narration": f"job {job.id} is {job.status.value}; last event={last}",
            }
        if mode == "c":
            return {
                "mode": "c",
                "status": job.status.value,
                "class": job.job_class,
                "steps": list(job.steps),
                "artifacts": list(job.artifacts),
                "final_output": job.final_output,
                "reasoning": [e.kind for e in job.events],
            }
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mode must be one of 'a', 'b', 'c'",
        )

    async def stream(self, job: Job) -> AsyncIterator[PipelineEvent]:
        """Yield pipeline events for a job until terminal."""
        q: asyncio.Queue[PipelineEvent | None] = asyncio.Queue()
        for ev in job.events:
            await q.put(ev)
        if job.status in _TERMINAL:
            await q.put(None)
        else:
            job.subscribers.append(q)
        try:
            while True:
                ev = await q.get()
                if ev is None:
                    return
                yield ev
        finally:
            if q in job.subscribers:
                job.subscribers.remove(q)


_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    """Return the process-wide :class:`JobManager`, creating one lazily."""
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager


def set_job_manager(mgr: JobManager | None) -> None:
    """Install (or clear) the process-wide :class:`JobManager`. Test hook."""
    global _manager
    _manager = mgr


router = APIRouter(tags=["jobs"])


class JobSubmit(BaseModel):
    """Body for ``POST /jobs``."""

    user_msg: str = Field(..., min_length=1)
    model: str | None = None


class StatusRequest(BaseModel):
    """Body for ``POST /jobs/{id}/status``."""

    mode: Literal["a", "b", "c"]


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def submit_job(payload: JobSubmit) -> dict[str, str]:
    """Enqueue a job and return its id immediately."""
    mgr = get_job_manager()
    job = mgr.submit(payload.user_msg, payload.model)
    return {"job_id": job.id}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    """Return the full job state plus a status mode A snapshot."""
    job = get_job_manager().get(job_id)
    state = job.to_state()
    state["snapshot"] = build_snapshot(job).to_dict()
    return state


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    """SSE stream of pipeline events for the job."""
    mgr = get_job_manager()
    job = mgr.get(job_id)

    async def gen() -> AsyncIterator[bytes]:
        async for ev in mgr.stream(job):
            payload = json.dumps(ev.data, default=str)
            yield f"event: {ev.kind}\ndata: {payload}\n\n".encode()

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/jobs/{job_id}/status")
async def job_status(job_id: str, payload: StatusRequest) -> dict[str, Any]:
    """Return a status payload in mode ``a``, ``b`` or ``c``."""
    mgr = get_job_manager()
    job = mgr.get(job_id)
    return mgr.status_payload(job, payload.mode)


@router.post("/jobs/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_job(job_id: str) -> dict[str, str]:
    """Cooperatively cancel a running job."""
    mgr = get_job_manager()
    job = mgr.get(job_id)
    await mgr.cancel(job)
    return {"job_id": job.id, "status": job.status.value}
