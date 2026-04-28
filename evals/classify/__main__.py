"""CLI entrypoint: ``python -m evals.classify``.

Loads the golden dataset, runs a classifier (a stub keyword classifier by
default so the harness is exercisable in CI without a live model), and
prints the aggregated report. Exits non-zero if accuracy falls below the
configurable threshold.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from evals.classify.runner import (
    ClassifierFn,
    ClassifyEvalReport,
    format_report,
    load_golden,
    run_classifier_eval,
)

_DEFAULT_SUITE = Path(__file__).resolve().parent / "golden.jsonl"


_STATUS_RE = re.compile(
    r"\b(status|progress|update|where are we|what'?s? happening|how is .* going)\b",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"\b(research|investigate|compare|survey|literature|find sources|cite)\b",
    re.IGNORECASE,
)
_DEEP_RE = re.compile(
    r"\b(refactor|design|architecture|implement|analy[sz]e|deep dive|root cause|debug)\b",
    re.IGNORECASE,
)


def stub_classifier(prompt: str) -> str:
    """Tiny keyword classifier used as the default CLI client.

    Real evaluations should pass a wrapper around the production classifier.
    """

    if _STATUS_RE.search(prompt):
        return "status"
    if _RESEARCH_RE.search(prompt):
        return "research"
    if _DEEP_RE.search(prompt):
        return "deep"
    return "fast"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.classify")
    parser.add_argument(
        "--suite",
        type=Path,
        default=_DEFAULT_SUITE,
        help="Path to the JSONL golden dataset.",
    )
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=0.0,
        help="Fail the run if accuracy is below this threshold.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report instead of plain text.",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    classifier: ClassifierFn | None = None,
) -> int:
    args = _parse_args(argv)
    fn = classifier or stub_classifier
    report: ClassifyEvalReport = run_classifier_eval(fn, args.suite)
    if args.json:
        sys.stdout.write(report.to_json() + "\n")
    else:
        sys.stdout.write(format_report(report))
    if report.accuracy < args.min_accuracy:
        sys.stderr.write(
            f"accuracy {report.accuracy:.3f} below threshold {args.min_accuracy:.3f}\n"
        )
        return 1
    return 0


def _entrypoint() -> None:  # pragma: no cover - thin wrapper
    raise SystemExit(main())


# Allow ``load_golden`` symbol re-export for ad hoc CLI scripting.
__all__ = ["load_golden", "main", "stub_classifier"]


if __name__ == "__main__":  # pragma: no cover - module entry
    _entrypoint()
