"""Tests for the classifier eval harness.

These tests use a mocked classifier to verify the harness math: overall
accuracy, per-intent precision/recall/F1, the confusion matrix, and the
misclassified-row capture. The CLI entrypoint is exercised with a stub
classifier so we never hit a live model.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evals.classify import (
    ClassifyEvalReport,
    ClassifyEvalRunner,
    GoldenCase,
    load_golden,
    run_classifier_eval,
)
from evals.classify.__main__ import main, stub_classifier
from evals.classify.runner import format_report

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "evals" / "classify" / "golden.jsonl"


def _make_cases() -> list[GoldenCase]:
    # 6 cases, two per "expected" label: fast, deep, status
    return [
        GoldenCase(prompt="p-fast-1", expected="fast"),
        GoldenCase(prompt="p-fast-2", expected="fast"),
        GoldenCase(prompt="p-deep-1", expected="deep"),
        GoldenCase(prompt="p-deep-2", expected="deep"),
        GoldenCase(prompt="p-status-1", expected="status"),
        GoldenCase(prompt="p-status-2", expected="status"),
    ]


def _scripted_classifier(mapping: dict[str, str]):
    fn = MagicMock(side_effect=lambda prompt: mapping[prompt])
    return fn


def test_perfect_classifier_yields_full_marks() -> None:
    cases = _make_cases()
    fn = _scripted_classifier({c.prompt: c.expected for c in cases})

    report = ClassifyEvalRunner(classifier=fn).run(cases)

    assert report.total == 6
    assert report.correct == 6
    assert report.accuracy == 1.0
    assert report.misclassified == []
    for label in ("fast", "deep", "status"):
        m = report.per_intent[label]
        assert m.support == 2
        assert m.predicted == 2
        assert m.precision == 1.0
        assert m.recall == 1.0
        assert m.f1 == 1.0
    assert fn.call_count == 6


def test_metrics_with_mixed_predictions() -> None:
    cases = _make_cases()
    # Flip one fast -> deep (false negative for fast, false positive for deep)
    # and one status -> fast (FN status, FP fast)
    predictions = {
        "p-fast-1": "fast",
        "p-fast-2": "deep",
        "p-deep-1": "deep",
        "p-deep-2": "deep",
        "p-status-1": "status",
        "p-status-2": "fast",
    }
    fn = _scripted_classifier(predictions)

    report = ClassifyEvalRunner(classifier=fn).run(cases)

    # 4/6 correct
    assert report.total == 6
    assert report.correct == 4
    assert math.isclose(report.accuracy, 4 / 6)

    fast = report.per_intent["fast"]
    assert fast.support == 2
    assert fast.true_positive == 1
    assert fast.false_negative == 1  # p-fast-2 -> deep
    assert fast.false_positive == 1  # p-status-2 -> fast
    assert math.isclose(fast.precision, 0.5)
    assert math.isclose(fast.recall, 0.5)
    assert math.isclose(fast.f1, 0.5)

    deep = report.per_intent["deep"]
    assert deep.true_positive == 2
    assert deep.false_positive == 1  # p-fast-2 misrouted to deep
    assert deep.false_negative == 0
    assert math.isclose(deep.precision, 2 / 3)
    assert deep.recall == 1.0

    status = report.per_intent["status"]
    assert status.true_positive == 1
    assert status.false_negative == 1
    assert status.false_positive == 0
    assert status.precision == 1.0
    assert math.isclose(status.recall, 0.5)

    # Confusion matrix shape
    assert report.confusion["fast"] == {"fast": 1, "deep": 1}
    assert report.confusion["deep"] == {"deep": 2}
    assert report.confusion["status"] == {"status": 1, "fast": 1}

    # Misclassified rows captured with prompt+expected+actual
    assert {(m["prompt"], m["expected"], m["actual"]) for m in report.misclassified} == {
        ("p-fast-2", "fast", "deep"),
        ("p-status-2", "status", "fast"),
    }


def test_unseen_predicted_label_gets_metrics_row() -> None:
    cases = [GoldenCase(prompt="x", expected="fast")]
    fn = _scripted_classifier({"x": "research"})

    report = ClassifyEvalRunner(classifier=fn).run(cases)

    assert report.accuracy == 0.0
    assert "research" in report.per_intent
    research = report.per_intent["research"]
    assert research.support == 0
    assert research.predicted == 1
    assert research.precision == 0.0
    # recall is 0 because there is no support: safe-divide returns 0
    assert research.recall == 0.0
    assert research.f1 == 0.0


def test_runner_rejects_empty_iterable() -> None:
    fn = _scripted_classifier({})
    with pytest.raises(ValueError):
        ClassifyEvalRunner(classifier=fn).run([])


def test_load_golden_skips_blank_and_comment_lines(tmp_path: Path) -> None:
    suite = tmp_path / "g.jsonl"
    suite.write_text(
        "\n".join(
            [
                "# header comment",
                "",
                json.dumps({"prompt": "hello", "expected": "fast"}),
                json.dumps({"prompt": "deep dive", "expected": "deep", "notes": "n"}),
            ]
        ),
        encoding="utf-8",
    )

    cases = load_golden(suite)

    assert len(cases) == 2
    assert cases[0].prompt == "hello"
    assert cases[1].notes == "n"


def test_load_golden_rejects_missing_fields(tmp_path: Path) -> None:
    suite = tmp_path / "bad.jsonl"
    suite.write_text(json.dumps({"prompt": "x"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"prompt.*expected"):
        load_golden(suite)


def test_load_golden_rejects_non_object_line(tmp_path: Path) -> None:
    suite = tmp_path / "bad.jsonl"
    suite.write_text(json.dumps(["nope"]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_golden(suite)


def test_load_golden_rejects_empty_file(tmp_path: Path) -> None:
    suite = tmp_path / "empty.jsonl"
    suite.write_text("# only comments\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_golden(suite)


def test_run_classifier_eval_reads_jsonl(tmp_path: Path) -> None:
    suite = tmp_path / "g.jsonl"
    suite.write_text(json.dumps({"prompt": "p1", "expected": "fast"}) + "\n", encoding="utf-8")
    fn = _scripted_classifier({"p1": "fast"})
    report = run_classifier_eval(fn, suite)
    assert report.accuracy == 1.0


def test_report_to_json_round_trips() -> None:
    cases = _make_cases()
    fn = _scripted_classifier({c.prompt: c.expected for c in cases})
    report: ClassifyEvalReport = ClassifyEvalRunner(classifier=fn).run(cases)
    payload = json.loads(report.to_json())
    assert payload["accuracy"] == 1.0
    assert "fast" in payload["per_intent"]
    assert payload["per_intent"]["fast"]["precision"] == 1.0


def test_format_report_includes_misclassified_rows() -> None:
    cases = [
        GoldenCase(prompt="p1", expected="fast"),
        GoldenCase(prompt="p2", expected="deep"),
    ]
    fn = _scripted_classifier({"p1": "deep", "p2": "deep"})
    report = ClassifyEvalRunner(classifier=fn).run(cases)
    text = format_report(report)
    assert "accuracy:" in text
    assert "misclassified:" in text
    assert "expected=fast" in text


def test_format_report_omits_misclassified_section_when_perfect() -> None:
    cases = [GoldenCase(prompt="p1", expected="fast")]
    fn = _scripted_classifier({"p1": "fast"})
    report = ClassifyEvalRunner(classifier=fn).run(cases)
    text = format_report(report)
    assert "misclassified:" not in text


def test_cli_main_prints_text_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite = tmp_path / "g.jsonl"
    suite.write_text(
        json.dumps({"prompt": "what's the status?", "expected": "status"}) + "\n",
        encoding="utf-8",
    )

    rc = main(["--suite", str(suite)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "accuracy:" in out


def test_cli_main_emits_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suite = tmp_path / "g.jsonl"
    suite.write_text(
        json.dumps({"prompt": "what's the status?", "expected": "status"}) + "\n",
        encoding="utf-8",
    )

    rc = main(["--suite", str(suite), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["accuracy"] == 1.0


def test_cli_main_exits_nonzero_below_threshold(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite = tmp_path / "g.jsonl"
    suite.write_text(
        json.dumps({"prompt": "what's the status?", "expected": "fast"}) + "\n",
        encoding="utf-8",
    )

    rc = main(["--suite", str(suite), "--min-accuracy", "0.99"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "below threshold" in err


def test_cli_main_accepts_injected_classifier(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite = tmp_path / "g.jsonl"
    suite.write_text(
        json.dumps({"prompt": "anything", "expected": "deep"}) + "\n",
        encoding="utf-8",
    )
    fn = MagicMock(return_value="deep")

    rc = main(["--suite", str(suite)], classifier=fn)

    assert rc == 0
    fn.assert_called_once_with("anything")


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("what is the status of x?", "status"),
        ("research the best embeddings model", "research"),
        ("refactor the queue worker", "deep"),
        ("capital of France", "fast"),
    ],
)
def test_stub_classifier_branches(prompt: str, expected: str) -> None:
    assert stub_classifier(prompt) == expected


def test_golden_dataset_balanced_and_complete() -> None:
    cases = load_golden(GOLDEN_PATH)
    assert len(cases) >= 30
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.expected] = counts.get(case.expected, 0) + 1
    # Every classifier label must be represented.
    for label in ("fast", "deep", "research", "status"):
        assert counts.get(label, 0) >= 6, f"label {label} underrepresented: {counts}"
