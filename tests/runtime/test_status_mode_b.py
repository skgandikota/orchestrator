"""Tests for status mode B (issue #14): narrator + status_b wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coracle.api.app import create_app
from coracle.api.tasks import (
    Job as ApiJob,
)
from coracle.api.tasks import (
    JobManager,
    JobStatus,
    PipelineEvent,
    set_job_manager,
)
from coracle.config.settings import Settings, StatusSettings
from coracle.models.narrator import Narrator, build_prompt
from coracle.runtime.status import RamReading
from coracle.runtime.status_b import status_b

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ram() -> RamReading:
    return RamReading(used_mb=2048.0, available_mb=6144.0, total_mb=8192.0)


@dataclass
class _FakeJob:
    id: str = "j1"
    status: Any = "running"
    steps: list[Any] = field(default_factory=lambda: [{"name": "plan"}, {"name": "code"}])
    events: list[PipelineEvent] = field(
        default_factory=lambda: [PipelineEvent(kind="started", data={}, ts=100.0)]
    )
    model: str | None = "qwen2.5:7b"
    total_steps: int | None = 4
    started_at: float | None = None


class _StubClient:
    """Captures the prompt + options and returns a canned response."""

    def __init__(self, response: str = "The job is halfway done.") -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        model_id: str,
        prompt: str,
        *,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> str:
        self.calls.append(
            {
                "model": model_id,
                "prompt": prompt,
                "system": system,
                "options": options,
                "stream": stream,
            }
        )
        return self.response


# ---------------------------------------------------------------------------
# Settings: narrator off by default
# ---------------------------------------------------------------------------


def test_default_settings_have_narrator_disabled() -> None:
    s = Settings()
    assert s.status.narrator_enabled is False
    assert s.status.narrator_model == "qwen2.5:1.5b"
    assert s.status.narrator_max_tokens == 80


def test_status_settings_validates_max_tokens() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        StatusSettings(narrator_max_tokens=0)


# ---------------------------------------------------------------------------
# Narrator construction
# ---------------------------------------------------------------------------


def test_disabled_narrator_does_not_instantiate_a_client() -> None:
    def boom() -> Any:  # pragma: no cover - must not be called
        raise AssertionError("client_factory must not run when disabled")

    n = Narrator(enabled=False, client_factory=boom)
    assert n.enabled is False
    assert n.model == "qwen2.5:1.5b"
    assert n.max_tokens == 80


def test_disabled_narrator_narrate_raises() -> None:
    n = Narrator(enabled=False)
    with pytest.raises(RuntimeError, match="disabled"):
        n.narrate({})


def test_enabled_narrator_requires_client_factory() -> None:
    with pytest.raises(ValueError, match="client_factory"):
        Narrator(enabled=True)


def test_max_tokens_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        Narrator(enabled=False, max_tokens=0)


# ---------------------------------------------------------------------------
# Prompt shape
# ---------------------------------------------------------------------------


def test_build_prompt_includes_payload_fields() -> None:
    snap = {"phase": "running", "percent": 50.0, "current_step": "code"}
    prompt = build_prompt(snap)
    assert "Summarize this job status" in prompt
    assert "1-2 short sentences" in prompt
    assert '"phase": "running"' in prompt
    assert '"percent": 50.0' in prompt


def test_build_prompt_accepts_to_dict_objects() -> None:
    class S:
        def to_dict(self) -> dict[str, Any]:
            return {"phase": "queued"}

    prompt = build_prompt(S())
    assert '"phase": "queued"' in prompt


def test_build_prompt_rejects_unknown_types() -> None:
    with pytest.raises(TypeError):
        build_prompt(object())


# ---------------------------------------------------------------------------
# Narrator.narrate
# ---------------------------------------------------------------------------


def test_narrate_dispatches_to_client_with_capped_options() -> None:
    client = _StubClient(response="Job is running step 'code'; about 50% done.")
    n = Narrator(
        enabled=True,
        model="qwen2.5:1.5b",
        max_tokens=80,
        client_factory=lambda: client,
    )
    out = n.narrate({"phase": "running", "percent": 50.0})

    assert out == "Job is running step 'code'; about 50% done."
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "qwen2.5:1.5b"
    assert call["stream"] is False
    assert call["options"]["num_predict"] == 80
    assert call["options"]["stop"]
    assert "Summarize this job status" in call["prompt"]


def test_narrate_caps_long_output() -> None:
    long = " ".join(f"word{i}" for i in range(50))
    client = _StubClient(response=long)
    n = Narrator(enabled=True, max_tokens=10, client_factory=lambda: client)
    out = n.narrate({"phase": "running"})
    parts = out.rstrip("…").split()
    assert len(parts) == 10
    assert out.endswith("…")


def test_narrate_handles_iterable_response() -> None:
    class IterClient:
        def generate(self, *a: Any, **kw: Any) -> Any:
            return iter(["hello ", "world"])

    n = Narrator(enabled=True, max_tokens=80, client_factory=IterClient)
    assert n.narrate({"phase": "queued"}) == "hello world"


def test_narrate_returns_empty_when_client_returns_blank() -> None:
    n = Narrator(enabled=True, max_tokens=80, client_factory=lambda: _StubClient(response="   "))
    assert n.narrate({"phase": "queued"}) == ""


# ---------------------------------------------------------------------------
# status_b wrapper
# ---------------------------------------------------------------------------


def test_status_b_falls_back_when_narrator_is_none() -> None:
    job = _FakeJob()
    payload = status_b(job, ram_sampler=_ram, now=lambda: 110.0)
    assert payload["mode"] == "b"
    assert payload["narrator_disabled"] is True
    assert payload["phase"] == "running"
    assert payload["percent"] == 50.0
    assert "narration" not in payload
    assert "narrator_error" not in payload


def test_status_b_falls_back_when_narrator_disabled() -> None:
    n = Narrator(enabled=False)
    payload = status_b(_FakeJob(), narrator=n, ram_sampler=_ram, now=lambda: 110.0)
    assert payload["narrator_disabled"] is True


def test_status_b_attaches_narration_when_enabled() -> None:
    client = _StubClient(response="Halfway through; current step is code.")
    n = Narrator(enabled=True, max_tokens=80, client_factory=lambda: client)
    payload = status_b(_FakeJob(), narrator=n, ram_sampler=_ram, now=lambda: 110.0)
    assert payload["mode"] == "b"
    assert payload["narration"] == "Halfway through; current step is code."
    assert "narrator_disabled" not in payload
    assert "narrator_error" not in payload
    # Mode A fields are still present.
    assert payload["job_id"] == "j1"
    assert payload["percent"] == 50.0


def test_status_b_captures_narrator_errors() -> None:
    class BoomClient:
        def generate(self, *a: Any, **kw: Any) -> str:
            raise RuntimeError("ollama down")

    n = Narrator(enabled=True, max_tokens=80, client_factory=BoomClient)
    payload = status_b(_FakeJob(), narrator=n, ram_sampler=_ram, now=lambda: 110.0)
    assert "narrator_error" in payload
    assert "ollama down" in payload["narrator_error"]
    # Mode A fields still returned (HTTP 200, not 500).
    assert payload["phase"] == "running"


# ---------------------------------------------------------------------------
# HTTP wiring (POST /jobs/{id}/status?mode=b)
# ---------------------------------------------------------------------------


@pytest.fixture
def http_client() -> Any:
    mgr = JobManager()
    set_job_manager(mgr)
    job = ApiJob(id="job-b", user_msg="hi", model="qwen2.5:7b", status=JobStatus.RUNNING)
    mgr._jobs[job.id] = job  # type: ignore[attr-defined]
    app = create_app()
    yield TestClient(app), mgr, job
    set_job_manager(None)


def test_http_mode_b_disabled_path(http_client: Any) -> None:
    client, _mgr, job = http_client
    resp = client.post(f"/jobs/{job.id}/status", json={"mode": "b"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "b"
    assert body["narrator_disabled"] is True


def test_http_mode_b_enabled_path(http_client: Any) -> None:
    client, mgr, job = http_client
    stub = _StubClient(response="all good")
    mgr.set_narrator(Narrator(enabled=True, max_tokens=80, client_factory=lambda: stub))
    resp = client.post(f"/jobs/{job.id}/status", json={"mode": "b"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["narration"] == "all good"
    assert "narrator_disabled" not in body
    assert len(stub.calls) == 1
