"""Tests for the pipeline plan step."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from coracle.pipeline import (
    BigModelRouter,
    PlanError,
    RefinedPrompt,
    plan,
)
from coracle.pipeline.plan import Plan, PlanStep, _load_system_prompt


def _valid_plan_payload(*, ids: list[str] | None = None) -> dict[str, Any]:
    ids = ids or ["discover", "implement", "verify"]
    kinds = ["shell", "code", "verify"]
    return {
        "summary": "Discover, implement, verify.",
        "steps": [
            {
                "id": step_id,
                "kind": kinds[i % len(kinds)],
                "goal": f"goal-{step_id}",
                "expected_output_shape": "string",
                "required_tools": ["fs"],
                "estimated_tokens": 100,
                "fallback_strategy": "retry once",
            }
            for i, step_id in enumerate(ids)
        ],
    }


class FakeBigAI:
    """Configurable fake :class:`BigModelRouter`."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []
        self.response_formats: list[dict[str, Any] | None] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append(messages)
        self.response_formats.append(response_format)
        if not self._responses:
            raise AssertionError("FakeBigAI ran out of canned responses")
        return self._responses.pop(0)


@pytest.fixture
def refined() -> RefinedPrompt:
    return RefinedPrompt(
        system="You are a planning assistant for project demo.",
        user="ship feature x",
    )


def test_protocol_runtime_check() -> None:
    fake = FakeBigAI([json.dumps(_valid_plan_payload())])
    assert isinstance(fake, BigModelRouter)


def test_plan_returns_validated_plan(refined: RefinedPrompt) -> None:
    fake = FakeBigAI([json.dumps(_valid_plan_payload())])
    result = plan(refined, big_ai=fake)
    assert isinstance(result, Plan)
    assert result.summary == "Discover, implement, verify."
    assert [s.id for s in result.steps] == ["discover", "implement", "verify"]
    assert isinstance(result.steps[0], PlanStep)
    # Single call: no retry path was needed.
    assert len(fake.calls) == 1
    assert fake.response_formats == [{"type": "json_object"}]


def test_plan_embeds_schema_in_system_prompt(refined: RefinedPrompt) -> None:
    fake = FakeBigAI([json.dumps(_valid_plan_payload())])
    plan(refined, big_ai=fake)
    system_message = fake.calls[0][0]
    assert system_message["role"] == "system"
    assert "JSON Schema" in system_message["content"]
    assert "expected_output_shape" in system_message["content"]
    assert _load_system_prompt().splitlines()[0] in system_message["content"]


def test_model_json_schema_exported() -> None:
    schema = Plan.model_json_schema()
    assert schema["type"] == "object"
    assert "steps" in schema["properties"]


def test_retry_succeeds_on_validation_error(refined: RefinedPrompt) -> None:
    bad = json.dumps({"summary": "x", "steps": []})  # empty steps -> validation error
    good = json.dumps(_valid_plan_payload())
    fake = FakeBigAI([bad, good])
    result = plan(refined, big_ai=fake)
    assert len(result.steps) == 3
    assert len(fake.calls) == 2
    correction = fake.calls[1][-1]
    assert correction["role"] == "user"
    assert "failed validation" in correction["content"]


def test_retry_succeeds_on_json_parse_error(refined: RefinedPrompt) -> None:
    bad = "not json at all {"
    good = json.dumps(_valid_plan_payload())
    fake = FakeBigAI([bad, good])
    result = plan(refined, big_ai=fake)
    assert len(result.steps) == 3
    assert len(fake.calls) == 2


def test_retry_exhaustion_raises_plan_error(refined: RefinedPrompt) -> None:
    bad1 = "definitely not json"
    bad2 = json.dumps({"summary": "still bad", "steps": []})
    fake = FakeBigAI([bad1, bad2])
    with pytest.raises(PlanError) as exc_info:
        plan(refined, big_ai=fake)
    assert exc_info.value.last_raw_output == bad2
    assert "validation failed after retry" in str(exc_info.value)


def test_duplicate_ids_are_auto_suffixed(refined: RefinedPrompt) -> None:
    payload = _valid_plan_payload(ids=["s1", "s1", "s1"])
    fake = FakeBigAI([json.dumps(payload)])
    with pytest.warns(UserWarning, match="duplicate step id"):
        result = plan(refined, big_ai=fake)
    assert [s.id for s in result.steps] == ["s1", "s1_2", "s1_3"]


def test_no_dedupe_when_ids_unique(refined: RefinedPrompt) -> None:
    fake = FakeBigAI([json.dumps(_valid_plan_payload())])
    result = plan(refined, big_ai=fake)
    assert [s.id for s in result.steps] == ["discover", "implement", "verify"]


def test_checkpoint_writes_to_sqlite(refined: RefinedPrompt, tmp_path: Path) -> None:
    db = tmp_path / "nested" / "ckpt.db"
    fake = FakeBigAI([json.dumps(_valid_plan_payload())])
    result = plan(refined, big_ai=fake, checkpoint_db=db)
    assert db.exists()
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT step, refined_prompt, plan_json FROM checkpoints").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    step, refined_json, plan_json = rows[0]
    assert step == "plan"
    assert json.loads(refined_json)["user"] == "ship feature x"
    assert json.loads(plan_json) == json.loads(result.model_dump_json())


def test_streaming_emits_step_events(refined: RefinedPrompt) -> None:
    fake = FakeBigAI([json.dumps(_valid_plan_payload())])
    events: list[dict[str, Any]] = []
    plan(refined, big_ai=fake, stream=True, event_handler=events.append)
    kinds = [e["event"] for e in events]
    assert kinds == ["plan.started", "plan.step", "plan.step", "plan.step", "plan.completed"]
    assert events[0]["step_count"] == 3
    assert events[1]["step"]["id"] == "discover"
    assert events[-1]["step_count"] == 3


def test_streaming_requires_handler(refined: RefinedPrompt) -> None:
    fake = FakeBigAI([json.dumps(_valid_plan_payload())])
    with pytest.raises(ValueError, match="event_handler is required"):
        plan(refined, big_ai=fake, stream=True)


def test_plan_error_attaches_raw_output() -> None:
    err = PlanError("boom", last_raw_output="<garbage>")
    assert err.last_raw_output == "<garbage>"
    assert "boom" in str(err)
