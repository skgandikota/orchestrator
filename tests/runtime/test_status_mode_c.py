"""Tests for :mod:`coracle.runtime.status_c` (status mode C)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from coracle.runtime.status import RamReading, Snapshot
from coracle.runtime.status_c import (
    StatusCCoordinator,
    SynthesisRequest,
    SynthesisResult,
    status_c,
)


def _ram() -> RamReading:
    return RamReading(used_mb=1.0, available_mb=2.0, total_mb=3.0)


@dataclass
class _FakeJob:
    id: str = "j1"
    status: Any = "running"
    steps: list[Any] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)
    model: str | None = "gpt-x"
    total_steps: int | None = 2
    started_at: float | None = None


class _FakeReasoning:
    def __init__(self, text: str = "narrative") -> None:
        self.text = text
        self.calls: list[tuple[Any, Snapshot]] = []

    def synthesize(self, job: Any, snap: Snapshot) -> str:
        self.calls.append((job, snap))
        return f"{self.text}:{snap.job_id}"


def _ids() -> Any:
    counter = iter(("sid-1", "sid-2", "sid-3", "sid-4"))
    return lambda: next(counter)


def _make_coord(text: str = "narrative") -> tuple[StatusCCoordinator, _FakeReasoning]:
    rm = _FakeReasoning(text)
    return StatusCCoordinator(reasoning_model=rm, id_factory=_ids()), rm


# --- public contract -------------------------------------------------------


def test_status_c_returns_immediately_with_queued_payload() -> None:
    coord, _ = _make_coord()
    job = _FakeJob(steps=[{"name": "plan"}])
    out = status_c(job, coord, ram_sampler=_ram, now=lambda: 1.0)

    assert out["status"] == "queued"
    assert out["synthesis_id"] == "sid-1"
    placeholder = out["placeholder"]
    assert placeholder["job_id"] == "j1"
    assert placeholder["phase"] == "running"
    assert placeholder["current_step"] == "plan"
    # Reasoning model not invoked yet.
    assert coord.pending_for("j1") is not None


def test_status_c_returns_in_under_200ms_even_under_load() -> None:
    coord, _ = _make_coord()
    job = _FakeJob(steps=[{"name": f"s{i}"} for i in range(50)], total_steps=100)
    t0 = time.perf_counter()
    out = status_c(job, coord, ram_sampler=_ram, now=lambda: 0.0)
    elapsed = (time.perf_counter() - t0) * 1000
    assert out["status"] == "queued"
    assert elapsed < 200.0


# --- queueing & coalescing -------------------------------------------------


def test_request_status_synthesis_enqueues_and_persists_record() -> None:
    coord, _ = _make_coord()
    snap = Snapshot("j1", "running", None, 0, None, 0.0, None, 0.0, 0.0, 0.0, None, 0.0)
    req = coord.request_status_synthesis("j1", snap)

    assert isinstance(req, SynthesisRequest)
    assert req.synthesis_id == "sid-1"
    assert len(coord) == 1
    record = coord.get_record("sid-1")
    assert record is not None
    assert record.status == "queued"
    assert record.placeholder is snap
    assert list(coord.iter_pending()) == [req]


def test_coalesce_returns_existing_request_for_same_job() -> None:
    coord, _ = _make_coord()
    snap = Snapshot("j1", "running", None, 0, None, 0.0, None, 0.0, 0.0, 0.0, None, 0.0)
    first = coord.request_status_synthesis("j1", snap)
    second = coord.request_status_synthesis("j1", snap)

    assert first is second
    assert len(coord) == 1
    # status_c on same job also coalesces
    job = _FakeJob(id="j1")
    out = status_c(job, coord, ram_sampler=_ram, now=lambda: 0.0)
    assert out["synthesis_id"] == first.synthesis_id
    assert len(coord) == 1


def test_distinct_jobs_get_distinct_synthesis_ids() -> None:
    coord, _ = _make_coord()
    out1 = status_c(_FakeJob(id="a"), coord, ram_sampler=_ram, now=lambda: 0.0)
    out2 = status_c(_FakeJob(id="b"), coord, ram_sampler=_ram, now=lambda: 0.0)
    assert out1["synthesis_id"] != out2["synthesis_id"]
    assert len(coord) == 2


def test_get_record_returns_none_for_unknown_id() -> None:
    coord, _ = _make_coord()
    assert coord.get_record("nope") is None
    assert coord.pending_for("ghost") is None


# --- drain / SSE -----------------------------------------------------------


def test_drain_runs_reasoning_emits_sse_and_persists_result() -> None:
    coord, rm = _make_coord("syn")
    job = _FakeJob(id="j1", steps=[{"name": "plan"}])
    out = status_c(job, coord, ram_sampler=_ram, now=lambda: 0.0)
    sid = out["synthesis_id"]

    received: list[tuple[str, dict[str, Any]]] = []
    coord.subscribe("j1", lambda evt, payload: received.append((evt, payload)))

    results = coord.drain_post_checkpoint_hooks(lambda jid: job if jid == "j1" else None)

    assert results == [SynthesisResult(job_id="j1", synthesis_id=sid, text="syn:j1")]
    assert len(rm.calls) == 1
    record = coord.get_record(sid)
    assert record is not None
    assert record.status == "done"
    assert record.text == "syn:j1"
    assert len(coord) == 0
    assert received == [
        ("status_synthesis", {"job_id": "j1", "synthesis_id": sid, "text": "syn:j1"}),
    ]


def test_drain_marks_failed_and_emits_event_when_job_missing() -> None:
    coord, rm = _make_coord()
    snap = Snapshot("ghost", "running", None, 0, None, 0.0, None, 0.0, 0.0, 0.0, None, 0.0)
    req = coord.request_status_synthesis("ghost", snap)

    received: list[tuple[str, dict[str, Any]]] = []
    coord.subscribe("ghost", lambda evt, payload: received.append((evt, payload)))

    def lookup(_: str) -> Any:
        raise KeyError("ghost")

    results = coord.drain_post_checkpoint_hooks(lookup)

    assert results == []
    assert rm.calls == []
    record = coord.get_record(req.synthesis_id)
    assert record is not None
    assert record.status == "failed"
    assert received[0][0] == "status_synthesis_failed"
    assert received[0][1]["reason"] == "job_not_found"


def test_drain_with_returning_none_also_marks_failed() -> None:
    coord, rm = _make_coord()
    snap = Snapshot("j1", "running", None, 0, None, 0.0, None, 0.0, 0.0, 0.0, None, 0.0)
    coord.request_status_synthesis("j1", snap)
    results = coord.drain_post_checkpoint_hooks(lambda _jid: None)
    assert results == []
    assert rm.calls == []


def test_drain_is_idempotent_when_queue_empty() -> None:
    coord, _ = _make_coord()
    assert coord.drain_post_checkpoint_hooks(lambda _: _FakeJob()) == []


def test_unsubscribe_stops_delivery_and_is_safe_to_call_twice() -> None:
    coord, _ = _make_coord()
    job = _FakeJob(id="j1")
    received: list[tuple[str, dict[str, Any]]] = []
    unsub = coord.subscribe("j1", lambda evt, p: received.append((evt, p)))
    unsub()
    unsub()  # second call is a no-op
    status_c(job, coord, ram_sampler=_ram, now=lambda: 0.0)
    coord.drain_post_checkpoint_hooks(lambda _: job)
    assert received == []


def test_unsubscribe_unknown_sink_is_noop() -> None:
    coord, _ = _make_coord()

    def sink(_evt: str, _p: dict[str, Any]) -> None:
        return None

    unsub = coord.subscribe("j1", sink)
    # Manually clear subscribers then call unsub: should not raise.
    coord._subscribers.clear()
    unsub()


def test_multiple_subscribers_all_notified() -> None:
    coord, _ = _make_coord()
    job = _FakeJob(id="j1")
    a: list[Any] = []
    b: list[Any] = []
    coord.subscribe("j1", lambda evt, p: a.append((evt, p)))
    coord.subscribe("j1", lambda evt, p: b.append((evt, p)))
    status_c(job, coord, ram_sampler=_ram, now=lambda: 0.0)
    coord.drain_post_checkpoint_hooks(lambda _: job)
    assert len(a) == 1 and len(b) == 1


def test_drain_processes_multiple_jobs_in_fifo_order() -> None:
    coord, rm = _make_coord("n")
    job_a = _FakeJob(id="a")
    job_b = _FakeJob(id="b")
    status_c(job_a, coord, ram_sampler=_ram, now=lambda: 0.0)
    status_c(job_b, coord, ram_sampler=_ram, now=lambda: 0.0)
    lookup = {"a": job_a, "b": job_b}
    results = coord.drain_post_checkpoint_hooks(lambda jid: lookup[jid])
    assert [r.job_id for r in results] == ["a", "b"]
    assert [c[0].id for c in rm.calls] == ["a", "b"]


# --- defaults --------------------------------------------------------------


def test_default_id_factory_produces_unique_hex_ids() -> None:
    coord = StatusCCoordinator(reasoning_model=_FakeReasoning())
    snap = Snapshot("j1", "running", None, 0, None, 0.0, None, 0.0, 0.0, 0.0, None, 0.0)
    snap2 = Snapshot("j2", "running", None, 0, None, 0.0, None, 0.0, 0.0, 0.0, None, 0.0)
    a = coord.request_status_synthesis("j1", snap)
    b = coord.request_status_synthesis("j2", snap2)
    assert a.synthesis_id != b.synthesis_id
    assert len(a.synthesis_id) == 32  # uuid4().hex
    assert len(b.synthesis_id) == 32


def test_status_c_uses_default_clock_and_ram_sampler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coracle.runtime import status as status_mod

    class _VM:
        used = 1024 * 1024 * 10
        available = 1024 * 1024 * 20
        total = 1024 * 1024 * 30

    monkeypatch.setattr(status_mod.psutil, "virtual_memory", lambda: _VM())
    monkeypatch.setattr(status_mod.time, "time", lambda: 42.0)

    coord, _ = _make_coord()
    out = status_c(_FakeJob(id="j1"), coord)
    assert out["placeholder"]["captured_at"] == 42.0
    assert out["placeholder"]["ram_total_mb"] == 30.0
