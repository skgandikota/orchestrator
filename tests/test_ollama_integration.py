"""Integration test for the Ollama adapter load/verify/unload cycle.

The default variant is hermetic: it exercises the full
:class:`OllamaLocalAdapter` against an :class:`httpx.MockTransport` that
emulates the daemon's ``/api/generate`` and ``/api/ps`` surface, so the test
runs anywhere -- CI included -- without a real Ollama daemon.

A second variant marked ``@pytest.mark.live`` opts in to hitting a real local
daemon. It is gated by the ``OLLAMA_LIVE=1`` environment variable (consistent
with the smoke-test pattern in #113) and requires ``qwen2.5:7b`` to be pulled.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from coracle.config.settings import load_settings
from coracle.models.ollama_local import OllamaLocalAdapter


def _mocked_daemon_handler() -> tuple[Any, dict[str, Any]]:
    """Return a handler emulating the minimal Ollama daemon surface.

    The handler tracks the currently resident model so ``/api/ps`` reflects
    the effect of ``load`` (keep_alive!=0) and ``unload`` (keep_alive==0)
    calls, exactly like the real daemon.
    """
    state: dict[str, Any] = {"resident": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/generate":
            payload = json.loads(request.content)
            if payload.get("keep_alive") == 0:
                state["resident"] = None
            else:
                state["resident"] = payload["model"]
            return httpx.Response(200, json={"response": "", "done": True})
        if path == "/api/ps":
            running = [{"name": state["resident"]}] if state["resident"] else []
            return httpx.Response(200, json={"models": running})
        return httpx.Response(404, text=f"unexpected path: {path}")

    return handler, state


def test_load_verify_unload_cycle_mocked() -> None:
    """Default hermetic test: full lifecycle against MockTransport."""
    handler, state = _mocked_daemon_handler()
    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url="http://localhost:11434", transport=transport)
    adapter = OllamaLocalAdapter(
        base_url="http://localhost:11434",
        request_timeout_s=5.0,
        keep_alive="24h",
        client=client,
    )
    model = "qwen2.5:7b"
    try:
        assert adapter.verify_unloaded(model) is True

        adapter.load(model)
        assert state["resident"] == model
        assert adapter.verify_loaded(model) is True

        adapter.unload(model)
        assert state["resident"] is None
        assert adapter.verify_unloaded(model) is True
    finally:
        adapter.close()
        client.close()


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("OLLAMA_LIVE") != "1",
    reason="live Ollama test; set OLLAMA_LIVE=1 to enable",
)
def test_load_verify_unload_cycle_against_real_daemon() -> None:
    """Live variant: requires a running Ollama daemon with the reasoning model pulled."""
    settings = load_settings().ollama
    adapter = OllamaLocalAdapter(
        base_url=settings.base_url,
        request_timeout_s=settings.request_timeout_s,
        keep_alive=settings.keep_alive,
    )
    model = settings.reasoning_model
    try:
        adapter.load(model)
        assert adapter.verify_loaded(model) is True
        adapter.unload(model)
        assert adapter.verify_unloaded(model) is True
    finally:
        adapter.close()
