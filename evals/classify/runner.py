"""Eval harness that scores an intent classifier against a golden set.

The harness is intentionally decoupled from the production classifier:
callers pass any ``ClassifierFn`` (a sync ``str -> str`` callable) and the
runner produces an :class:`ClassifyEvalReport` with overall accuracy and
per-intent precision and recall. This keeps the harness CI-safe (no live
LLM required) and makes it easy to plug in mocks in tests.

Datasets are stored as JSONL where each line is a JSON object with at
least ``prompt`` and ``expected`` keys plus optional ``notes``.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

ClassifierFn = Callable[[str], str]
"""A synchronous classifier: takes a user prompt, returns an intent label."""


@dataclass(frozen=True)
class GoldenCase:
    """A single labeled prompt from the golden dataset."""

    prompt: str
    expected: str
    notes: str = ""


@dataclass(frozen=True)
class IntentMetrics:
    """Per-intent precision/recall/F1 with raw confusion counts."""

    intent: str
    support: int
    predicted: int
    true_positive: int
    false_positive: int
    false_negative: int
    precision: float
    recall: float
    f1: float


@dataclass
class ClassifyEvalReport:
    """Aggregate report for a classifier eval run."""

    total: int
    correct: int
    accuracy: float
    per_intent: dict[str, IntentMetrics]
    confusion: dict[str, dict[str, int]]
    misclassified: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "correct": self.correct,
            "accuracy": self.accuracy,
            "per_intent": {k: asdict(v) for k, v in self.per_intent.items()},
            "confusion": self.confusion,
            "misclassified": self.misclassified,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def load_golden(path: str | Path) -> list[GoldenCase]:
    """Load a JSONL golden file into :class:`GoldenCase` records."""

    p = Path(path)
    cases: list[GoldenCase] = []
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        record = json.loads(line)
        if not isinstance(record, Mapping):
            raise ValueError(f"each line must be a JSON object; got {type(record).__name__}")
        if "prompt" not in record or "expected" not in record:
            raise ValueError("each record requires 'prompt' and 'expected' fields")
        cases.append(
            GoldenCase(
                prompt=str(record["prompt"]),
                expected=str(record["expected"]),
                notes=str(record.get("notes", "")),
            )
        )
    if not cases:
        raise ValueError(f"golden file {p} is empty")
    return cases


def _safe_div(num: float, denom: float) -> float:
    return num / denom if denom else 0.0


@dataclass
class ClassifyEvalRunner:
    """Runs a :class:`ClassifierFn` against a list of golden cases."""

    classifier: ClassifierFn

    def run(self, cases: Iterable[GoldenCase]) -> ClassifyEvalReport:
        cases_list = list(cases)
        if not cases_list:
            raise ValueError("at least one case is required")

        labels: set[str] = set()
        confusion: dict[str, dict[str, int]] = {}
        true_pos: Counter[str] = Counter()
        false_pos: Counter[str] = Counter()
        false_neg: Counter[str] = Counter()
        support: Counter[str] = Counter()
        predicted: Counter[str] = Counter()
        misclassified: list[dict[str, str]] = []
        correct = 0

        for case in cases_list:
            actual = self.classifier(case.prompt)
            labels.add(case.expected)
            labels.add(actual)
            support[case.expected] += 1
            predicted[actual] += 1
            row = confusion.setdefault(case.expected, {})
            row[actual] = row.get(actual, 0) + 1
            if actual == case.expected:
                correct += 1
                true_pos[case.expected] += 1
            else:
                false_neg[case.expected] += 1
                false_pos[actual] += 1
                misclassified.append(
                    {
                        "prompt": case.prompt,
                        "expected": case.expected,
                        "actual": actual,
                    }
                )

        per_intent: dict[str, IntentMetrics] = {}
        for label in sorted(labels):
            tp = true_pos[label]
            fp = false_pos[label]
            fn = false_neg[label]
            precision = _safe_div(tp, tp + fp)
            recall = _safe_div(tp, tp + fn)
            f1 = _safe_div(2 * precision * recall, precision + recall)
            per_intent[label] = IntentMetrics(
                intent=label,
                support=support[label],
                predicted=predicted[label],
                true_positive=tp,
                false_positive=fp,
                false_negative=fn,
                precision=precision,
                recall=recall,
                f1=f1,
            )

        total = len(cases_list)
        return ClassifyEvalReport(
            total=total,
            correct=correct,
            accuracy=_safe_div(correct, total),
            per_intent=per_intent,
            confusion=confusion,
            misclassified=misclassified,
        )


def run_classifier_eval(
    classifier: ClassifierFn,
    suite_path: str | Path,
) -> ClassifyEvalReport:
    """Convenience wrapper: load ``suite_path`` and run ``classifier``."""

    return ClassifyEvalRunner(classifier=classifier).run(load_golden(suite_path))


def format_report(report: ClassifyEvalReport) -> str:
    """Human-readable text report (used by the CLI)."""

    lines = [
        f"accuracy: {report.accuracy:.3f}  ({report.correct}/{report.total})",
        "",
        f"{'intent':<12}{'support':>9}{'precision':>12}{'recall':>10}{'f1':>8}",
        "-" * 51,
    ]
    for metrics in report.per_intent.values():
        lines.append(
            f"{metrics.intent:<12}{metrics.support:>9}"
            f"{metrics.precision:>12.3f}{metrics.recall:>10.3f}{metrics.f1:>8.3f}"
        )
    if report.misclassified:
        lines.append("")
        lines.append("misclassified:")
        for entry in report.misclassified:
            lines.append(
                f"  expected={entry['expected']:<8} actual={entry['actual']:<8} "
                f"prompt={entry['prompt']!r}"
            )
    return "\n".join(lines) + "\n"
