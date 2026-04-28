"""Tests for the pipeline ``verify`` step."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from orchestrator.pipeline import (
    ExecutableStep,
    OllamaClient,
    Plan,
    PlanStep,
    StateRecorder,
    VerifyDecision,
    verify,
)
from orchestrator.pipeline.verify import (
    _MODEL,
    _fail_open,
    _format_remaining,
    _prompt_template,
    _render_prompt,
)


def _step(**overrides: Any) -> ExecutableStep:
    base = {
        "id": "s1",
        "description": "Run unit tests",
        "expected_outcome": "All tests pass",
        "actual_output": "Ran 12 tests, 12 passed",
    }
    base.update(overrides)
    return ExecutableStep(**base)


def _plan(*, with_pending: bool = True) -> Plan:
    steps = [
        PlanStep(id="s1", description="Run unit tests", status="done"),
    ]
    if with_pending:
        steps.append(PlanStep(id="s2", description="Deploy to staging", status="pending"))
        steps.append(PlanStep(id="s3", description="Notify team", status="pending"))
    return Plan(goal="ship feature x", steps=steps)


class FakeOllama:
    """Configurable fake :class:`OllamaClient` returning canned payloads."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def structured(
        self,
        *,
        model: str,
        schema: type[BaseModel],
        prompt: str,
    ) -> Any:
        self.calls.append({"model": model, "schema": schema, "prompt": prompt})
        if not self._responses:
            raise AssertionError("FakeOllama ran out of canned responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[ExecutableStep, VerifyDecision]] = []

    def record_verify(self, step: ExecutableStep, decision: VerifyDecision) -> None:
        self.calls.append((step, decision))


# ---------------------------------------------------------------------------
# Action coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,reason,hint",
    [
        ("continue", "step met its expected outcome", "read files in parallel"),
        ("replan", "build error unrelated to remaining steps", None),
        ("done", "user goal already met", None),
    ],
)
@pytest.mark.asyncio
async def test_verify_returns_each_action(
    action: str, reason: str, hint: str | None
) -> None:
    payload = {"action": action, "reason": reason, "next_step_hint": hint}
    ollama = FakeOllama([json.dumps(payload)])
    recorder = FakeRecorder()

    decision = await verify(_step(), plan=_plan(), ollama=ollama, recorder=recorder)

    assert isinstance(decision, VerifyDecision)
    assert decision.action == action
    assert decision.reason == reason
    assert decision.next_step_hint == hint
    assert len(ollama.calls) == 1
    assert ollama.calls[0]["model"] == _MODEL
    assert ollama.calls[0]["schema"] is VerifyDecision
    assert recorder.calls[0][0].id == "s1"
    assert recorder.calls[0][1] is decision


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_violation_retries_once_then_succeeds() -> None:
    bad = json.dumps({"action": "maybe", "reason": "bad enum"})  # invalid action
    good = json.dumps(
        {"action": "continue", "reason": "ok now", "next_step_hint": None}
    )
    ollama = FakeOllama([bad, good])

    decision = await verify(_step(), plan=_plan(), ollama=ollama)

    assert decision.action == "continue"
    assert decision.reason == "ok now"
    assert len(ollama.calls) == 2


@pytest.mark.asyncio
async def test_json_decode_error_retries_once_then_succeeds() -> None:
    good = json.dumps(
        {"action": "done", "reason": "all green", "next_step_hint": None}
    )
    ollama = FakeOllama(["not json {", good])

    decision = await verify(_step(), plan=_plan(), ollama=ollama)

    assert decision.action == "done"
    assert len(ollama.calls) == 2


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_fail_open_decision() -> None:
    ollama = FakeOllama(["nope", json.dumps({"action": "??", "reason": ""})])
    recorder = FakeRecorder()

    decision = await verify(_step(), plan=_plan(), ollama=ollama, recorder=recorder)

    assert decision == _fail_open()
    assert decision.action == "continue"
    assert decision.reason == "verifier failed open"
    assert decision.next_step_hint is None
    assert len(ollama.calls) == 2
    assert len(recorder.calls) == 1
    assert recorder.calls[0][1] is decision


@pytest.mark.asyncio
async def test_unexpected_payload_type_falls_through_to_fail_open() -> None:
    # _coerce raises ValueError for non-dict, non-str, non-bytes payloads.
    ollama = FakeOllama([12345, [1, 2, 3]])

    decision = await verify(_step(), plan=_plan(), ollama=ollama)

    assert decision == _fail_open()
    assert len(ollama.calls) == 2


@pytest.mark.asyncio
async def test_transport_error_is_treated_as_validation_failure() -> None:
    good = json.dumps(
        {"action": "continue", "reason": "fine", "next_step_hint": None}
    )
    ollama = FakeOllama([TypeError("boom"), good])

    decision = await verify(_step(), plan=_plan(), ollama=ollama)

    assert decision.action == "continue"
    assert len(ollama.calls) == 2


# ---------------------------------------------------------------------------
# Checkpointing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_written_before_return_on_success() -> None:
    payload = {"action": "continue", "reason": "ok", "next_step_hint": "go"}
    ollama = FakeOllama([json.dumps(payload)])
    recorder = FakeRecorder()

    decision = await verify(_step(), plan=_plan(), ollama=ollama, recorder=recorder)

    assert len(recorder.calls) == 1
    recorded_step, recorded_decision = recorder.calls[0]
    assert recorded_step.id == "s1"
    assert recorded_decision is decision


@pytest.mark.asyncio
async def test_checkpoint_written_on_fail_open() -> None:
    ollama = FakeOllama(["bad", "still bad"])
    recorder = FakeRecorder()

    decision = await verify(_step(), plan=_plan(), ollama=ollama, recorder=recorder)

    assert decision == _fail_open()
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_no_recorder_is_supported() -> None:
    payload = {"action": "done", "reason": "fin", "next_step_hint": None}
    ollama = FakeOllama([json.dumps(payload)])

    decision = await verify(_step(), plan=_plan(), ollama=ollama)

    assert decision.action == "done"


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_includes_required_sections() -> None:
    payload = {"action": "continue", "reason": "ok", "next_step_hint": None}
    ollama = FakeOllama([json.dumps(payload)])

    await verify(_step(), plan=_plan(), ollama=ollama)
    prompt = ollama.calls[0]["prompt"]

    assert "Run unit tests" in prompt  # step description
    assert "All tests pass" in prompt  # expected outcome
    assert "Ran 12 tests, 12 passed" in prompt  # actual output
    assert "Deploy to staging" in prompt  # remaining pending step
    assert "Notify team" in prompt
    assert "verify v1" in prompt  # versioned prompt header


def test_render_prompt_excludes_current_step_from_remaining() -> None:
    plan = Plan(
        goal="g",
        steps=[
            PlanStep(id="s1", description="self", status="pending"),
            PlanStep(id="s2", description="other", status="pending"),
        ],
    )
    rendered = _render_prompt(_step(id="s1"), plan)
    assert "other" in rendered
    # The current step id is "s1" and its description "Run unit tests" — its
    # *current* description should still appear (under "Step description"),
    # but the remaining-steps block should not list ``s1``.
    remaining_section = rendered.split("Remaining plan steps", 1)[1]
    assert "- s1:" not in remaining_section
    assert "- s2: other" in remaining_section


def test_format_remaining_returns_none_marker_when_empty() -> None:
    plan = Plan(goal="g", steps=[PlanStep(id="s1", description="d", status="done")])
    assert _format_remaining(plan, "s1") == "(none)"


def test_render_prompt_handles_empty_actual_output() -> None:
    rendered = _render_prompt(_step(actual_output=""), _plan())
    assert "(empty)" in rendered


def test_prompt_template_is_cached() -> None:
    a = _prompt_template()
    b = _prompt_template()
    assert a is b


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_protocols_are_importable() -> None:
    assert OllamaClient is not None
    assert StateRecorder is not None


def test_verify_decision_rejects_invalid_action() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VerifyDecision(action="explode", reason="x", next_step_hint=None)  # type: ignore[arg-type]


def test_verify_decision_requires_non_empty_reason() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VerifyDecision(action="continue", reason="", next_step_hint=None)


def test_coerce_passthrough_for_decision_and_bytes() -> None:
    from orchestrator.pipeline.verify import _coerce

    pre = VerifyDecision(action="done", reason="r", next_step_hint=None)
    assert _coerce(pre) is pre

    payload = {"action": "continue", "reason": "from bytes", "next_step_hint": None}
    raw_bytes = json.dumps(payload).encode("utf-8")
    decoded = _coerce(raw_bytes)
    assert decoded.action == "continue"
    assert decoded.reason == "from bytes"

    raw_bytearray = bytearray(json.dumps(payload), "utf-8")
    assert _coerce(raw_bytearray).reason == "from bytes"


def test_fail_open_shape() -> None:
    d = _fail_open()
    assert d.action == "continue"
    assert d.reason == "verifier failed open"
    assert d.next_step_hint is None
