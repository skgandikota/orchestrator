"""Compatibility surface mirroring issue #22's ``harness`` module name.

Re-exports the canonical harness types from :mod:`evals.runner` and adds
``run_suite`` for callers that prefer a function-style entry point.
"""

from __future__ import annotations

from pathlib import Path

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

__all__ = [
    "EvalCase",
    "EvalReport",
    "EvalResult",
    "EvalRunner",
    "EvalSuite",
    "FakeModelClient",
    "ModelClient",
    "ModelResponse",
    "load_suite",
    "run_suite",
]


def run_suite(suite_path: str | Path, client: ModelClient | None = None) -> EvalReport:
    """Load ``suite_path`` and execute it, defaulting to a stub client."""

    suite = load_suite(suite_path)
    runner = EvalRunner(client=client or FakeModelClient())
    return runner.run(suite)
