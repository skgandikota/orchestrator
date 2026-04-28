"""Adapter for the local Ollama daemon.

Implements the four callables the single-slot scheduler (#34) needs --
``load``, ``unload``, ``verify_loaded``, ``verify_unloaded`` -- plus
``generate``/``chat``/``list_models`` for end users. By default Ollama keeps
models resident for 5 minutes after the last request via the ``keep_alive``
field; for our single-slot RAM strategy we drive eviction explicitly with
``keep_alive=0`` and warm-up with a long ``keep_alive`` value.

All HTTP calls go through a single :class:`httpx.Client` so connections are
reused. Errors map to :class:`OllamaError`/:class:`OllamaTimeout`; retries are
left to callers.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import httpx

__all__ = [
    "OllamaError",
    "OllamaLocalAdapter",
    "OllamaTimeout",
]


class OllamaError(RuntimeError):
    """Raised on a non-2xx response from the Ollama daemon."""

    def __init__(self, message: str, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class OllamaTimeout(OllamaError):
    """Raised when an HTTP call to the Ollama daemon times out."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=None, body="")


class OllamaLocalAdapter:
    """Synchronous client for the local Ollama HTTP API.

    Args:
        base_url: Root URL of the Ollama daemon (no trailing slash required).
        request_timeout_s: Per-request timeout in seconds.
        keep_alive: Keep-alive duration sent with ``load``/``generate``/``chat``
            so Ollama does not auto-evict mid-job. Ollama accepts duration
            strings like ``"24h"`` or integer seconds. ``unload`` always sends
            ``0`` regardless of this value.
        client: Optional pre-built :class:`httpx.Client` (used by tests with
            a mocked transport). When provided, the adapter does not own its
            lifetime -- :meth:`close` is a no-op for externally-supplied
            clients.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        request_timeout_s: float = 120.0,
        *,
        keep_alive: str = "24h",
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._request_timeout_s = request_timeout_s
        self._keep_alive = keep_alive
        if client is None:
            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=request_timeout_s,
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP client (if owned)."""
        if self._owns_client:
            self._client.close()

    def __del__(self) -> None:  # pragma: no cover - GC timing dependent
        with contextlib.suppress(Exception):
            self.close()

    # -- internals --------------------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            try:
                body = response.text
            except Exception:  # pragma: no cover - defensive
                body = ""
            raise OllamaError(
                f"ollama returned HTTP {response.status_code}",
                status_code=response.status_code,
                body=body,
            )

    def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            response = self._client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise OllamaTimeout(f"timeout calling {path}: {exc}") from exc
        self._raise_for_status(response)
        return response

    def _get(self, path: str) -> httpx.Response:
        try:
            response = self._client.get(path)
        except httpx.TimeoutException as exc:
            raise OllamaTimeout(f"timeout calling {path}: {exc}") from exc
        self._raise_for_status(response)
        return response

    @staticmethod
    def _iter_ndjson(response: httpx.Response) -> Iterator[dict[str, Any]]:
        for raw in response.iter_lines():
            line = raw.strip()
            if not line:
                continue
            yield json.loads(line)

    # -- scheduler surface ------------------------------------------------

    def load(self, model_id: str) -> None:
        """Pull ``model_id`` into memory using a warm-up generate.

        Sends ``POST /api/generate`` with an empty prompt and the configured
        ``keep_alive`` duration so Ollama keeps the model resident.
        """
        self._post(
            "/api/generate",
            {
                "model": model_id,
                "prompt": "",
                "stream": False,
                "keep_alive": self._keep_alive,
            },
        )

    def unload(self, model_id: str) -> None:
        """Evict ``model_id`` immediately via ``keep_alive=0``."""
        self._post(
            "/api/generate",
            {
                "model": model_id,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            },
        )

    def _running_model_names(self) -> list[str]:
        response = self._get("/api/ps")
        data = response.json() or {}
        models = data.get("models") or []
        names: list[str] = []
        for entry in models:
            name = entry.get("name") or entry.get("model")
            if name:
                names.append(name)
        return names

    def verify_loaded(self, model_id: str) -> bool:
        return model_id in self._running_model_names()

    def verify_unloaded(self, model_id: str) -> bool:
        return model_id not in self._running_model_names()

    # -- inference --------------------------------------------------------

    def list_models(self) -> list[str]:
        """Return names of models available locally (``GET /api/tags``)."""
        response = self._get("/api/tags")
        data = response.json() or {}
        models = data.get("models") or []
        names: list[str] = []
        for entry in models:
            name = entry.get("name") or entry.get("model")
            if name:
                names.append(name)
        return names

    def generate(
        self,
        model_id: str,
        prompt: str,
        *,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> str | Iterator[str]:
        """Call ``POST /api/generate``.

        When ``stream=True`` returns a generator yielding each chunk's
        ``response`` field. When ``stream=False`` returns the joined string.
        """
        payload: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "stream": stream,
            "keep_alive": self._keep_alive,
        }
        if system is not None:
            payload["system"] = system
        if options is not None:
            payload["options"] = options

        if stream:
            return self._stream_generate(payload)
        response = self._post("/api/generate", payload)
        data = response.json() or {}
        return str(data.get("response", ""))

    def _stream_generate(self, payload: dict[str, Any]) -> Iterator[str]:
        try:
            with self._client.stream("POST", "/api/generate", json=payload) as response:
                self._raise_for_status(response)
                for chunk in self._iter_ndjson(response):
                    piece = chunk.get("response")
                    if piece:
                        yield str(piece)
                    if chunk.get("done"):
                        break
        except httpx.TimeoutException as exc:
            raise OllamaTimeout(f"timeout calling /api/generate: {exc}") from exc

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> str | Iterator[str]:
        """Call ``POST /api/chat``.

        Streaming mode yields each chunk's ``message.content``; non-streaming
        returns the assistant message content joined into one string.
        """
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": stream,
            "keep_alive": self._keep_alive,
        }
        if options is not None:
            payload["options"] = options

        if stream:
            return self._stream_chat(payload)
        response = self._post("/api/chat", payload)
        data = response.json() or {}
        message = data.get("message") or {}
        return str(message.get("content", ""))

    def _stream_chat(self, payload: dict[str, Any]) -> Iterator[str]:
        try:
            with self._client.stream("POST", "/api/chat", json=payload) as response:
                self._raise_for_status(response)
                for chunk in self._iter_ndjson(response):
                    msg = chunk.get("message") or {}
                    piece = msg.get("content")
                    if piece:
                        yield str(piece)
                    if chunk.get("done"):
                        break
        except httpx.TimeoutException as exc:
            raise OllamaTimeout(f"timeout calling /api/chat: {exc}") from exc
