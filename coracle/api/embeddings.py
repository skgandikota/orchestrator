"""OpenAI-compatible ``/v1/embeddings`` passthrough.

This router is **deliberately narrow**. The coracle's value-add is
chat-completions plus the local pipeline (classifier, scheduler, tool
execution); embeddings are stateless vector lookups that have nothing
to gain from any of that machinery. We therefore expose embeddings as
a thin passthrough that walks the configured provider fallback chain
(see :mod:`coracle.providers.fallback`) and returns the canonical
OpenAI embeddings response shape.

Scheduler-bypass contract
-------------------------
Streaming, tool calls, the classifier, the multi-step pipeline, and the
local LLM slot are **never** touched for embeddings requests. The
handler picks a provider, calls it, and returns the result untouched.
If a provider fails transiently, the chain advances; if every provider
is exhausted, the client receives a 429 with the same body shape used
by chat completions.

Image / audio / batch / rerank endpoints are explicit non-goals; see
``docs/INTERFACES.md``.

The router lives in its own module (mounted separately by
:mod:`coracle.api.app`) so it can be edited and rebased without
conflicting with the chat-completions router in
:mod:`coracle.api.openai_compat`.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from coracle.api.openai_compat import _auth_dependency

__all__ = [
    "EMBEDDING_MODEL_IDS",
    "EmbeddingRequest",
    "EmbeddingsBackend",
    "build_router",
    "router",
    "set_embeddings_backend",
]


EMBEDDING_MODEL_IDS: tuple[str, ...] = (
    "coracle-embed",
    "coracle-embed-small",
)


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class EmbeddingRequest(BaseModel):
    """Canonical OpenAI ``/v1/embeddings`` request shape."""

    model: str
    input: str | list[str] | list[int] | list[list[int]]
    encoding_format: str | None = Field(default=None)
    dimensions: int | None = Field(default=None, ge=1)
    user: str | None = None


# ---------------------------------------------------------------------------
# Backend protocol + injection
# ---------------------------------------------------------------------------


class EmbeddingsBackend(Protocol):
    """Minimal surface a provider-chain backend must satisfy.

    Implementations forward the request to the configured provider chain
    (Gemini -> Voyage -> OpenAI-compatible local, etc.) and return one
    vector per input. Failures should be raised as :class:`HTTPException`
    or as :class:`coracle.providers.fallback.AllProvidersFailed`;
    the router translates the latter into a 429.
    """

    def embed(
        self,
        *,
        model: str,
        inputs: list[str],
        encoding_format: str | None,
        dimensions: int | None,
    ) -> list[list[float]]:
        """Embed ``inputs`` using ``model`` and return one vector per input."""


class _StubBackend:
    """Default backend returning a deterministic zero-vector.

    Used when no real provider chain has been wired in (e.g. unit tests
    that target *other* routes). Real deployments inject a chain-backed
    backend via :func:`set_embeddings_backend`.
    """

    def embed(
        self,
        *,
        model: str,
        inputs: list[str],
        encoding_format: str | None,
        dimensions: int | None,
    ) -> list[list[float]]:
        dim = dimensions or 8
        return [[0.0] * dim for _ in inputs]


_BACKEND: EmbeddingsBackend = _StubBackend()


def set_embeddings_backend(backend: EmbeddingsBackend) -> None:
    """Inject the embeddings backend (used by bootstrap and tests)."""
    global _BACKEND
    _BACKEND = backend


def _current_backend() -> EmbeddingsBackend:
    return _BACKEND


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_inputs(value: Any) -> list[str]:
    """Normalise the polymorphic ``input`` field to ``list[str]``.

    OpenAI accepts a string, list of strings, list of ints (a single
    pre-tokenised sequence) or list of lists of ints (a batch). We do
    not tokenise locally; token arrays are stringified so the upstream
    provider sees a deterministic textual form.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        if not value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="input must not be empty",
            )
        if all(isinstance(v, str) for v in value):
            return list(value)
        if all(isinstance(v, int) for v in value):
            return [" ".join(str(t) for t in value)]
        if all(isinstance(v, list) and v and all(isinstance(t, int) for t in v) for v in value):
            return [" ".join(str(t) for t in row) for row in value]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="input must be str, list[str], list[int] or list[list[int]]",
    )


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_router() -> APIRouter:
    """Build the embeddings router (mounted under ``/v1`` by the app)."""
    api = APIRouter(dependencies=[Depends(_auth_dependency)])

    @api.post("/embeddings")
    def embeddings(req: EmbeddingRequest) -> dict[str, Any]:
        if req.model not in EMBEDDING_MODEL_IDS:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown embedding model {req.model!r}",
            )
        inputs = _coerce_inputs(req.input)
        backend = _current_backend()

        # Lazy import to avoid hard-coupling this module to providers at
        # import time (keeps test surface small and avoids cycles).
        from coracle.providers.fallback import AllProvidersFailed

        try:
            vectors = backend.embed(
                model=req.model,
                inputs=inputs,
                encoding_format=req.encoding_format,
                dimensions=req.dimensions,
            )
        except AllProvidersFailed as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"all embeddings providers exhausted: {exc}",
            ) from exc

        if len(vectors) != len(inputs):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="embeddings backend returned wrong number of vectors",
            )

        prompt_tokens = sum(_approx_tokens(t) for t in inputs)
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": list(vec)}
                for i, vec in enumerate(vectors)
            ],
            "model": req.model,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "total_tokens": prompt_tokens,
            },
        }

    return api


router = build_router()
