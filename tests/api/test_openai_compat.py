"""Tests for the OpenAI-compatible HTTP surface."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

from coracle.api import create_app, openai_compat
from coracle.api.openai_compat import (
    MODEL_IDS,
    ChatBackend,
    Message,
    PipelineEvent,
    set_backend,
)


class FakeBackend:
    """Deterministic backend that emits one of every event type."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def stream(
        self,
        *,
        job_id: str,
        model: str,
        messages: list[Message],
        temperature: float | None,
        max_tokens: int | None,
    ) -> AsyncIterator[PipelineEvent]:
        self.calls.append(
            {
                "job_id": job_id,
                "model": model,
                "messages": [m.content for m in messages],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        yield PipelineEvent(type="classify", data={"profile": model})
        yield PipelineEvent(type="consolidate", text="ctx")
        yield PipelineEvent(type="step", data={"name": "plan"})
        yield PipelineEvent(type="token", text="hello")
        yield PipelineEvent(type="token", text=" world")
        yield PipelineEvent(type="final", text="")


@pytest.fixture
def fake_backend() -> Iterator[FakeBackend]:
    backend = FakeBackend()
    set_backend(backend)
    try:
        yield backend
    finally:
        set_backend(openai_compat._StubBackend())


@pytest.fixture
def client(fake_backend: FakeBackend) -> TestClient:
    return TestClient(create_app())


def test_list_models_shape(client: TestClient) -> None:
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert ids == list(MODEL_IDS)
    for m in body["data"]:
        assert m["object"] == "model"
        assert m["owned_by"] == "coracle"
        assert isinstance(m["created"], int)


def test_chat_completion_non_stream(client: TestClient, fake_backend: FakeBackend) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "coracle",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.1,
            "max_tokens": 16,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "coracle"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "hello world",
    }
    usage = body["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert fake_backend.calls[0]["model"] == "coracle"
    assert fake_backend.calls[0]["temperature"] == 0.1
    assert fake_backend.calls[0]["max_tokens"] == 16


def test_chat_completion_stream_sse(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "coracle-fast",
            "messages": [{"role": "user", "content": "stream please"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join(resp.iter_bytes()).decode("utf-8")

    lines = [line for line in body.split("\n\n") if line.strip()]
    assert lines[-1] == "data: [DONE]"

    payloads = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert all(p["object"] == "chat.completion.chunk" for p in payloads)
    assert all(p["model"] == "coracle-fast" for p in payloads)
    content_pieces = [
        p["choices"][0]["delta"].get("content", "")
        for p in payloads
        if "content" in p["choices"][0]["delta"]
    ]
    joined = "".join(content_pieces)
    assert "[classify:coracle-fast]" in joined
    assert "[consolidate] ctx" in joined
    assert "[step:plan]" in joined
    assert "hello world" in joined
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


def test_completions_shim(client: TestClient) -> None:
    resp = client.post(
        "/v1/completions",
        json={"model": "coracle-deep", "prompt": "ping"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "text_completion"
    assert body["id"].startswith("cmpl-")
    assert body["choices"][0]["text"] == "hello world"
    assert body["choices"][0]["finish_reason"] == "stop"
    usage = body["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_completions_stream(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/completions",
        json={"model": "coracle-research", "prompt": "p", "stream": True},
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode("utf-8")
    assert body.rstrip().endswith("data: [DONE]")


def test_unknown_model_rejected(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    assert "unknown model" in resp.json()["detail"]


def test_unknown_model_rejected_completions(client: TestClient) -> None:
    resp = client.post(
        "/v1/completions",
        json={"model": "gpt-4", "prompt": "hi"},
    )
    assert resp.status_code == 400


def test_bearer_auth_required_when_token_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORACLE_API_TOKEN", "secret")
    resp = client.get("/v1/models")
    assert resp.status_code == 401
    resp = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    resp = client.get("/v1/models", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    resp = client.get("/v1/models", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


def test_stub_backend_emits_all_event_types() -> None:
    backend = openai_compat._StubBackend()

    async def collect() -> list[PipelineEvent]:
        return [
            ev
            async for ev in backend.stream(
                job_id="job",
                model="coracle",
                messages=[
                    Message(role="system", content="ignored"),
                    Message(role="user", content=""),
                ],
                temperature=None,
                max_tokens=None,
            )
        ]

    events = asyncio.run(collect())
    types = {e.type for e in events}
    assert {"classify", "consolidate", "step", "token", "final"} <= types


def test_set_backend_round_trip(fake_backend: FakeBackend) -> None:
    assert isinstance(openai_compat._current_backend(), FakeBackend)


def test_chat_backend_protocol_is_runtime_usable() -> None:
    assert ChatBackend is not None


def test_assemble_content_ignores_non_token_events() -> None:
    events = [
        PipelineEvent(type="classify"),
        PipelineEvent(type="token", text="a"),
        PipelineEvent(type="consolidate", text="x"),
        PipelineEvent(type="token", text="b"),
        PipelineEvent(type="final"),
    ]
    assert openai_compat._assemble_content(events) == "ab"


def test_event_to_delta_final_returns_none() -> None:
    assert openai_compat._event_to_delta(PipelineEvent(type="final")) is None


def test_approx_tokens_handles_empty_string() -> None:
    assert openai_compat._approx_tokens("") == 0
    assert openai_compat._approx_tokens("hello world") >= 1
