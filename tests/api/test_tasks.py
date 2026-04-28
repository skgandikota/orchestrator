"""Tests for the native job HTTP API (issue #15)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi.testclient import TestClient

from orchestrator.api import app as app_module
from orchestrator.api import tasks as tasks_module
from orchestrator.api.tasks import (
    Job,
    JobManager,
    JobStatus,
    PipelineEvent,
    get_job_manager,
    set_job_manager,
)


@pytest.fixture(autouse=True)
def _reset_manager() -> AsyncIterator[None]:
    set_job_manager(None)
    yield
    set_job_manager(None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app)


# ---------------------------------------------------------------------------
# router composition
# ---------------------------------------------------------------------------


def test_app_mounts_native_router() -> None:
    paths = {route.path for route in app_module.app.router.routes}
    assert {
        "/jobs",
        "/jobs/{job_id}",
        "/jobs/{job_id}/stream",
        "/jobs/{job_id}/status",
        "/jobs/{job_id}/cancel",
    } <= paths


# ---------------------------------------------------------------------------
# POST /jobs
# ---------------------------------------------------------------------------


def test_submit_returns_202_with_job_id(client: TestClient) -> None:
    async def quiet(job: Job, mgr: JobManager) -> None:
        await mgr.emit(job, "started", {})
        # leave running so we can observe queued/running state

    set_job_manager(JobManager(runner=quiet))
    resp = client.post("/jobs", json={"user_msg": "hello", "model": "m1"})
    assert resp.status_code == 202
    body = resp.json()
    assert "job_id" in body and isinstance(body["job_id"], str)


def test_submit_validates_empty_msg(client: TestClient) -> None:
    resp = client.post("/jobs", json={"user_msg": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /jobs/{id}
# ---------------------------------------------------------------------------


def test_get_unknown_job_is_404(client: TestClient) -> None:
    resp = client.get("/jobs/nope")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown job_id"


def test_get_returns_full_state(client: TestClient) -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        job.job_class = "qa"
        job.steps.append({"name": "plan", "ok": True})
        job.artifacts.append({"path": "out.md"})
        await mgr.emit(job, "step", {"name": "plan"})
        job.final_output = "answer"

    set_job_manager(JobManager(runner=runner))
    job_id = client.post("/jobs", json={"user_msg": "q"}).json()["job_id"]
    # let the runner finish
    for _ in range(20):
        state = client.get(f"/jobs/{job_id}").json()
        if state["status"] == "completed":
            break
    assert state["class"] == "qa"
    assert state["steps"] == [{"name": "plan", "ok": True}]
    assert state["artifacts"] == [{"path": "out.md"}]
    assert state["final_output"] == "answer"
    assert state["error"] is None


# ---------------------------------------------------------------------------
# POST /jobs/{id}/status
# ---------------------------------------------------------------------------


def test_status_modes_a_b_c(client: TestClient) -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        job.job_class = "chat"
        job.steps.append({"name": "s1"})
        await mgr.emit(job, "step", {"name": "s1"})

    set_job_manager(JobManager(runner=runner))
    job_id = client.post("/jobs", json={"user_msg": "hi"}).json()["job_id"]
    for _ in range(20):
        if client.get(f"/jobs/{job_id}").json()["status"] == "completed":
            break

    a = client.post(f"/jobs/{job_id}/status", json={"mode": "a"}).json()
    assert a == {"mode": "a", "status": "completed", "steps_done": 1, "class": "chat"}

    b = client.post(f"/jobs/{job_id}/status", json={"mode": "b"}).json()
    assert b["mode"] == "b" and "narration" in b and b["status"] == "completed"

    c = client.post(f"/jobs/{job_id}/status", json={"mode": "c"}).json()
    assert c["mode"] == "c"
    assert c["steps"] == [{"name": "s1"}]
    assert "step" in c["reasoning"]


def test_status_invalid_mode_rejected_by_pydantic(client: TestClient) -> None:
    set_job_manager(JobManager(runner=_noop_runner))
    job_id = client.post("/jobs", json={"user_msg": "hi"}).json()["job_id"]
    resp = client.post(f"/jobs/{job_id}/status", json={"mode": "z"})
    assert resp.status_code == 422


def test_status_invalid_mode_in_manager_raises_422() -> None:
    mgr = JobManager()
    job = Job(id="x", user_msg="m", model=None)
    with pytest.raises(Exception) as ei:
        mgr.status_payload(job, "z")
    assert "422" in repr(ei.value) or "mode" in repr(ei.value)


def test_status_mode_b_idle_when_no_events() -> None:
    mgr = JobManager()
    job = Job(id="x", user_msg="m", model=None)
    payload = mgr.status_payload(job, "b")
    assert "idle" in payload["narration"]


def test_status_unknown_job_404(client: TestClient) -> None:
    resp = client.post("/jobs/nope/status", json={"mode": "a"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /jobs/{id}/cancel
# ---------------------------------------------------------------------------


async def _noop_runner(job: Job, mgr: JobManager) -> None:
    await mgr.emit(job, "started", {})


def test_cancel_unknown_404(client: TestClient) -> None:
    resp = client.post("/jobs/nope/cancel")
    assert resp.status_code == 404


def test_cancel_running_job() -> None:
    async def slow(job: Job, mgr: JobManager) -> None:
        await mgr.emit(job, "started", {})
        await asyncio.sleep(5)
        await mgr.emit(job, "completed", {})  # pragma: no cover - cancelled first

    async def scenario() -> dict[str, object]:
        set_job_manager(JobManager(runner=slow))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module.app), base_url="http://t"
        ) as ac:
            job_id = (await ac.post("/jobs", json={"user_msg": "x"})).json()["job_id"]
            await asyncio.sleep(0.05)
            resp = await ac.post(f"/jobs/{job_id}/cancel")
            assert resp.status_code == 202
            assert resp.json()["status"] == "cancelled"
            state = (await ac.get(f"/jobs/{job_id}")).json()
            return state

    state = asyncio.run(scenario())
    assert state["status"] == "cancelled"


def test_cancel_terminal_is_noop() -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        await mgr.emit(job, "started", {})

    async def scenario() -> str:
        mgr = JobManager(runner=runner)
        set_job_manager(mgr)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module.app), base_url="http://t"
        ) as ac:
            job_id = (await ac.post("/jobs", json={"user_msg": "x"})).json()["job_id"]
            for _ in range(20):
                state = (await ac.get(f"/jobs/{job_id}")).json()
                if state["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            resp = await ac.post(f"/jobs/{job_id}/cancel")
            assert resp.status_code == 202
            return resp.json()["status"]

    assert asyncio.run(scenario()) == "completed"


# ---------------------------------------------------------------------------
# GET /jobs/{id}/stream  (SSE)
# ---------------------------------------------------------------------------


def test_stream_replays_events_for_terminal_job() -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        await mgr.emit(job, "started", {"x": 1})
        await mgr.emit(job, "step", {"name": "s"})

    async def scenario() -> list[str]:
        set_job_manager(JobManager(runner=runner))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module.app), base_url="http://t"
        ) as ac:
            job_id = (await ac.post("/jobs", json={"user_msg": "x"})).json()["job_id"]
            for _ in range(20):
                if (await ac.get(f"/jobs/{job_id}")).json()["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            kinds: list[str] = []
            async with ac.stream("GET", f"/jobs/{job_id}/stream") as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        kinds.append(line.removeprefix("event: "))
            return kinds

    kinds = asyncio.run(scenario())
    assert "started" in kinds and "step" in kinds


def test_stream_live_subscriber_receives_events() -> None:
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def runner(job: Job, mgr: JobManager) -> None:
        await mgr.emit(job, "started", {})
        started.set()
        await asyncio.wait_for(proceed.wait(), timeout=2.0)
        await mgr.emit(job, "done-step", {"k": "v"})

    async def scenario() -> list[str]:
        set_job_manager(JobManager(runner=runner))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module.app), base_url="http://t"
        ) as ac:
            job_id = (await ac.post("/jobs", json={"user_msg": "x"})).json()["job_id"]
            await started.wait()
            kinds: list[str] = []

            async def reader() -> None:
                async with ac.stream("GET", f"/jobs/{job_id}/stream") as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("event: "):
                            kinds.append(line.removeprefix("event: "))
                        if line.startswith("data: "):
                            json.loads(line.removeprefix("data: "))

            reader_task = asyncio.create_task(reader())
            await asyncio.sleep(0.05)
            proceed.set()
            await asyncio.wait_for(reader_task, timeout=2.0)
            return kinds

    kinds = asyncio.run(scenario())
    assert "done-step" in kinds


def test_stream_unknown_job_404(client: TestClient) -> None:
    resp = client.get("/jobs/nope/stream")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# JobManager unit coverage
# ---------------------------------------------------------------------------


def test_runner_failure_marks_failed() -> None:
    async def boom(job: Job, mgr: JobManager) -> None:
        raise RuntimeError("kaboom")

    async def scenario() -> dict[str, object]:
        mgr = JobManager(runner=boom)
        job = mgr.submit("x", None)
        await asyncio.sleep(0.05)
        return job.to_state()

    state = asyncio.run(scenario())
    assert state["status"] == "failed"
    assert "kaboom" in str(state["error"])


def test_get_job_manager_singleton() -> None:
    set_job_manager(None)
    a = get_job_manager()
    b = get_job_manager()
    assert a is b


def test_default_runner_executes() -> None:
    async def scenario() -> Job:
        mgr = JobManager()  # uses _default_runner
        job = mgr.submit("hi", "m")
        for _ in range(30):
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                break
            await asyncio.sleep(0.01)
        return job

    job = asyncio.run(scenario())
    assert job.status == JobStatus.COMPLETED
    assert job.final_output == "hi"
    assert job.job_class == "default"


def test_pipeline_event_defaults() -> None:
    ev = PipelineEvent(kind="x", data={"a": 1})
    assert ev.kind == "x" and ev.data == {"a": 1} and ev.ts > 0


def test_module_exports() -> None:
    assert hasattr(tasks_module, "router")
    assert hasattr(tasks_module, "JobManager")
