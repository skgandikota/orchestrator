"""OpenAI-compatible HTTP API.

Implements the subset of the OpenAI REST API required for coding-agent
clients (opencode, Claude Code, codex, Cursor, Continue) to treat the
orchestrator as a drop-in model provider.

Endpoints
---------
* ``GET  /v1/models``           - lists the orchestrator model profiles.
* ``POST /v1/chat/completions`` - chat-completions (sync or SSE stream).
* ``POST /v1/completions``      - thin shim wrapping chat-completions.

The handlers route through a :class:`ChatBackend` protocol so the
LiteLLM/fallback router (or, in tests, a fake) can be plugged in
without changing the HTTP surface.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

__all__ = [
    "MODEL_IDS",
    "ChatBackend",
    "ChatCompletionRequest",
    "CompletionRequest",
    "Message",
    "PipelineEvent",
    "build_router",
    "set_backend",
]


MODEL_IDS: tuple[str, ...] = (
    "orchestrator",
    "orchestrator-fast",
    "orchestrator-deep",
    "orchestrator-research",
    "orchestrator-status",
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)


class CompletionRequest(BaseModel):
    model: str
    prompt: str
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineEvent:
    """One event in the orchestrator pipeline event stream.

    ``type`` is one of: ``classify``, ``consolidate``, ``token``,
    ``step``, ``final``. Streaming chunks are emitted in arrival order.
    """

    type: Literal["classify", "consolidate", "token", "step", "final"]
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class ChatBackend(Protocol):
    """Minimal surface needed to translate OpenAI requests to pipeline events."""

    async def stream(
        self,
        *,
        job_id: str,
        model: str,
        messages: list[Message],
        temperature: float | None,
        max_tokens: int | None,
    ) -> AsyncIterator[PipelineEvent]: ...


# ---------------------------------------------------------------------------
# Default (stub) backend
# ---------------------------------------------------------------------------


class _StubBackend:
    """Backend used when no real router is wired in.

    Emits a deterministic event sequence that exercises every chunk
    type in the AC list. Real deployments inject a router-backed
    backend via :func:`set_backend`.
    """

    async def stream(
        self,
        *,
        job_id: str,
        model: str,
        messages: list[Message],
        temperature: float | None,
        max_tokens: int | None,
    ) -> AsyncIterator[PipelineEvent]:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        yield PipelineEvent(type="classify", data={"profile": model, "job_id": job_id})
        yield PipelineEvent(type="consolidate", text=f"summary: {last_user[:64]}")
        yield PipelineEvent(type="step", data={"step": 1, "name": "plan"})
        for tok in ("ok", ":", " ", last_user[:32] or "ack"):
            yield PipelineEvent(type="token", text=tok)
        yield PipelineEvent(type="final", text="")


_BACKEND: ChatBackend = _StubBackend()


def set_backend(backend: ChatBackend) -> None:
    """Inject a backend (used by application bootstrap and tests)."""
    global _BACKEND
    _BACKEND = backend


def _current_backend() -> ChatBackend:
    return _BACKEND


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _auth_dependency(request: Request) -> None:
    expected = os.environ.get("ORCHESTRATOR_API_TOKEN")
    if not expected:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = header.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_model(model: str) -> None:
    if model not in MODEL_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown model {model!r}; valid: {list(MODEL_IDS)}",
        )


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _prompt_tokens(messages: Iterable[Message]) -> int:
    return sum(_approx_tokens(m.content) for m in messages)


def _new_completion_id(job_id: str) -> str:
    return f"chatcmpl-{job_id}"


def _chunk_payload(
    *,
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _event_to_delta(event: PipelineEvent) -> dict[str, Any] | None:
    """Translate a pipeline event into an OpenAI delta payload.

    Returns ``None`` for events that should not produce a chunk
    (e.g. the terminal ``final`` marker).
    """
    if event.type == "final":
        return None
    if event.type == "token":
        return {"content": event.text}
    if event.type == "classify":
        profile = event.data.get("profile", "")
        return {"content": f"[classify:{profile}]"}
    if event.type == "consolidate":
        return {"content": f"[consolidate] {event.text}"}
    name = event.data.get("name", "")
    return {"content": f"[step:{name}]"}


# ---------------------------------------------------------------------------
# Streaming + non-streaming handlers
# ---------------------------------------------------------------------------


async def _run_chat(
    *,
    req: ChatCompletionRequest,
) -> tuple[str, list[PipelineEvent]]:
    job_id = uuid.uuid4().hex[:12]
    backend = _current_backend()
    events: list[PipelineEvent] = []
    async for ev in backend.stream(
        job_id=job_id,
        model=req.model,
        messages=req.messages,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
    ):
        events.append(ev)
    return job_id, events


def _assemble_content(events: Iterable[PipelineEvent]) -> str:
    parts: list[str] = []
    for ev in events:
        if ev.type == "token":
            parts.append(ev.text)
    return "".join(parts)


async def _stream_chat_response(req: ChatCompletionRequest) -> AsyncIterator[str]:
    job_id = uuid.uuid4().hex[:12]
    completion_id = _new_completion_id(job_id)
    created = int(time.time())
    backend = _current_backend()

    yield _sse(
        _chunk_payload(
            completion_id=completion_id,
            created=created,
            model=req.model,
            delta={"role": "assistant"},
        )
    )

    async for event in backend.stream(
        job_id=job_id,
        model=req.model,
        messages=req.messages,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
    ):
        delta = _event_to_delta(event)
        if delta is None:
            continue
        yield _sse(
            _chunk_payload(
                completion_id=completion_id,
                created=created,
                model=req.model,
                delta=delta,
            )
        )

    yield _sse(
        _chunk_payload(
            completion_id=completion_id,
            created=created,
            model=req.model,
            delta={},
            finish_reason="stop",
        )
    )
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_router() -> APIRouter:
    """Build the ``/v1`` FastAPI router."""
    router = APIRouter(prefix="/v1", dependencies=[Depends(_auth_dependency)])

    @router.get("/models")
    def list_models() -> dict[str, Any]:
        created = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": mid,
                    "object": "model",
                    "created": created,
                    "owned_by": "orchestrator",
                }
                for mid in MODEL_IDS
            ],
        }

    @router.post("/chat/completions")
    async def chat_completions(req: ChatCompletionRequest) -> Any:
        _validate_model(req.model)
        if req.stream:
            return StreamingResponse(
                _stream_chat_response(req),
                media_type="text/event-stream",
            )

        job_id, events = await _run_chat(req=req)
        content = _assemble_content(events)
        prompt_tokens = _prompt_tokens(req.messages)
        completion_tokens = _approx_tokens(content)
        return {
            "id": _new_completion_id(job_id),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    @router.post("/completions")
    async def completions(req: CompletionRequest) -> Any:
        _validate_model(req.model)
        chat_req = ChatCompletionRequest(
            model=req.model,
            messages=[Message(role="user", content=req.prompt)],
            stream=req.stream,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
        if chat_req.stream:
            return StreamingResponse(
                _stream_chat_response(chat_req),
                media_type="text/event-stream",
            )
        job_id, events = await _run_chat(req=chat_req)
        content = _assemble_content(events)
        prompt_tokens = _approx_tokens(req.prompt)
        completion_tokens = _approx_tokens(content)
        return {
            "id": f"cmpl-{job_id}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "text": content,
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    return router
