"""Tests for :mod:`orchestrator.runtime.status` (status mode A)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.tasks import (
    Job as ApiJob,
)
from orchestrator.api.tasks import (
    JobManager,
    PipelineEvent,
    set_job_manager,
)
from orchestrator.runtime import status as status_mod
from orchestrator.runtime.status import RamReading, Snapshot, snapshot


def _ram() -> RamReading:
    return RamReading(used_mb=2048.0, available_mb=6144.0, total_mb=8192.0)


@dataclass
class _Step:
    name: str


@dataclass
class _FakeJob:
    id: str = "j1"
    status: Any = "running"
    steps: list[Any] = field(default_factory=list)
    events: list[PipelineEvent] = field(default_factory=list)
    model: str | None = "gpt-x"
    total_steps: int | None = 4
    started_at: float | None = None


def test_snapshot_basic_with_dict_steps_and_events() -> None:
    job = _FakeJob(
        steps=[{"name": "plan"}, {"name": "code"}],
        events=[PipelineEvent(kind="started", data={}, ts=100.0)],
        total_steps=4,
    )
    snap = snapshot(job, ram_sampler=_ram, now=lambda: 110.0)

    assert isinstance(snap, Snapshot)
    assert snap.job_id == "j1"
    assert snap.phase == "running"
    assert snap.current_step == "code"
    assert snap.steps_done == 2
    assert snap.total_steps == 4
    assert snap.percent == 50.0
    # rate = 10s / 2 done = 5s/step; remaining=2 -> ETA 10s
    assert snap.eta_seconds == 10.0
    assert snap.ram_used_mb == 2048.0
    assert snap.ram_available_mb == 6144.0
    assert snap.ram_total_mb == 8192.0
    assert snap.model == "gpt-x"
    assert snap.captured_at == 110.0


def test_snapshot_to_dict_is_json_friendly() -> None:
    job = _FakeJob(steps=[], events=[], total_steps=None)
    d = snapshot(job, ram_sampler=_ram, now=lambda: 1.0).to_dict()
    assert set(d) == {
        "job_id",
        "phase",
        "current_step",
        "steps_done",
        "total_steps",
        "percent",
        "eta_seconds",
        "ram_used_mb",
        "ram_available_mb",
        "ram_total_mb",
        "model",
        "captured_at",
    }
    assert d["percent"] == 0.0
    assert d["eta_seconds"] is None
    assert d["current_step"] is None
    assert d["total_steps"] is None


def test_snapshot_phase_handles_enum_value_and_none() -> None:
    class _S:
        value = "queued"

    assert snapshot(_FakeJob(status=_S()), ram_sampler=_ram, now=lambda: 0.0).phase == "queued"
    assert snapshot(_FakeJob(status=None), ram_sampler=_ram, now=lambda: 0.0).phase == "unknown"


def test_snapshot_current_step_falls_back_to_event_kind() -> None:
    job = _FakeJob(
        steps=[],
        events=[
            PipelineEvent(kind="started", data={}, ts=10.0),
            PipelineEvent(kind="step", data={}, ts=20.0),
        ],
    )
    snap = snapshot(job, ram_sampler=_ram, now=lambda: 30.0)
    assert snap.current_step == "step"


def test_snapshot_step_object_with_name_attribute() -> None:
    job = _FakeJob(steps=[_Step(name="verify")], events=[])
    snap = snapshot(job, ram_sampler=_ram, now=lambda: 5.0)
    assert snap.current_step == "verify"


def test_snapshot_step_without_name_or_kind_returns_none() -> None:
    job = _FakeJob(
        steps=[{"ok": True}],  # no "name"
        events=[PipelineEvent(kind="", data={}, ts=1.0)],
    )
    snap = snapshot(job, ram_sampler=_ram, now=lambda: 2.0)
    assert snap.current_step is None


def test_snapshot_started_at_attribute_used_when_no_events() -> None:
    job = _FakeJob(
        steps=[{"name": "a"}, {"name": "b"}],
        events=[],
        total_steps=4,
        started_at=200.0,
    )
    snap = snapshot(job, ram_sampler=_ram, now=lambda: 220.0)
    # 20s elapsed, 2 done -> 10s/step, 2 remaining -> ETA 20s
    assert snap.eta_seconds == 20.0


def test_snapshot_eta_none_when_no_started_or_no_total_or_no_progress() -> None:
    base = _FakeJob(steps=[{"name": "a"}], total_steps=2, events=[])
    # no started_at, no events -> ETA None
    assert snapshot(base, ram_sampler=_ram, now=lambda: 10.0).eta_seconds is None
    # no total
    no_total = _FakeJob(
        steps=[{"name": "a"}],
        events=[PipelineEvent(kind="x", data={}, ts=1.0)],
        total_steps=None,
    )
    assert snapshot(no_total, ram_sampler=_ram, now=lambda: 5.0).eta_seconds is None
    # no progress yet
    no_prog = _FakeJob(
        steps=[],
        events=[PipelineEvent(kind="started", data={}, ts=1.0)],
        total_steps=3,
    )
    assert snapshot(no_prog, ram_sampler=_ram, now=lambda: 5.0).eta_seconds is None


def test_snapshot_eta_zero_when_complete_and_capped_percent() -> None:
    job = _FakeJob(
        steps=[{"name": str(i)} for i in range(5)],  # over-count vs total=3
        events=[PipelineEvent(kind="started", data={}, ts=1.0)],
        total_steps=3,
    )
    snap = snapshot(job, ram_sampler=_ram, now=lambda: 10.0)
    assert snap.eta_seconds == 0.0
    assert snap.percent == 100.0  # capped at 100


def test_snapshot_eta_none_when_elapsed_non_positive() -> None:
    job = _FakeJob(
        steps=[{"name": "a"}],
        events=[PipelineEvent(kind="started", data={}, ts=100.0)],
        total_steps=2,
    )
    # captured_at <= started -> elapsed <= 0
    snap = snapshot(job, ram_sampler=_ram, now=lambda: 100.0)
    assert snap.eta_seconds is None


def test_snapshot_zero_total_treated_as_unknown() -> None:
    job = _FakeJob(steps=[{"name": "a"}], total_steps=0, events=[])
    snap = snapshot(job, ram_sampler=_ram, now=lambda: 1.0)
    assert snap.percent == 0.0
    assert snap.eta_seconds is None


def test_snapshot_event_without_ts_skipped() -> None:
    ev_no_ts = PipelineEvent(kind="x", data={})
    object.__setattr__(ev_no_ts, "ts", None)
    job = _FakeJob(
        steps=[{"name": "a"}],
        events=[ev_no_ts, PipelineEvent(kind="y", data={}, ts=50.0)],
        total_steps=2,
    )
    snap = snapshot(job, ram_sampler=_ram, now=lambda: 60.0)
    # uses second event ts=50; elapsed=10, rate=10, remaining=1 -> ETA 10
    assert snap.eta_seconds == 10.0


def test_snapshot_missing_id_becomes_empty_string() -> None:
    class _Bare:
        status = "queued"

    snap = snapshot(_Bare(), ram_sampler=_ram, now=lambda: 0.0)
    assert snap.job_id == ""
    assert snap.steps_done == 0
    assert snap.model is None


def test_default_ram_sampler_uses_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    class _VM:
        used = 1024 * 1024 * 100
        available = 1024 * 1024 * 200
        total = 1024 * 1024 * 300

    monkeypatch.setattr(status_mod.psutil, "virtual_memory", lambda: _VM())
    reading = status_mod._default_ram_sampler()
    assert reading == RamReading(used_mb=100.0, available_mb=200.0, total_mb=300.0)


def test_default_clock_used_when_now_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_mod.time, "time", lambda: 999.0)
    job = _FakeJob(steps=[], events=[], total_steps=None)
    snap = snapshot(job, ram_sampler=_ram)
    assert snap.captured_at == 999.0


def test_snapshot_wired_into_get_job_endpoint() -> None:
    async def runner(job: ApiJob, mgr: JobManager) -> None:
        job.job_class = "qa"
        job.steps.append({"name": "plan", "ok": True})
        await mgr.emit(job, "step", {"name": "plan"})
        job.final_output = "answer"

    mgr = JobManager(runner=runner)
    set_job_manager(mgr)
    try:
        client = TestClient(create_app())
        job_id = client.post("/jobs", json={"user_msg": "hi"}).json()["job_id"]
        body: dict[str, Any] = {}
        for _ in range(50):
            body = client.get(f"/jobs/{job_id}").json()
            if body["status"] == "completed":
                break
        # Existing shape preserved
        assert body["id"] == job_id
        assert body["status"] == "completed"
        assert body["steps"] == [{"name": "plan", "ok": True}]
        # Additive snapshot field
        snap = body["snapshot"]
        assert snap["job_id"] == job_id
        assert snap["phase"] == "completed"
        assert snap["current_step"] == "plan"
        assert snap["steps_done"] == 1
        assert "ram_available_mb" in snap
    finally:
        set_job_manager(None)
