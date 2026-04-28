"""Unit tests for :mod:`orchestrator.models.ollama_local`.

All HTTP traffic goes through an :class:`httpx.MockTransport` so no real
Ollama daemon is required.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from orchestrator.core.scheduler import LlmSlotScheduler
from orchestrator.models.ollama_local import (
    OllamaError,
    OllamaLocalAdapter,
    OllamaTimeout,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _make_adapter(handler: Handler, *, keep_alive: str = "24h") -> OllamaLocalAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url="http://localhost:11434", transport=transport)
    return OllamaLocalAdapter(
        base_url="http://localhost:11434",
        request_timeout_s=5.0,
        keep_alive=keep_alive,
        client=client,
    )


def _ndjson(chunks: list[dict[str, Any]]) -> bytes:
    return ("\n".join(json.dumps(c) for c in chunks) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# load / unload
# ---------------------------------------------------------------------------


def test_load_sends_warmup_with_keep_alive() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"response": "", "done": True})

    adapter = _make_adapter(handler, keep_alive="24h")
    adapter.load("qwen2.5:7b")
    assert seen == [{"model": "qwen2.5:7b", "prompt": "", "stream": False, "keep_alive": "24h"}]
    adapter.close()


def test_unload_uses_keep_alive_zero() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"response": "", "done": True})

    adapter = _make_adapter(handler)
    adapter.unload("qwen2.5:7b")
    assert seen[0]["keep_alive"] == 0
    assert seen[0]["model"] == "qwen2.5:7b"
    adapter.close()


def test_load_http_500_raises_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    adapter = _make_adapter(handler)
    with pytest.raises(OllamaError) as info:
        adapter.load("qwen2.5:7b")
    assert info.value.status_code == 500
    assert info.value.body == "boom"
    adapter.close()


# ---------------------------------------------------------------------------
# verify_loaded / verify_unloaded
# ---------------------------------------------------------------------------


def _ps_handler(names: list[str]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": [{"name": n} for n in names]})

    return handler


def test_verify_loaded_true_when_present() -> None:
    adapter = _make_adapter(_ps_handler(["qwen2.5:7b", "other:1b"]))
    assert adapter.verify_loaded("qwen2.5:7b") is True
    adapter.close()


def test_verify_loaded_false_when_absent() -> None:
    adapter = _make_adapter(_ps_handler([]))
    assert adapter.verify_loaded("qwen2.5:7b") is False
    adapter.close()


def test_verify_unloaded_true_when_absent() -> None:
    adapter = _make_adapter(_ps_handler(["other:1b"]))
    assert adapter.verify_unloaded("qwen2.5:7b") is True
    adapter.close()


def test_verify_unloaded_false_when_present() -> None:
    adapter = _make_adapter(_ps_handler(["qwen2.5:7b"]))
    assert adapter.verify_unloaded("qwen2.5:7b") is False
    adapter.close()


def test_verify_handles_model_key_alias_and_empty_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Empty body coerces to {}, exercising the `data or {}` branch.
        if request.url.path == "/api/ps":
            return httpx.Response(
                200,
                json={"models": [{"model": "foo:7b"}, {}]},
            )
        return httpx.Response(404)

    adapter = _make_adapter(handler)
    assert adapter.verify_loaded("foo:7b") is True
    adapter.close()


# ---------------------------------------------------------------------------
# generate / chat
# ---------------------------------------------------------------------------


def test_generate_non_stream_returns_response_field() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"response": "hello world", "done": True})

    adapter = _make_adapter(handler)
    out = adapter.generate(
        "qwen2.5:7b",
        "hi",
        system="be brief",
        options={"temperature": 0.1},
    )
    assert out == "hello world"
    assert seen[0]["system"] == "be brief"
    assert seen[0]["options"] == {"temperature": 0.1}
    assert seen[0]["stream"] is False
    assert seen[0]["keep_alive"] == "24h"
    adapter.close()


def test_generate_stream_yields_chunks() -> None:
    chunks = [
        {"response": "hel", "done": False},
        {"response": "lo", "done": False},
        {"response": "", "done": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ndjson(chunks))

    adapter = _make_adapter(handler)
    result = adapter.generate("qwen2.5:7b", "hi", stream=True)
    assert isinstance(result, Iterator)
    assert list(result) == ["hel", "lo"]
    adapter.close()


def test_generate_stream_handles_blank_lines_and_natural_eof() -> None:
    body = b'\n{"response": "a", "done": false}\n   \n{"response": "b", "done": false}\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    adapter = _make_adapter(handler)
    out = list(adapter.generate("qwen2.5:7b", "hi", stream=True))  # type: ignore[arg-type]
    assert out == ["a", "b"]
    adapter.close()


def test_chat_stream_natural_eof() -> None:
    body = (
        b'{"message": {"content": "x"}, "done": false}\n'
        b'{"message": {"content": "y"}, "done": false}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    adapter = _make_adapter(handler)
    out = list(
        adapter.chat(  # type: ignore[arg-type]
            "qwen2.5:7b",
            [{"role": "user", "content": "hi"}],
            stream=True,
        )
    )
    assert out == ["x", "y"]
    adapter.close()


def test_generate_stream_500_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    adapter = _make_adapter(handler)
    gen = adapter.generate("qwen2.5:7b", "hi", stream=True)
    with pytest.raises(OllamaError):
        list(gen)  # type: ignore[arg-type]
    adapter.close()


def test_chat_non_stream_returns_message_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "hi back"}, "done": True},
        )

    adapter = _make_adapter(handler)
    out = adapter.chat(
        "qwen2.5:7b",
        [{"role": "user", "content": "hi"}],
        options={"temperature": 0.0},
    )
    assert out == "hi back"
    adapter.close()


def test_chat_stream_yields_content() -> None:
    chunks = [
        {"message": {"role": "assistant", "content": "he"}, "done": False},
        {"message": {"role": "assistant", "content": "llo"}, "done": False},
        {"message": {}, "done": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ndjson(chunks))

    adapter = _make_adapter(handler)
    result = adapter.chat(
        "qwen2.5:7b",
        [{"role": "user", "content": "hi"}],
        stream=True,
    )
    assert isinstance(result, Iterator)
    assert list(result) == ["he", "llo"]
    adapter.close()


def test_chat_stream_500_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    adapter = _make_adapter(handler)
    gen = adapter.chat("qwen2.5:7b", [{"role": "user", "content": "hi"}], stream=True)
    with pytest.raises(OllamaError):
        list(gen)  # type: ignore[arg-type]
    adapter.close()


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


def test_list_models_returns_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen2.5:7b"}, {"name": "qwen2.5-coder:7b"}, {}]},
        )

    adapter = _make_adapter(handler)
    assert adapter.list_models() == ["qwen2.5:7b", "qwen2.5-coder:7b"]
    adapter.close()


def test_list_models_empty_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    adapter = _make_adapter(handler)
    assert adapter.list_models() == []
    adapter.close()


# ---------------------------------------------------------------------------
# timeouts and error wrapping
# ---------------------------------------------------------------------------


def test_post_timeout_raises_ollama_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    adapter = _make_adapter(handler)
    with pytest.raises(OllamaTimeout):
        adapter.load("qwen2.5:7b")
    adapter.close()


def test_get_timeout_raises_ollama_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    adapter = _make_adapter(handler)
    with pytest.raises(OllamaTimeout):
        adapter.list_models()
    adapter.close()


def test_stream_timeout_raises_ollama_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    adapter = _make_adapter(handler)
    gen = adapter.generate("qwen2.5:7b", "hi", stream=True)
    with pytest.raises(OllamaTimeout):
        list(gen)  # type: ignore[arg-type]

    gen2 = adapter.chat("qwen2.5:7b", [{"role": "user", "content": "hi"}], stream=True)
    with pytest.raises(OllamaTimeout):
        list(gen2)  # type: ignore[arg-type]
    adapter.close()


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def test_default_constructor_owns_client_and_close_is_idempotent() -> None:
    adapter = OllamaLocalAdapter(base_url="http://localhost:11434/", request_timeout_s=1.0)
    assert adapter._owns_client is True
    assert adapter._base_url == "http://localhost:11434"
    adapter.close()
    adapter.close()


def test_externally_supplied_client_is_not_closed_by_adapter() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    client = httpx.Client(base_url="http://localhost:11434", transport=transport)
    adapter = OllamaLocalAdapter(client=client)
    adapter.close()
    # Client still usable because adapter does not own it.
    assert client.get("/api/tags").status_code == 200
    client.close()


# ---------------------------------------------------------------------------
# scheduler integration
# ---------------------------------------------------------------------------


class _FakeRamMonitor:
    def __init__(self, available_mb: float) -> None:
        self._available = available_mb

    def current_snapshot(self) -> Any:
        class _Snap:
            available_mb = self._available

        return _Snap()


def test_scheduler_integration_swap_cycle() -> None:
    state = {"resident": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/generate":
            payload = json.loads(request.content)
            ka = payload.get("keep_alive")
            if ka == 0:
                state["resident"] = None
            else:
                state["resident"] = payload["model"]
            return httpx.Response(200, json={"response": "", "done": True})
        if path == "/api/ps":
            running = [{"name": state["resident"]}] if state["resident"] else []
            return httpx.Response(200, json={"models": running})
        return httpx.Response(404)

    adapter = _make_adapter(handler)
    scheduler = LlmSlotScheduler(
        ram_monitor=_FakeRamMonitor(available_mb=10_000),
        min_free_mb_for_load=5_500,
    )
    for mid in ("qwen2.5:7b", "qwen2.5-coder:7b"):
        scheduler.register_adapter(
            mid,
            load=adapter.load,
            unload=adapter.unload,
            verify_loaded=adapter.verify_loaded,
            verify_unloaded=adapter.verify_unloaded,
        )

    with scheduler.acquire("qwen2.5:7b"):
        assert state["resident"] == "qwen2.5:7b"
    with scheduler.acquire("qwen2.5-coder:7b"):
        assert state["resident"] == "qwen2.5-coder:7b"

    adapter.close()
