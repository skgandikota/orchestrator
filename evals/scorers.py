"""Pluggable scorers for the eval harness."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import jsonschema

if TYPE_CHECKING:
    from evals.runner import EvalCase, ModelResponse


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of running a single scorer against a case/response."""

    passed: bool
    detail: str = ""


@runtime_checkable
class Scorer(Protocol):
    """Pluggable scoring interface."""

    name: str

    def score(
        self, case: EvalCase, response: ModelResponse
    ) -> ScoreResult:  # pragma: no cover - protocol
        ...


@dataclass
class SubstringScorer:
    """Verifies expected and forbidden substrings."""

    name: str = "substring"

    def score(self, case: EvalCase, response: ModelResponse) -> ScoreResult:
        text = response.text
        missing = [s for s in case.expected_substrings if s not in text]
        leaked = [s for s in case.forbidden_substrings if s in text]
        if missing:
            return ScoreResult(False, f"missing substrings: {missing}")
        if leaked:
            return ScoreResult(False, f"forbidden substrings present: {leaked}")
        return ScoreResult(True, "ok")


@dataclass
class RegexScorer:
    """Verifies that all ``expected_regex`` patterns match."""

    name: str = "regex"

    def score(self, case: EvalCase, response: ModelResponse) -> ScoreResult:
        unmatched = [p for p in case.expected_regex if not re.search(p, response.text)]
        if unmatched:
            return ScoreResult(False, f"regex did not match: {unmatched}")
        return ScoreResult(True, "ok")


@dataclass
class JsonShapeScorer:
    """Validates the response text against a JSON Schema."""

    name: str = "json_shape"

    def score(self, case: EvalCase, response: ModelResponse) -> ScoreResult:
        if not case.json_schema:
            return ScoreResult(True, "skipped")
        try:
            payload: Any = json.loads(response.text)
        except json.JSONDecodeError as exc:
            return ScoreResult(False, f"invalid JSON: {exc.msg}")
        try:
            jsonschema.validate(payload, case.json_schema)
        except jsonschema.ValidationError as exc:
            return ScoreResult(False, f"schema violation: {exc.message}")
        return ScoreResult(True, "ok")


@dataclass
class ClassificationScorer:
    """Compares ``response.intent`` against ``case.expected_intent``."""

    name: str = "classification"

    def score(self, case: EvalCase, response: ModelResponse) -> ScoreResult:
        if case.expected_intent is None:
            return ScoreResult(True, "skipped")
        if response.intent != case.expected_intent:
            return ScoreResult(
                False,
                f"intent {response.intent!r} != expected {case.expected_intent!r}",
            )
        return ScoreResult(True, "ok")


_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("api_key", re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9]{16,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)


@dataclass
class NoLeakScorer:
    """Fails the case if PII / secret patterns appear in the output."""

    name: str = "no_leak"

    def score(self, case: EvalCase, response: ModelResponse) -> ScoreResult:
        if not case.no_leak:
            return ScoreResult(True, "skipped")
        leaks = [label for label, pat in _LEAK_PATTERNS if pat.search(response.text)]
        if leaks:
            return ScoreResult(False, f"leaked patterns: {leaks}")
        return ScoreResult(True, "ok")


def default_scorers() -> list[Scorer]:
    """The standard scorer pipeline used by :class:`EvalRunner`."""

    return [
        SubstringScorer(),
        RegexScorer(),
        JsonShapeScorer(),
        ClassificationScorer(),
        NoLeakScorer(),
    ]
