"""Refine step: rewrite a consolidated brief into a frontier-model prompt.

The frontier model (e.g. Claude Opus, GPT-5) is expensive, so we burn local
compute on a resident reasoning model (default: ``qwen2.5:7b`` via Ollama)
to produce the highest-quality prompt we can before spending a frontier call.

The control flow lives here in Python; the prompt template at
``orchestrator/prompts/refine.md`` is versioned and declarative.

The step is idempotent: given the same brief and a deterministic
``ModelClient`` (``temperature=0``), re-running ``refine`` produces a
semantically equivalent ``RefinedPrompt``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

PROMPT_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "prompts" / "refine.md"
PROMPT_TEMPLATE_VERSION = 1
DEFAULT_MODEL = "qwen2.5:7b"

ResponseFormat = Literal["markdown", "json", "code", "text"]
ProviderHint = Literal["anthropic", "openai", "google", "local"]


class RefineError(RuntimeError):
    """Raised when the reasoning model fails to produce a valid RefinedPrompt.

    Callers (e.g. the job runner) may downgrade to using the user message
    verbatim as the frontier-model prompt rather than failing the whole job.
    """


@runtime_checkable
class ConsolidatedBrief(Protocol):
    """Structural type for the consolidate-step output.

    The real ``ConsolidatedBrief`` Pydantic model lives in a sibling module
    (issue #39); we depend on the *shape* only so this step can be tested
    and shipped independently.
    """

    intent: str
    goals: Sequence[str]
    constraints: Sequence[str]
    examples: Sequence[str]
    workspace_files: Sequence[str]


@runtime_checkable
class ModelClient(Protocol):
    """Minimal generate-only interface to a local reasoning model."""

    def generate(self, *, model: str, prompt: str, temperature: float) -> str:
        """Return the model's completion as a single string."""
        ...


class RefinedPrompt(BaseModel):
    """High-quality prompt ready to send to a frontier model."""

    system: str = Field(min_length=1)
    user: str = Field(min_length=1)
    response_format: ResponseFormat = "markdown"
    max_tokens: int = Field(default=2048, ge=256, le=8192)
    recommended_provider: ProviderHint = "anthropic"
    template_version: int = PROMPT_TEMPLATE_VERSION


def _brief_to_dict(brief: ConsolidatedBrief) -> dict[str, object]:
    return {
        "intent": brief.intent,
        "goals": list(brief.goals),
        "constraints": list(brief.constraints),
        "examples": list(brief.examples),
        "workspace_files": list(brief.workspace_files),
    }


def _render_template(brief: ConsolidatedBrief) -> str:
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    data = _brief_to_dict(brief)
    examples = list(brief.examples)
    examples_block = (
        "\n".join(f"- {ex}" for ex in examples) if examples else "(none provided)"
    )
    files = list(brief.workspace_files)
    files_block = (
        "\n".join(f"- {f}" for f in files) if files else "(none provided)"
    )
    brief_json = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    return (
        template.replace("{brief_json}", brief_json)
        .replace("{examples_block}", examples_block)
        .replace("{files_block}", files_block)
    )


def _parse_response(raw: str) -> RefinedPrompt:
    text = raw.strip()
    if text.startswith("```"):
        # strip leading ```json / ``` and trailing ```
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RefineError(f"reasoning model returned non-JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RefineError("reasoning model returned non-object JSON")
    try:
        return RefinedPrompt.model_validate(payload)
    except ValidationError as exc:
        raise RefineError(f"reasoning model output failed schema: {exc}") from exc


def refine(
    brief: ConsolidatedBrief,
    *,
    client: ModelClient,
    model: str = DEFAULT_MODEL,
) -> RefinedPrompt:
    """Run the refine pipeline step.

    Calls the reasoning ``client`` with the rendered prompt template and
    parses the response into a ``RefinedPrompt``. Retries once on
    schema-validation failure; a second failure raises ``RefineError``.

    Idempotent given a deterministic client (``temperature=0``).
    """
    prompt = _render_template(brief)
    last_error: RefineError | None = None
    for _ in range(2):
        raw = client.generate(model=model, prompt=prompt, temperature=0.0)
        try:
            return _parse_response(raw)
        except RefineError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error
