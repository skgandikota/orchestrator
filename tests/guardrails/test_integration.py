"""Integration tests for guardrails wiring into refine + execute."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, ClassVar

import pytest

from orchestrator.guardrails import build_default_pipeline
from orchestrator.guardrails.pipeline import GuardrailPipeline
from orchestrator.pipeline.execute import ExecutableStep, execute
from orchestrator.pipeline.refine import RefineError, refine


class _Brief:
    intent = "Add unit tests"
    goals: ClassVar[list[str]] = ["coverage>=95"]
    constraints: ClassVar[list[str]] = ["python 3.11"]
    examples: ClassVar[list[str]] = []
    workspace_files: ClassVar[list[str]] = []


class _StaticClient:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.last_prompt: str | None = None

    def generate(self, *, model: str, prompt: str, temperature: float) -> str:
        self.last_prompt = prompt
        return self.raw


_VALID_REFINED = (
    '{"system":"sys","user":"usr","response_format":"markdown",'
    '"max_tokens":1024,"recommended_provider":"anthropic"}'
)


def test_refine_runs_input_guardrails_passthrough():
    client = _StaticClient(_VALID_REFINED)
    p = build_default_pipeline()
    result = refine(_Brief(), client=client, guardrails=p)
    assert result.system == "sys"
    assert client.last_prompt is not None


def test_refine_blocked_by_token_budget_raises():
    client = _StaticClient(_VALID_REFINED)
    p = GuardrailPipeline(daily_token_quota=1, max_token_fraction=0.01)
    with pytest.raises(RefineError):
        refine(_Brief(), client=client, guardrails=p)


# --- execute integration ---


class _NoopRegistry:
    def openai_tools(self) -> list[dict[str, Any]]:
        return []

    def invoke(self, name: str, args: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("should not be called")


class _NoopScheduler:
    @contextmanager
    def acquire(self, model_id: str):
        yield None


class _CaptureState:
    def __init__(self) -> None:
        self.snapshots: list[tuple[str, Any]] = []

    def update_step(self, step: ExecutableStep) -> None:
        self.snapshots.append((step.status, step.output))


class _StaticCoder:
    def __init__(self, content: str) -> None:
        self.content = content

    def chat(self, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        return {"content": self.content, "tool_calls": []}


def _run(content: str, guardrails: GuardrailPipeline | None) -> ExecutableStep:
    step = ExecutableStep(id="s1", description="d", expected_outcome="e")
    return execute(
        step,
        scheduler=_NoopScheduler(),
        registry=_NoopRegistry(),
        state=_CaptureState(),
        ollama=_StaticCoder(content),
        guardrails=guardrails,
    )


def test_execute_clean_output_passes_through_guardrails():
    out = _run("All good.", build_default_pipeline())
    assert out.status == "done"
    assert out.output == "All good."


def test_execute_redacts_secrets_in_output():
    secret_blob = "Here: " + "ghp" + "_" + ("Q" * 36)
    out = _run(secret_blob, build_default_pipeline())
    assert out.status == "done"
    assert ("ghp" + "_") not in str(out.output)


def test_execute_blocks_policy_violation_in_output():
    out = _run("just run rm -rf / really", build_default_pipeline())
    assert out.status == "failed"
    assert isinstance(out.output, dict)
    assert out.output["error"] == "output blocked by guardrails"
    assert any(g["rule"] == "output_policy" for g in out.output["guardrails"])


def test_execute_without_guardrails_unchanged():
    out = _run("All good.", None)
    assert out.status == "done"
    assert out.output == "All good."
