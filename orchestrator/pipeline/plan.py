"""Pipeline ``plan`` step.

Invokes the *big* AI model (via the :class:`BigModelRouter` protocol) with a
:class:`RefinedPrompt` and parses the response into a JSON-schema-validated
:class:`Plan`. On validation failure the planner retries **once** with a
self-correcting follow-up message; a second failure raises :class:`PlanError`.

The step writes a SQLite checkpoint (``step='plan'``) before returning and,
when streaming is enabled, emits per-step events for UI consumption.

Only this module, ``orchestrator/prompts/plan.md`` and the package
``__init__`` are touched as part of this change.
"""

from __future__ import annotations

import json
import sqlite3
import warnings
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .refine import RefinedPrompt

logger = structlog.get_logger("orchestrator.pipeline.plan")

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "plan.md"

PlanStepKind = Literal["shell", "code", "web", "verify"]


class PlanStep(BaseModel):
    """A single step in a multi-step plan."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    kind: PlanStepKind
    goal: str = Field(..., min_length=1)
    expected_output_shape: str = Field(..., min_length=1)
    required_tools: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(..., ge=0)
    fallback_strategy: str = Field(..., min_length=1)


class Plan(BaseModel):
    """A validated multi-step plan with a single root summary."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=1)
    steps: list[PlanStep] = Field(..., min_length=1)


class PlanError(RuntimeError):
    """Raised when the planner cannot produce a valid plan after one retry.

    The last raw model output is attached as :attr:`last_raw_output` for
    debugging by upstream callers (job runner, CLI, etc.).
    """

    def __init__(self, message: str, *, last_raw_output: str) -> None:
        super().__init__(message)
        self.last_raw_output = last_raw_output


@runtime_checkable
class BigModelRouter(Protocol):
    """Protocol for the *big* AI router used by the plan step.

    Implementations are provider-agnostic (OpenAI, Anthropic, local llama.cpp,
    ...). Only the ``complete`` method is required. Tests provide a fake.
    """

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = ...,
    ) -> str:  # pragma: no cover - protocol definition
        ...


EventHandler = Callable[[dict[str, Any]], None]


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _build_messages(refined: RefinedPrompt) -> list[dict[str, str]]:
    schema = json.dumps(Plan.model_json_schema(), indent=2)
    system = (
        f"{_load_system_prompt()}\n\n"
        f"## Upstream system guidance\n\n{refined.system}\n\n"
        "## JSON Schema (authoritative)\n\n"
        f"```json\n{schema}\n```\n"
    )
    user_payload = {"prompt": refined.user}
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload)},
    ]


def _parse_plan(raw: str) -> Plan:
    """Parse and validate a raw model response into a :class:`Plan`."""
    data = json.loads(raw)
    return Plan.model_validate(data)


def _dedupe_step_ids(plan: Plan) -> Plan:
    """Auto-suffix duplicate step IDs and emit a warning.

    Mirrors the AC: duplicates become ``<id>``, ``<id>_2``, ``<id>_3`` ...
    """
    seen: dict[str, int] = {}
    new_steps: list[PlanStep] = []
    rewrote = False
    for step in plan.steps:
        base = step.id
        count = seen.get(base, 0) + 1
        seen[base] = count
        if count == 1:
            new_steps.append(step)
            continue
        rewrote = True
        new_id = f"{base}_{count}"
        warnings.warn(
            f"plan: duplicate step id {base!r} auto-suffixed to {new_id!r}",
            stacklevel=2,
        )
        logger.warning(
            "plan.duplicate_step_id",
            original_id=base,
            new_id=new_id,
        )
        new_steps.append(step.model_copy(update={"id": new_id}))
    if not rewrote:
        return plan
    return plan.model_copy(update={"steps": new_steps})


def _checkpoint(db_path: str | Path, refined: RefinedPrompt, plan: Plan) -> None:
    """Persist the plan to SQLite under ``step='plan'``."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                step TEXT NOT NULL,
                refined_prompt TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO checkpoints (step, refined_prompt, plan_json) VALUES (?, ?, ?)",
            ("plan", refined.model_dump_json(), plan.model_dump_json()),
        )
        conn.commit()
    finally:
        conn.close()


def _emit_stream(plan: Plan, handler: EventHandler) -> None:
    handler({"event": "plan.started", "summary": plan.summary, "step_count": len(plan.steps)})
    for index, step in enumerate(plan.steps):
        handler({"event": "plan.step", "index": index, "step": step.model_dump()})
    handler({"event": "plan.completed", "step_count": len(plan.steps)})


def plan(
    refined: RefinedPrompt,
    *,
    big_ai: BigModelRouter,
    checkpoint_db: str | Path | None = None,
    stream: bool = False,
    event_handler: EventHandler | None = None,
) -> Plan:
    """Produce a validated :class:`Plan` from a :class:`RefinedPrompt`.

    Parameters
    ----------
    refined:
        Output of the ``refine`` step.
    big_ai:
        A :class:`BigModelRouter` implementation. Tests pass a fake.
    checkpoint_db:
        Optional path to a SQLite database. When given, the validated plan is
        persisted under ``step='plan'`` before returning.
    stream:
        When ``True``, emits per-step events via ``event_handler``.
    event_handler:
        Required when ``stream=True``. Called once with ``plan.started``,
        once per step with ``plan.step``, and once with ``plan.completed``.
    """
    if stream and event_handler is None:
        raise ValueError("event_handler is required when stream=True")

    messages = _build_messages(refined)
    response_format = {"type": "json_object"}

    raw = big_ai.complete(messages, response_format=response_format)
    try:
        parsed = _parse_plan(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        errors: Iterable[Any]
        if isinstance(exc, ValidationError):
            errors = exc.errors()
        else:
            errors = [{"type": "json_decode", "msg": str(exc)}]
        logger.warning("plan.validation_failed", errors=list(errors))
        correction = (
            "Your previous output failed validation: "
            f"{json.dumps(list(errors), default=str)}. "
            "Return ONLY a JSON object that matches the embedded schema. "
            "No prose, no markdown fences."
        )
        retry_messages = [
            *messages,
            {"role": "assistant", "content": raw},
            {"role": "user", "content": correction},
        ]
        retry_raw = big_ai.complete(retry_messages, response_format=response_format)
        try:
            parsed = _parse_plan(retry_raw)
        except (json.JSONDecodeError, ValidationError) as retry_exc:
            logger.error("plan.retry_failed", error=str(retry_exc))
            raise PlanError(
                f"plan validation failed after retry: {retry_exc}",
                last_raw_output=retry_raw,
            ) from retry_exc

    parsed = _dedupe_step_ids(parsed)

    if checkpoint_db is not None:
        _checkpoint(checkpoint_db, refined, parsed)

    if stream and event_handler is not None:
        _emit_stream(parsed, event_handler)

    return parsed


__all__ = [
    "BigModelRouter",
    "EventHandler",
    "Plan",
    "PlanError",
    "PlanStep",
    "PlanStepKind",
    "RefinedPrompt",
    "plan",
]
