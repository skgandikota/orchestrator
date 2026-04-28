"""Intent classifier — auto-routes a user message to a pipeline class.

Every job starts here. The classifier decides whether the request should be
handled by the ``fast``, ``deep``, ``research``, or ``status`` pipeline. Two
tiers of work happen, in order:

1. A cheap regex pre-filter short-circuits obvious status queries (``status``,
   ``what's happening``, ``progress``, ``where are we``) without invoking any
   model. The hit returns ``confidence=1.0`` and ``reason="regex pre-filter"``.
2. Anything else is sent to the resident reasoning model (``qwen2.5:7b`` via
   :class:`OllamaClient`) using structured output. The prompt lives at
   ``orchestrator/prompts/classify.md`` and is loaded once at import time.

The model call is wrapped in a single retry: if the first attempt yields
malformed JSON or a payload that does not validate against
:class:`ClassifyResult`, the call is repeated once. A second failure yields
the deterministic fallback ``ClassifyResult(class_="deep", confidence=0.0,
reason="classifier fallback")`` so callers never see an exception from this
function.

Every decision (pre-filter hit, model success, retry, fallback) is recorded
on the optional :class:`StateRecorder`. Persisting to job state is the
*caller's* concern in production; this module just hands the recorder a
single :class:`ClassifyResult` per call so #38's eval harness can grade live
behaviour.

The module is deliberately pure: no scheduler integration, no tool dispatch,
no audit-log import. The :class:`OllamaClient` is a :class:`Protocol` so the
real Ollama adapter (#35) and tests can both satisfy it without a circular
import.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "ClassName",
    "ClassifyResult",
    "OllamaClient",
    "StateRecorder",
    "classify",
]

ClassName = Literal["fast", "deep", "research", "status"]

_MODEL = "qwen2.5:7b"
_MAX_ATTEMPTS = 2
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "classify.md"


class ClassifyResult(BaseModel):
    """Structured output of the classifier.

    The class field is named ``class_`` in Python (``class`` is a reserved
    keyword) but serialises as ``class`` so the JSON sent to and received
    from the model uses the natural key.
    """

    model_config = ConfigDict(populate_by_name=True)

    class_: ClassName = Field(alias="class")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class OllamaClient(Protocol):
    """Minimal contract the classifier needs from the Ollama adapter (#35)."""

    def structured(
        self,
        *,
        model: str,
        schema: type[BaseModel],
        prompt: str,
    ) -> Awaitable[Any]:  # pragma: no cover - protocol
        ...


class StateRecorder(Protocol):
    """Contract for the Phase 1 state module (#32) used to log decisions."""

    def record_classification(
        self, user_msg: str, result: ClassifyResult
    ) -> None:  # pragma: no cover - protocol
        ...


# Regex pre-filter table. Adding a new shortcut is one line.
_REGEX_RULES: tuple[tuple[re.Pattern[str], ClassName], ...] = (
    (
        re.compile(
            r"^\s*(status|what'?s? happening|progress|where are we)\b",
            re.IGNORECASE,
        ),
        "status",
    ),
)


@lru_cache(maxsize=1)
def _prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _render_prompt(user_msg: str) -> str:
    return _prompt_template().replace("{{user_msg}}", user_msg)


def _coerce(raw: Any) -> ClassifyResult:
    """Validate a raw model payload (str/bytes/dict) into a ClassifyResult."""
    if isinstance(raw, ClassifyResult):
        return raw
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError(f"unexpected payload type: {type(raw).__name__}")
    return ClassifyResult.model_validate(raw)


def _fallback() -> ClassifyResult:
    return ClassifyResult(class_="deep", confidence=0.0, reason="classifier fallback")


async def classify(
    user_msg: str,
    *,
    ollama: OllamaClient,
    recorder: StateRecorder | None = None,
) -> ClassifyResult:
    """Classify ``user_msg`` into one of ``fast``/``deep``/``research``/``status``.

    The function never raises on a model failure: malformed output triggers a
    single retry, then a deterministic fallback. The optional ``recorder`` is
    notified exactly once per call with the final :class:`ClassifyResult`.
    """

    # Tier 1 — regex pre-filter.
    for pattern, class_name in _REGEX_RULES:
        if pattern.search(user_msg):
            result = ClassifyResult(
                class_=class_name,
                confidence=1.0,
                reason="regex pre-filter",
            )
            if recorder is not None:
                recorder.record_classification(user_msg, result)
            return result

    # Tier 2 — resident reasoning model with one retry.
    prompt = _render_prompt(user_msg)
    result: ClassifyResult | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        try:
            raw = await ollama.structured(model=_MODEL, schema=ClassifyResult, prompt=prompt)
            result = _coerce(raw)
            break
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
            result = None

    if result is None:
        result = _fallback()

    if recorder is not None:
        recorder.record_classification(user_msg, result)
    return result
