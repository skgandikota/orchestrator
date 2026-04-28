"""Prompt evaluation harness for the orchestrator project.

This package provides a model-agnostic harness for measuring prompt
quality. It is intentionally decoupled from ``orchestrator.models`` and
``orchestrator.providers`` so that suites can run against any callable
or :class:`ModelClient` implementation (real or stubbed).
"""

from evals.runner import (
    EvalCase,
    EvalReport,
    EvalResult,
    EvalRunner,
    EvalSuite,
    FakeModelClient,
    ModelClient,
    ModelResponse,
    load_suite,
)
from evals.scorers import (
    ClassificationScorer,
    JsonShapeScorer,
    NoLeakScorer,
    RegexScorer,
    Scorer,
    ScoreResult,
    SubstringScorer,
    default_scorers,
)

__all__ = [
    "ClassificationScorer",
    "EvalCase",
    "EvalReport",
    "EvalResult",
    "EvalRunner",
    "EvalSuite",
    "FakeModelClient",
    "JsonShapeScorer",
    "ModelClient",
    "ModelResponse",
    "NoLeakScorer",
    "RegexScorer",
    "ScoreResult",
    "Scorer",
    "SubstringScorer",
    "default_scorers",
    "load_suite",
]
