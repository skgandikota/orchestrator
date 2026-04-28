"""Eval runner with a pluggable :class:`ModelClient` protocol.

The runner reads YAML suites and executes each case against an injected
``ModelClient``. Results aggregate into an :class:`EvalReport` that can
be serialized as JSON or Markdown.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml


@dataclass(frozen=True)
class ModelResponse:
    """Structured response returned by a :class:`ModelClient`."""

    text: str
    intent: str | None = None
    confidence: float = 1.0
    latency_ms: float = 0.0


@runtime_checkable
class ModelClient(Protocol):
    """Minimal model interface consumed by the eval harness."""

    def complete(self, prompt: str) -> ModelResponse:  # pragma: no cover - protocol
        ...


@dataclass
class FakeModelClient:
    """Deterministic stub used by tests and ``--fake-client`` smoke runs.

    The mapping is keyed by the *prompt* and yields a canned response. Any
    prompt not in the mapping falls back to a heuristic echo response: the
    prompt text is returned verbatim, a coarse keyword classifier picks an
    intent, and the original payload is preserved when the prompt parses
    as JSON. This is enough for offline smoke runs to pass against the
    shipped suites without a real model.
    """

    responses: dict[str, ModelResponse] = field(default_factory=dict)
    default_intent: str = "other"
    default_confidence: float = 0.99

    _INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("summarize", ("summar", "tl;dr", "tldr")),
        ("code", ("code", "function", "python", "refactor", "rewrite", "script")),
        ("search", ("how ", "what ", "why ", "where ", "when ", "who ", "?", "capital")),
        ("chat", ("hello", "hi ", "joke", "how are you", "tell me")),
    )

    def complete(self, prompt: str) -> ModelResponse:
        if prompt in self.responses:
            return self.responses[prompt]
        # If the prompt is itself valid JSON, echo it verbatim so JSON
        # shape assertions can be satisfied offline.
        stripped = prompt.strip()
        if stripped.startswith(("{", "[")):
            try:
                import json as _json

                _json.loads(stripped)
                return ModelResponse(
                    text=stripped,
                    intent=self.default_intent,
                    confidence=self.default_confidence,
                    latency_ms=1.0,
                )
            except ValueError:
                pass
        intent = self._infer_intent(prompt)
        return ModelResponse(
            text=prompt,
            intent=intent,
            confidence=self.default_confidence,
            latency_ms=1.0,
        )

    def _infer_intent(self, prompt: str) -> str:
        lowered = prompt.lower()
        for label, keywords in self._INTENT_KEYWORDS:
            if any(kw in lowered for kw in keywords):
                return label
        return self.default_intent


@dataclass
class EvalCase:
    """A single eval test case."""

    name: str
    prompt: str
    expected_substrings: list[str] = field(default_factory=list)
    expected_intent: str | None = None
    forbidden_substrings: list[str] = field(default_factory=list)
    max_latency_ms: float | None = None
    min_confidence: float | None = None
    expected_regex: list[str] = field(default_factory=list)
    json_schema: dict[str, Any] | None = None
    classification_label: str | None = None
    no_leak: bool = False


@dataclass
class EvalSuite:
    """A YAML-loaded eval suite."""

    name: str
    cases: list[EvalCase]
    version: int = 1
    min_pass_rate: float = 1.0


@dataclass
class EvalResult:
    """Per-case outcome with score breakdown."""

    case: EvalCase
    response: ModelResponse
    passed: bool
    score_results: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    """Aggregate report for a suite run."""

    suite: EvalSuite
    results: list[EvalResult]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return self.passed / self.total

    @property
    def meets_threshold(self) -> bool:
        return self.pass_rate >= self.suite.min_pass_rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite.name,
            "version": self.suite.version,
            "passed": self.passed,
            "total": self.total,
            "pass_rate": self.pass_rate,
            "min_pass_rate": self.suite.min_pass_rate,
            "meets_threshold": self.meets_threshold,
            "cases": [
                {
                    "name": r.case.name,
                    "passed": r.passed,
                    "failures": r.failures,
                    "scores": r.score_results,
                    "response": {
                        "text": r.response.text,
                        "intent": r.response.intent,
                        "confidence": r.response.confidence,
                        "latency_ms": r.response.latency_ms,
                    },
                }
                for r in self.results
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        lines = [
            f"# Eval report: {self.suite.name} (v{self.suite.version})",
            "",
            f"- Passed: **{self.passed}/{self.total}** "
            f"({self.pass_rate:.0%}, threshold {self.suite.min_pass_rate:.0%})",
            f"- Meets threshold: **{'yes' if self.meets_threshold else 'no'}**",
            "",
            "| Case | Result | Failures |",
            "| --- | --- | --- |",
        ]
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            failures = "; ".join(r.failures) if r.failures else "-"
            lines.append(f"| {r.case.name} | {status} | {failures} |")
        return "\n".join(lines) + "\n"


def _case_from_dict(data: dict[str, Any]) -> EvalCase:
    return EvalCase(
        name=str(data["name"]),
        prompt=str(data["prompt"]),
        expected_substrings=list(data.get("expected_substrings", []) or []),
        expected_intent=data.get("expected_intent"),
        forbidden_substrings=list(data.get("forbidden_substrings", []) or []),
        max_latency_ms=data.get("max_latency_ms"),
        min_confidence=data.get("min_confidence"),
        expected_regex=list(data.get("expected_regex", []) or []),
        json_schema=data.get("json_schema"),
        classification_label=data.get("classification_label"),
        no_leak=bool(data.get("no_leak", False)),
    )


def load_suite(path: str | Path) -> EvalSuite:
    """Load and validate a YAML suite definition."""

    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Suite {p} must be a YAML mapping")
    cases_raw = raw.get("cases") or []
    if not cases_raw:
        raise ValueError(f"Suite {p} has no cases")
    return EvalSuite(
        name=str(raw.get("name") or p.stem),
        version=int(raw.get("version", 1)),
        min_pass_rate=float(raw.get("min_pass_rate", 1.0)),
        cases=[_case_from_dict(c) for c in cases_raw],
    )


@dataclass
class EvalRunner:
    """Executes an :class:`EvalSuite` against a :class:`ModelClient`."""

    client: ModelClient
    scorers: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.scorers:
            from evals.scorers import default_scorers

            self.scorers = default_scorers()

    def run(self, suite: EvalSuite) -> EvalReport:
        results = [self._run_case(case) for case in suite.cases]
        return EvalReport(suite=suite, results=results)

    def _run_case(self, case: EvalCase) -> EvalResult:
        start = time.perf_counter()
        response = self.client.complete(case.prompt)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if response.latency_ms <= 0.0:
            response = ModelResponse(
                text=response.text,
                intent=response.intent,
                confidence=response.confidence,
                latency_ms=elapsed_ms,
            )

        score_results: list[dict[str, Any]] = []
        failures: list[str] = []
        for scorer in self.scorers:
            sr = scorer.score(case, response)
            score_results.append({"scorer": scorer.name, "passed": sr.passed, "detail": sr.detail})
            if not sr.passed:
                failures.append(f"{scorer.name}: {sr.detail}")

        if case.max_latency_ms is not None and response.latency_ms > case.max_latency_ms:
            failures.append(f"latency: {response.latency_ms:.1f}ms > {case.max_latency_ms:.1f}ms")
        if case.min_confidence is not None and response.confidence < case.min_confidence:
            failures.append(f"confidence: {response.confidence:.2f} < {case.min_confidence:.2f}")

        return EvalResult(
            case=case,
            response=response,
            passed=not failures,
            score_results=score_results,
            failures=failures,
        )
