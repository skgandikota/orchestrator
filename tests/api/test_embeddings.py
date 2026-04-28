"""Tests for the ``/v1/embeddings`` passthrough router."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from orchestrator.api import create_app
from orchestrator.api import embeddings as embeddings_module
from orchestrator.api.embeddings import (
    EMBEDDING_MODEL_IDS,
    set_embeddings_backend,
)
from orchestrator.providers.fallback import AllProvidersFailed, QuotaExceeded


class FakeBackend:
    """Deterministic embeddings backend for the happy path tests."""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls: list[dict[str, object]] = []

    def embed(
        self,
        *,
        model: str,
        inputs: list[str],
        encoding_format: str | None,
        dimensions: int | None,
    ) -> list[list[float]]:
        self.calls.append(
            {
                "model": model,
                "inputs": list(inputs),
                "encoding_format": encoding_format,
                "dimensions": dimensions,
            }
        )
        d = dimensions or self.dim
        return [[float(i + 1)] * d for i, _ in enumerate(inputs)]


class ExhaustedBackend:
    def embed(self, **_: object) -> list[list[float]]:
        raise AllProvidersFailed(failures=[("gemini", QuotaExceeded("rate limit"))])


class WrongCountBackend:
    def embed(self, *, inputs: list[str], **_: object) -> list[list[float]]:
        return [[0.0]] * (len(inputs) + 1)


@pytest.fixture
def fake_backend() -> Iterator[FakeBackend]:
    backend = FakeBackend()
    set_embeddings_backend(backend)
    try:
        yield backend
    finally:
        set_embeddings_backend(embeddings_module._StubBackend())


@pytest.fixture
def client(fake_backend: FakeBackend) -> TestClient:
    return TestClient(create_app())


# --- happy path -----------------------------------------------------------


def test_embeddings_string_input(client: TestClient, fake_backend: FakeBackend) -> None:
    resp = client.post(
        "/v1/embeddings",
        json={"model": "orchestrator-embed", "input": "hello world"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["model"] == "orchestrator-embed"
    assert len(body["data"]) == 1
    item = body["data"][0]
    assert item["object"] == "embedding"
    assert item["index"] == 0
    assert item["embedding"] == [1.0, 1.0, 1.0, 1.0]
    assert body["usage"]["prompt_tokens"] == body["usage"]["total_tokens"] >= 1
    assert fake_backend.calls[0]["inputs"] == ["hello world"]


def test_embeddings_list_of_strings_with_dimensions(
    client: TestClient, fake_backend: FakeBackend
) -> None:
    resp = client.post(
        "/v1/embeddings",
        json={
            "model": "orchestrator-embed-small",
            "input": ["a", "bb", "ccc"],
            "dimensions": 2,
            "encoding_format": "float",
            "user": "u-1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [d["embedding"] for d in body["data"]] == [
        [1.0, 1.0],
        [2.0, 2.0],
        [3.0, 3.0],
    ]
    assert fake_backend.calls[0]["dimensions"] == 2
    assert fake_backend.calls[0]["encoding_format"] == "float"


def test_embeddings_token_array(client: TestClient, fake_backend: FakeBackend) -> None:
    resp = client.post(
        "/v1/embeddings",
        json={"model": "orchestrator-embed", "input": [1, 2, 3]},
    )
    assert resp.status_code == 200
    assert fake_backend.calls[0]["inputs"] == ["1 2 3"]


def test_embeddings_token_batch(client: TestClient, fake_backend: FakeBackend) -> None:
    resp = client.post(
        "/v1/embeddings",
        json={"model": "orchestrator-embed", "input": [[1, 2], [3, 4, 5]]},
    )
    assert resp.status_code == 200
    assert fake_backend.calls[0]["inputs"] == ["1 2", "3 4 5"]
    assert len(resp.json()["data"]) == 2


# --- error paths ----------------------------------------------------------


def test_embeddings_unknown_model_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/v1/embeddings",
        json={"model": "gpt-nonsense", "input": "x"},
    )
    assert resp.status_code == 404
    assert "unknown embedding model" in resp.json()["detail"]


def test_embeddings_empty_input_list_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/embeddings",
        json={"model": "orchestrator-embed", "input": []},
    )
    assert resp.status_code == 400
    assert "must not be empty" in resp.json()["detail"]


def test_embeddings_invalid_input_type_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/embeddings",
        json={"model": "orchestrator-embed", "input": [1.5, 2.5]},
    )
    # pydantic rejects float-only list before our coercer; either 400 or 422.
    assert resp.status_code in (400, 422)


def test_embeddings_mixed_input_list_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/embeddings",
        json={"model": "orchestrator-embed", "input": [[1, 2], []]},
    )
    assert resp.status_code == 400


def test_embeddings_provider_exhaustion_returns_429() -> None:
    set_embeddings_backend(ExhaustedBackend())
    try:
        c = TestClient(create_app())
        resp = c.post(
            "/v1/embeddings",
            json={"model": "orchestrator-embed", "input": "x"},
        )
        assert resp.status_code == 429
        assert "exhausted" in resp.json()["detail"]
    finally:
        set_embeddings_backend(embeddings_module._StubBackend())


def test_embeddings_wrong_vector_count_returns_502() -> None:
    set_embeddings_backend(WrongCountBackend())
    try:
        c = TestClient(create_app())
        resp = c.post(
            "/v1/embeddings",
            json={"model": "orchestrator-embed", "input": "x"},
        )
        assert resp.status_code == 502
    finally:
        set_embeddings_backend(embeddings_module._StubBackend())


# --- default stub & misc --------------------------------------------------


def test_default_stub_backend_returns_zero_vectors() -> None:
    # Reset to default stub, no fixture.
    set_embeddings_backend(embeddings_module._StubBackend())
    c = TestClient(create_app())
    resp = c.post(
        "/v1/embeddings",
        json={"model": "orchestrator-embed", "input": ["hi", "there"], "dimensions": 3},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"][0]["embedding"] == [0.0, 0.0, 0.0]
    assert body["data"][1]["embedding"] == [0.0, 0.0, 0.0]


def test_default_stub_backend_uses_default_dim() -> None:
    set_embeddings_backend(embeddings_module._StubBackend())
    c = TestClient(create_app())
    resp = c.post(
        "/v1/embeddings",
        json={"model": "orchestrator-embed", "input": "hi"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"][0]["embedding"]) == 8


def test_known_embedding_model_ids_are_disjoint_from_chat() -> None:
    from orchestrator.api.openai_compat import MODEL_IDS as CHAT_IDS

    assert set(EMBEDDING_MODEL_IDS).isdisjoint(set(CHAT_IDS))


def test_auth_required_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_API_TOKEN", "secret")
    set_embeddings_backend(FakeBackend())
    try:
        c = TestClient(create_app())
        resp = c.post(
            "/v1/embeddings",
            json={"model": "orchestrator-embed", "input": "x"},
        )
        assert resp.status_code == 401

        resp_ok = c.post(
            "/v1/embeddings",
            json={"model": "orchestrator-embed", "input": "x"},
            headers={"Authorization": "Bearer secret"},
        )
        assert resp_ok.status_code == 200
    finally:
        set_embeddings_backend(embeddings_module._StubBackend())
