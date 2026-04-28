"""Status mode B narrator (issue #14).

A thin wrapper around a tiny ``qwen2.5:1.5b`` model held *outside* the
single-LLM-slot scheduler so it can co-exist with a 7B model. The
narrator turns a structured :class:`~orchestrator.runtime.status.Snapshot`
(mode A) into a one- or two-sentence natural-language gloss.

Architectural rules enforced here:

* Status queries must not require an LLM by default — the narrator is
  *opt-in* via ``[status] narrator_enabled = true``. When disabled, the
  constructor short-circuits and never opens an Ollama handle.
* The narrator lives outside the scheduler's single-slot mutex. It is a
  sidecar with its own ``keep_alive`` so Ollama keeps it resident
  alongside the active 7B reasoning/coder model.
* Output is bounded: the prompt requests ≤2 sentences, ``num_predict``
  caps tokens, and the result is hard-truncated client-side as a belt
  on the suspenders.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

__all__ = ["Narrator", "NarratorClient", "build_prompt"]


_PROMPT_TEMPLATE = (
    "Summarize this job status in 1-2 short sentences for the user. "
    "Be concrete; mention the phase, current step and percent complete "
    "if present. Do not invent details. Status JSON:\n{payload}"
)

_STOP_SEQUENCES: tuple[str, ...] = ("\n\n", "</s>")


class NarratorClient(Protocol):
    """Minimum surface the narrator needs from an Ollama-like adapter."""

    def generate(
        self,
        model_id: str,
        prompt: str,
        *,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> Any: ...


def _snapshot_payload(snapshot: Any) -> dict[str, Any]:
    """Coerce a Snapshot / dict / arbitrary mapping to a JSON-friendly dict."""
    to_dict = getattr(snapshot, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if isinstance(snapshot, dict):
        return dict(snapshot)
    raise TypeError(f"unsupported snapshot type: {type(snapshot).__name__}")


def build_prompt(snapshot: Any) -> str:
    """Render the fixed narrator prompt for ``snapshot``."""
    payload = _snapshot_payload(snapshot)
    return _PROMPT_TEMPLATE.format(payload=json.dumps(payload, sort_keys=True, default=str))


def _cap_output(text: str, max_tokens: int) -> str:
    """Hard-cap a narration to roughly ``max_tokens`` whitespace tokens."""
    cleaned = text.strip()
    if not cleaned:
        return cleaned
    parts = cleaned.split()
    if len(parts) <= max_tokens:
        return cleaned
    return " ".join(parts[:max_tokens]).rstrip(",;:- ") + "…"


class Narrator:
    """Sidecar narrator for status mode B.

    Args:
        enabled: Master switch (mirrors ``[status] narrator_enabled``).
            When ``False`` the narrator is inert: :meth:`narrate` raises
            :class:`RuntimeError` and no client is ever invoked.
        model: Ollama model id (default ``qwen2.5:1.5b``).
        max_tokens: Hard cap on the response length (≈ ~80 tokens / 3
            sentences). Sent as ``num_predict`` *and* enforced
            client-side after the call returns.
        client_factory: Zero-arg callable returning a
            :class:`NarratorClient`. Only invoked when ``enabled`` is
            true, so disabled narrators never instantiate an HTTP
            client. Required when ``enabled`` is true.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        model: str = "qwen2.5:1.5b",
        max_tokens: int = 80,
        client_factory: Callable[[], NarratorClient] | None = None,
    ) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        self._enabled = bool(enabled)
        self._model = model
        self._max_tokens = max_tokens
        self._client: NarratorClient | None = None
        if self._enabled:
            if client_factory is None:
                raise ValueError("client_factory is required when enabled=True")
            self._client = client_factory()

    @property
    def enabled(self) -> bool:
        """Whether this narrator can produce narration."""
        return self._enabled

    @property
    def model(self) -> str:
        """The Ollama model id this narrator dispatches to."""
        return self._model

    @property
    def max_tokens(self) -> int:
        """The configured token cap for narrations."""
        return self._max_tokens

    def narrate(self, snapshot: Any) -> str:
        """Render a short natural-language gloss of ``snapshot``.

        Args:
            snapshot: A :class:`~orchestrator.runtime.status.Snapshot`
                or any object exposing ``to_dict()``, or a plain dict.

        Raises:
            RuntimeError: If the narrator is disabled. Callers should
                check :attr:`enabled` first or use
                :func:`orchestrator.runtime.status_b.status_b`, which
                handles the disabled path gracefully.
        """
        if not self._enabled or self._client is None:
            raise RuntimeError("narrator is disabled")
        prompt = build_prompt(snapshot)
        result = self._client.generate(
            self._model,
            prompt,
            options={
                "num_predict": self._max_tokens,
                "stop": list(_STOP_SEQUENCES),
                "temperature": 0.2,
            },
            stream=False,
        )
        text = result if isinstance(result, str) else "".join(result)
        return _cap_output(text, self._max_tokens)
