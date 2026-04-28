"""Tests for the refine pipeline step."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from orchestrator.pipeline import (
    ConsolidatedBrief,
    ModelClient,
    RefinedPrompt,
    RefineError,
    refine,
)
from orchestrator.pipeline.refine import (
    DEFAULT_MODEL,
    PROMPT_TEMPLATE_VERSION,
    _parse_response,
    _render_template,
)


@dataclass
class FakeBrief:
    intent: str = "Refactor the auth module to use JWT."
    goals: list[str] = field(
        default_factory=lambda: ["Replace session cookies", "Keep API stable"]
    )
    constraints: list[str] = field(
        default_factory=lambda: ["No new dependencies", "Python 3.11+"]
    )
    examples: list[str] = field(
        default_factory=lambda: ["login() returns {token, expires_at}"]
    )
    workspace_files: list[str] = field(
        default_factory=lambda: ["src/auth.py", "tests/test_auth.py"]
    )


class ScriptedClient:
    """ModelClient stub that returns a queued sequence of responses."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def generate(self, *, model: str, prompt: str, temperature: float) -> str:
        self.calls.append(
            {"model": model, "prompt": prompt, "temperature": temperature}
        )
        if not self.responses:
            raise AssertionError("no more scripted responses")
        return self.responses.pop(0)


VALID_PAYLOAD = {
    "system": "You are a senior staff engineer.",
    "user": "Refactor auth to JWT, keep API stable, no new deps.",
    "response_format": "code",
    "max_tokens": 4096,
    "recommended_provider": "anthropic",
}


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload)


def test_protocols_runtime_checkable() -> None:
    assert isinstance(FakeBrief(), ConsolidatedBrief)
    assert isinstance(ScriptedClient([]), ModelClient)


def test_render_template_includes_brief_fields() -> None:
    brief = FakeBrief()
    rendered = _render_template(brief)
    assert "Refactor the auth module" in rendered
    assert "src/auth.py" in rendered
    assert "login() returns" in rendered
    # JSON dump should be present
    assert '"intent"' in rendered


def test_render_template_handles_empty_examples_and_files() -> None:
    brief = FakeBrief(examples=[], workspace_files=[])
    rendered = _render_template(brief)
    assert "(none provided)" in rendered


def test_refine_happy_path_returns_refined_prompt() -> None:
    client = ScriptedClient([_json(VALID_PAYLOAD)])
    brief = FakeBrief()

    result = refine(brief, client=client)

    assert isinstance(result, RefinedPrompt)
    assert result.system == VALID_PAYLOAD["system"]
    assert result.user == VALID_PAYLOAD["user"]
    assert result.response_format == "code"
    assert result.max_tokens == 4096
    assert result.recommended_provider == "anthropic"
    assert result.template_version == PROMPT_TEMPLATE_VERSION
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == DEFAULT_MODEL
    assert client.calls[0]["temperature"] == 0.0


def test_refine_strips_markdown_code_fences() -> None:
    fenced = "```json\n" + _json(VALID_PAYLOAD) + "\n```"
    client = ScriptedClient([fenced])
    result = refine(FakeBrief(), client=client)
    assert result.system == VALID_PAYLOAD["system"]


def test_refine_strips_plain_code_fences() -> None:
    fenced = "```\n" + _json(VALID_PAYLOAD) + "\n```"
    client = ScriptedClient([fenced])
    result = refine(FakeBrief(), client=client)
    assert result.user == VALID_PAYLOAD["user"]


def test_refine_retries_once_on_schema_violation() -> None:
    bad = _json({"system": "ok"})  # missing user
    client = ScriptedClient([bad, _json(VALID_PAYLOAD)])

    result = refine(FakeBrief(), client=client)

    assert result.system == VALID_PAYLOAD["system"]
    assert len(client.calls) == 2


def test_refine_retries_once_on_invalid_json() -> None:
    client = ScriptedClient(["not json at all", _json(VALID_PAYLOAD)])
    result = refine(FakeBrief(), client=client)
    assert isinstance(result, RefinedPrompt)
    assert len(client.calls) == 2


def test_refine_raises_after_two_failures() -> None:
    client = ScriptedClient(["nope", "still nope"])
    with pytest.raises(RefineError):
        refine(FakeBrief(), client=client)
    assert len(client.calls) == 2


def test_refine_raises_on_non_object_json() -> None:
    client = ScriptedClient(["[1, 2, 3]", "[4, 5]"])
    with pytest.raises(RefineError, match="non-object"):
        refine(FakeBrief(), client=client)


def test_refine_passes_custom_model_name() -> None:
    client = ScriptedClient([_json(VALID_PAYLOAD)])
    refine(FakeBrief(), client=client, model="llama3:8b")
    assert client.calls[0]["model"] == "llama3:8b"


def test_refine_is_idempotent_for_deterministic_client() -> None:
    payload = _json(VALID_PAYLOAD)
    c1 = ScriptedClient([payload])
    c2 = ScriptedClient([payload])
    r1 = refine(FakeBrief(), client=c1)
    r2 = refine(FakeBrief(), client=c2)
    assert r1.model_dump() == r2.model_dump()


def test_parse_response_rejects_bad_max_tokens() -> None:
    payload = dict(VALID_PAYLOAD, max_tokens=10)
    with pytest.raises(RefineError):
        _parse_response(_json(payload))


def test_parse_response_rejects_unknown_provider() -> None:
    payload = dict(VALID_PAYLOAD, recommended_provider="acme-ai")
    with pytest.raises(RefineError):
        _parse_response(_json(payload))


def test_refined_prompt_defaults() -> None:
    rp = RefinedPrompt(system="s", user="u")
    assert rp.response_format == "markdown"
    assert rp.max_tokens == 2048
    assert rp.recommended_provider == "anthropic"
    assert rp.template_version == PROMPT_TEMPLATE_VERSION
