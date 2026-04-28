"""Pipeline ``verify`` step.

The verifier runs after every ``execute`` step and decides what the job
runner should do next:

- ``continue`` — move on to the next pending step;
- ``replan`` — discard the rest of the plan and start over;
- ``done`` — declare the overall job complete.

Implementation details mirror the upstream ``classify`` step:

* The decision is produced by the resident reasoning model
  (``qwen2.5:7b`` by default) via :class:`OllamaClient` with structured
  output. The prompt template lives at
  ``orchestrator/prompts/verify.md`` and is loaded once at import time.
* On a malformed payload (JSON decode error or schema violation) the
  call is retried **once**. A second failure yields a deterministic
  *fail-open* decision — ``VerifyDecision(action="continue",
  reason="verifier failed open", next_step_hint=None)`` — so a flaky
  verifier never halts a job that is otherwise progressing.
* The final decision is checkpointed via :class:`StateRecorder` (with
  ``step="verify"``) **before** being returned. The Phase 5 job runner
  is responsible for honouring ``replan`` decisions by setting all
  remaining steps' status to ``skipped`` — that policy is documented
  here but not implemented in this module.

The module is deliberately self-contained: the upstream :class:`Plan`
and :class:`ExecutableStep` shapes are described as Pydantic models
inline so this file does not depend on any other pipeline module that
may still be landing in a parallel PR.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "ExecutableStep",
    "OllamaClient",
    "Plan",
    "PlanStep",
    "StateRecorder",
    "VerifyAction",
    "VerifyDecision",
    "verify",
]

VerifyAction = Literal["continue", "replan", "done"]

_MODEL = "qwen2.5:7b"
_MAX_ATTEMPTS = 2
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "verify.md"


class VerifyDecision(BaseModel):
    """Structured output of the verifier."""

    model_config = ConfigDict(extra="forbid")

    action: VerifyAction
    reason: str = Field(..., min_length=1)
    next_step_hint: str | None = None


class PlanStep(BaseModel):
    """Minimal plan-step shape used by the verifier prompt builder.

    Defined inline so this module is independent of the ``plan`` step's
    own ``PlanStep`` definition (which may land in a parallel PR).
    """

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    status: Literal["pending", "running", "done", "skipped", "failed"] = "pending"


class ExecutableStep(BaseModel):
    """A step the executor has just run."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    expected_outcome: str = Field(..., min_length=1)
    actual_output: str = ""


class Plan(BaseModel):
    """Minimal plan envelope used by the verifier."""

    model_config = ConfigDict(extra="allow")

    goal: str = Field(..., min_length=1)
    steps: list[PlanStep] = Field(default_factory=list)


class OllamaClient(Protocol):
    """Minimal contract the verifier needs from the Ollama adapter (#35)."""

    def structured(
        self,
        *,
        model: str,
        schema: type[BaseModel],
        prompt: str,
    ) -> Awaitable[Any]:  # pragma: no cover - protocol definition
        ...


class StateRecorder(Protocol):
    """Contract for the Phase 1 state module (#32) used to checkpoint decisions."""

    def record_verify(
        self, step: ExecutableStep, decision: VerifyDecision
    ) -> None:  # pragma: no cover - protocol definition
        ...


@lru_cache(maxsize=1)
def _prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _format_remaining(plan: Plan, current_step_id: str) -> str:
    pending = [s for s in plan.steps if s.status == "pending" and s.id != current_step_id]
    if not pending:
        return "(none)"
    return "\n".join(f"- {s.id}: {s.description}" for s in pending)


def _render_prompt(step: ExecutableStep, plan: Plan) -> str:
    template = _prompt_template()
    return (
        template.replace("{{step_description}}", step.description)
        .replace("{{expected_outcome}}", step.expected_outcome)
        .replace("{{actual_output}}", step.actual_output or "(empty)")
        .replace("{{remaining_steps}}", _format_remaining(plan, step.id))
    )


def _coerce(raw: Any) -> VerifyDecision:
    """Validate a raw model payload (str/bytes/dict/model) into a VerifyDecision."""
    if isinstance(raw, VerifyDecision):
        return raw
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError(f"unexpected payload type: {type(raw).__name__}")
    return VerifyDecision.model_validate(raw)


def _fail_open() -> VerifyDecision:
    return VerifyDecision(
        action="continue",
        reason="verifier failed open",
        next_step_hint=None,
    )


async def verify(
    step: ExecutableStep,
    *,
    plan: Plan,
    ollama: OllamaClient,
    recorder: StateRecorder | None = None,
) -> VerifyDecision:
    """Decide what the job runner should do after ``step`` has executed.

    Parameters
    ----------
    step:
        The just-executed step, including its description, expected
        outcome and actual output.
    plan:
        The current plan. Pending steps (excluding ``step``) are passed
        to the model as remaining-work context so it can suggest a
        ``next_step_hint``.
    ollama:
        Any object satisfying :class:`OllamaClient`. Tests pass a fake
        — there are no live LLM calls in CI.
    recorder:
        Optional :class:`StateRecorder`. When supplied, the final
        decision is checkpointed via ``record_verify`` *before* this
        function returns.

    Notes
    -----
    The function never raises on a model failure: malformed output
    triggers a single retry, then the deterministic fail-open
    ``VerifyDecision(action="continue", reason="verifier failed
    open", next_step_hint=None)``. This is intentional — a noisy
    verifier should not halt a job that is otherwise progressing.

    A ``replan`` decision is advisory: the job runner is responsible
    for setting every remaining step's ``status`` to ``"skipped"``
    before re-entering the planner. That policy lives in the Phase 5
    job runner, not here.
    """

    prompt = _render_prompt(step, plan)
    decision: VerifyDecision | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        try:
            raw = await ollama.structured(model=_MODEL, schema=VerifyDecision, prompt=prompt)
            decision = _coerce(raw)
            break
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
            decision = None

    if decision is None:
        decision = _fail_open()

    if recorder is not None:
        recorder.record_verify(step, decision)
    return decision
