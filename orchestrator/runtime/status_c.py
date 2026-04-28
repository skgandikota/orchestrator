"""Status mode C: queued reasoning synthesis at next checkpoint.

Mode C answers "what's really going on?" by asking the resident reasoning
model to synthesize a deeper narrative about a job's state. Per the
architectural rule that *status queries must not require an LLM by
default*, the public entry point :func:`status_c` returns immediately
with the cheap mode-A :class:`~orchestrator.runtime.status.Snapshot`
payload as a placeholder plus a ``synthesis_id`` correlator. The
synthesized narrative is produced later, only at a safe swap point
between coder steps, and streamed to subscribers via the SSE sink.

The module is intentionally self-contained so it can land in parallel
with other mode-B work that touches the scheduler. The
:class:`StatusCCoordinator` exposes a small surface (queue, drain hook,
subscribe) that the scheduler / HTTP layer can wire in without further
coupling.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from orchestrator.runtime.status import RamReading, Snapshot, snapshot

__all__ = [
    "ReasoningModel",
    "StatusCCoordinator",
    "SynthesisRecord",
    "SynthesisRequest",
    "SynthesisResult",
    "status_c",
]


class ReasoningModel(Protocol):
    """Minimal protocol the resident reasoning model must satisfy."""

    def synthesize(self, job: Any, snap: Snapshot) -> str:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class SynthesisRequest:
    """A queued request to synthesize a status narrative for a job."""

    job_id: str
    synthesis_id: str
    placeholder: Snapshot


@dataclass(frozen=True)
class SynthesisResult:
    """The deferred response body produced by Mode C."""

    job_id: str
    synthesis_id: str
    text: str


@dataclass
class SynthesisRecord:
    """Persisted state for a mode-C synthesis lifecycle."""

    job_id: str
    synthesis_id: str
    status: str = "queued"
    placeholder: Snapshot | None = None
    text: str | None = None


# Type alias for an SSE-style sink: receives ``(event_type, payload)``.
SseSink = Callable[[str, dict[str, Any]], None]


@dataclass
class _PendingEntry:
    request: SynthesisRequest
    record: SynthesisRecord


@dataclass
class StatusCCoordinator:
    """Owns the synthesis queue, persistence, and SSE fan-out for mode C.

    The coordinator is thread-safe enough for the orchestrator's
    single-writer scheduler loop: enqueueing, draining, and subscribing
    are guarded by a single lock. SSE delivery happens *outside* the
    lock so a slow subscriber cannot stall the scheduler.
    """

    reasoning_model: ReasoningModel
    id_factory: Callable[[], str] = field(default=lambda: uuid.uuid4().hex)
    _pending: OrderedDict[str, _PendingEntry] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _records: dict[str, SynthesisRecord] = field(default_factory=dict, init=False, repr=False)
    _subscribers: dict[str, list[SseSink]] = field(
        default_factory=lambda: defaultdict(list), init=False, repr=False
    )
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    # ---- enqueue / coalesce -------------------------------------------------

    def request_status_synthesis(self, job_id: str, placeholder: Snapshot) -> SynthesisRequest:
        """Enqueue a synthesis request, coalescing duplicates per job.

        If a synthesis is already pending for ``job_id``, the existing
        :class:`SynthesisRequest` is returned unchanged so its
        ``synthesis_id`` can be re-used by the caller.
        """
        with self._lock:
            existing = self._pending.get(job_id)
            if existing is not None:
                return existing.request
            req = SynthesisRequest(
                job_id=job_id,
                synthesis_id=self.id_factory(),
                placeholder=placeholder,
            )
            record = SynthesisRecord(
                job_id=job_id,
                synthesis_id=req.synthesis_id,
                status="queued",
                placeholder=placeholder,
            )
            self._pending[job_id] = _PendingEntry(request=req, record=record)
            self._records[req.synthesis_id] = record
            return req

    def pending_for(self, job_id: str) -> SynthesisRequest | None:
        """Return the queued request for ``job_id`` if one is pending."""
        with self._lock:
            entry = self._pending.get(job_id)
            return entry.request if entry is not None else None

    def get_record(self, synthesis_id: str) -> SynthesisRecord | None:
        """Return the persisted record for ``synthesis_id`` if any."""
        with self._lock:
            return self._records.get(synthesis_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)

    # ---- SSE fan-out --------------------------------------------------------

    def subscribe(self, job_id: str, sink: SseSink) -> Callable[[], None]:
        """Register ``sink`` for events on ``job_id`` and return an unsubscribe."""
        with self._lock:
            self._subscribers[job_id].append(sink)

        def _unsubscribe() -> None:
            with self._lock:
                sinks = self._subscribers.get(job_id)
                if not sinks:
                    return
                try:
                    sinks.remove(sink)
                except ValueError:
                    return
                if not sinks:
                    del self._subscribers[job_id]

        return _unsubscribe

    def _emit(self, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            sinks = list(self._subscribers.get(job_id, ()))
        for sink in sinks:
            sink(event_type, payload)

    # ---- drain hook ---------------------------------------------------------

    def drain_post_checkpoint_hooks(
        self, job_lookup: Callable[[str], Any]
    ) -> list[SynthesisResult]:
        """Drain all queued synthesis requests at a safe swap point.

        Called by the scheduler *between* coder steps. For each pending
        request, the resident reasoning model is invoked, the record is
        marked ``done``, and a ``status_synthesis`` SSE event is emitted
        on the job's stream. Requests whose job can no longer be
        resolved are marked ``failed`` and skipped.
        """
        with self._lock:
            drained = list(self._pending.values())
            self._pending.clear()

        results: list[SynthesisResult] = []
        for entry in drained:
            req = entry.request
            record = entry.record
            try:
                job = job_lookup(req.job_id)
            except KeyError:
                job = None
            if job is None:
                with self._lock:
                    record.status = "failed"
                self._emit(
                    req.job_id,
                    "status_synthesis_failed",
                    {
                        "job_id": req.job_id,
                        "synthesis_id": req.synthesis_id,
                        "reason": "job_not_found",
                    },
                )
                continue

            text = self.reasoning_model.synthesize(job, req.placeholder)
            with self._lock:
                record.status = "done"
                record.text = text
            result = SynthesisResult(
                job_id=req.job_id,
                synthesis_id=req.synthesis_id,
                text=text,
            )
            results.append(result)
            self._emit(
                req.job_id,
                "status_synthesis",
                {
                    "job_id": req.job_id,
                    "synthesis_id": req.synthesis_id,
                    "text": text,
                },
            )
        return results

    # ---- iter (debug / introspection) --------------------------------------

    def iter_pending(self) -> Iterator[SynthesisRequest]:
        with self._lock:
            return iter([e.request for e in self._pending.values()])


def status_c(
    job: Any,
    coordinator: StatusCCoordinator,
    *,
    ram_sampler: Callable[[], RamReading] | None = None,
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Mode C entry point: instant placeholder + queued synthesis.

    Returns the mode-A snapshot as ``placeholder`` and a correlator
    ``synthesis_id``. The synthesized narrative is produced
    asynchronously by ``coordinator`` at the scheduler's next safe swap
    point and streamed to subscribers.
    """
    placeholder = snapshot(job, ram_sampler=ram_sampler, now=now)
    request = coordinator.request_status_synthesis(placeholder.job_id, placeholder)
    return {
        "status": "queued",
        "placeholder": placeholder.to_dict(),
        "synthesis_id": request.synthesis_id,
    }
