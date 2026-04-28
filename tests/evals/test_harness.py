"""Tests for the prompt evaluation harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coracle.prompts import _loader
from evals import (
    ClassificationScorer,
    EvalCase,
    EvalReport,
    EvalRunner,
    EvalSuite,
    FakeModelClient,
    JsonShapeScorer,
    ModelClient,
    ModelResponse,
    NoLeakScorer,
    RegexScorer,
    SubstringScorer,
    cli,
    default_scorers,
    harness,
    load_suite,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITES_DIR = REPO_ROOT / "evals" / "suites"


# ---------------------------------------------------------------------------
# scorers
# ---------------------------------------------------------------------------


def _resp(text: str = "hello world", **kwargs: object) -> ModelResponse:
    return ModelResponse(text=text, **kwargs)  # type: ignore[arg-type]


def test_substring_scorer_pass_and_fail() -> None:
    scorer = SubstringScorer()
    case_ok = EvalCase(name="x", prompt="p", expected_substrings=["hello"])
    assert scorer.score(case_ok, _resp()).passed

    case_missing = EvalCase(name="x", prompt="p", expected_substrings=["nope"])
    res = scorer.score(case_missing, _resp())
    assert not res.passed and "missing" in res.detail

    case_forbidden = EvalCase(name="x", prompt="p", forbidden_substrings=["hello"])
    res = scorer.score(case_forbidden, _resp())
    assert not res.passed and "forbidden" in res.detail


def test_regex_scorer_pass_and_fail() -> None:
    scorer = RegexScorer()
    assert scorer.score(EvalCase(name="x", prompt="p", expected_regex=[r"\w+"]), _resp()).passed
    res = scorer.score(EvalCase(name="x", prompt="p", expected_regex=[r"\d+"]), _resp("abc"))
    assert not res.passed and "regex" in res.detail


def test_json_shape_scorer_paths() -> None:
    scorer = JsonShapeScorer()
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}

    assert scorer.score(EvalCase(name="x", prompt="p"), _resp("{}")).detail == "skipped"

    case = EvalCase(name="x", prompt="p", json_schema=schema)
    assert scorer.score(case, _resp('{"ok": true}')).passed

    bad_json = scorer.score(case, _resp("not-json"))
    assert not bad_json.passed and "invalid JSON" in bad_json.detail

    bad_schema = scorer.score(case, _resp('{"ok": "yes"}'))
    assert not bad_schema.passed and "schema" in bad_schema.detail


def test_classification_scorer_paths() -> None:
    scorer = ClassificationScorer()
    assert scorer.score(EvalCase(name="x", prompt="p"), _resp()).detail == "skipped"

    case = EvalCase(name="x", prompt="p", expected_intent="search")
    assert scorer.score(case, ModelResponse(text="t", intent="search")).passed
    res = scorer.score(case, ModelResponse(text="t", intent="chat"))
    assert not res.passed and "expected" in res.detail


def test_no_leak_scorer_paths() -> None:
    scorer = NoLeakScorer()
    assert scorer.score(EvalCase(name="x", prompt="p"), _resp()).detail == "skipped"
    case = EvalCase(name="x", prompt="p", no_leak=True)
    assert scorer.score(case, _resp("clean")).passed
    res = scorer.score(case, _resp("contact me at a@b.co"))
    assert not res.passed and "leaked" in res.detail


def test_default_scorers_returns_full_pipeline() -> None:
    names = {s.name for s in default_scorers()}
    assert names == {"substring", "regex", "json_shape", "classification", "no_leak"}


# ---------------------------------------------------------------------------
# runner / report
# ---------------------------------------------------------------------------


def test_runner_passes_and_fills_latency() -> None:
    suite = EvalSuite(
        name="t",
        cases=[EvalCase(name="hi", prompt="hi", expected_substrings=["hi"])],
    )
    runner = EvalRunner(client=FakeModelClient())
    report = runner.run(suite)
    assert report.passed == 1 and report.total == 1
    assert report.meets_threshold
    assert report.results[0].response.latency_ms >= 0.0


def test_runner_fails_on_latency_and_confidence() -> None:
    class SlowClient:
        def complete(self, prompt: str) -> ModelResponse:
            return ModelResponse(text="x", confidence=0.1, latency_ms=9000.0)

    suite = EvalSuite(
        name="t",
        cases=[
            EvalCase(name="latency", prompt="p", max_latency_ms=10.0),
            EvalCase(name="conf", prompt="p", min_confidence=0.9),
        ],
    )
    report = EvalRunner(client=SlowClient()).run(suite)
    assert report.passed == 0
    failures = [f for r in report.results for f in r.failures]
    assert any("latency" in f for f in failures)
    assert any("confidence" in f for f in failures)


def test_runner_protocol_is_satisfied() -> None:
    assert isinstance(FakeModelClient(), ModelClient)


def test_fake_client_canned_response() -> None:
    canned = ModelResponse(text="canned", intent="chat", confidence=1.0, latency_ms=2.0)
    client = FakeModelClient(responses={"hi": canned})
    assert client.complete("hi") is canned
    other = client.complete("Tell me a joke.")
    assert other.intent == "chat"
    assert other.text == "Tell me a joke."


def test_fake_client_echoes_json_payloads() -> None:
    client = FakeModelClient()
    assert client.complete('{"ok": true}').text == '{"ok": true}'


def test_fake_client_falls_back_to_default_intent() -> None:
    assert FakeModelClient().complete("####").intent == "other"


def test_fake_client_invalid_json_falls_through() -> None:
    response = FakeModelClient().complete("{not valid")
    assert response.text == "{not valid"


def test_report_serialization_round_trip() -> None:
    suite = EvalSuite(
        name="t",
        version=2,
        min_pass_rate=0.5,
        cases=[EvalCase(name="ok", prompt="hi")],
    )
    report = EvalRunner(client=FakeModelClient()).run(suite)
    payload = json.loads(report.to_json())
    assert payload["suite"] == "t" and payload["version"] == 2
    assert payload["meets_threshold"] is True
    md = report.to_markdown()
    assert "Eval report: t (v2)" in md and "| ok |" in md


def test_report_pass_rate_zero_when_empty() -> None:
    report = EvalReport(suite=EvalSuite(name="empty", cases=[]), results=[])
    assert report.pass_rate == 0.0


# ---------------------------------------------------------------------------
# load_suite
# ---------------------------------------------------------------------------


def test_load_suite_reads_yaml(tmp_path: Path) -> None:
    suite = load_suite(SUITES_DIR / "baseline.yaml")
    assert suite.name == "baseline"
    assert len(suite.cases) >= 10
    assert any(c.expected_intent == "search" for c in suite.cases)


def test_load_suite_rejects_non_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_suite(bad)


def test_load_suite_rejects_empty_cases(tmp_path: Path) -> None:
    bad = tmp_path / "empty.yaml"
    bad.write_text("name: x\ncases: []\n")
    with pytest.raises(ValueError, match="no cases"):
        load_suite(bad)


def test_all_shipped_suites_load_and_pass() -> None:
    for path in SUITES_DIR.glob("*.yaml"):
        suite = load_suite(path)
        report = EvalRunner(client=FakeModelClient()).run(suite)
        # Suites are written so the FakeModelClient's echo response
        # satisfies all assertions, keeping CI green offline.
        assert report.meets_threshold, (
            f"{path.name} below threshold: {report.passed}/{report.total}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_run_succeeds_and_writes_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_json = tmp_path / "r.json"
    out_md = tmp_path / "r.md"
    rc = cli.main(
        [
            "run",
            str(SUITES_DIR / "baseline.yaml"),
            "--fake-client",
            "--out-json",
            str(out_json),
            "--out-md",
            str(out_md),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Suite: baseline" in captured and "Summary:" in captured
    assert json.loads(out_json.read_text())["suite"] == "baseline"
    assert "Eval report: baseline" in out_md.read_text()


def test_cli_run_without_fake_client_uses_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["run", str(SUITES_DIR / "classify.yaml")])
    assert rc == 0
    assert "Suite: classify" in capsys.readouterr().out


def test_cli_exits_nonzero_when_below_threshold(tmp_path: Path) -> None:
    suite = tmp_path / "fail.yaml"
    suite.write_text(
        yaml.safe_dump(
            {
                "name": "fail",
                "min_pass_rate": 1.0,
                "cases": [
                    {
                        "name": "must_contain",
                        "prompt": "p",
                        "expected_substrings": ["IMPOSSIBLE"],
                    }
                ],
            }
        )
    )
    assert cli.main(["run", str(suite), "--fake-client"]) == 1


def test_cli_rejects_unknown_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.main(["bogus"])  # argparse errors out


# ---------------------------------------------------------------------------
# harness compatibility surface
# ---------------------------------------------------------------------------


def test_harness_run_suite_uses_default_fake_client() -> None:
    report = harness.run_suite(SUITES_DIR / "classify.yaml")
    assert report.suite.name == "classify"
    assert report.total == len(report.results)


def test_harness_run_suite_accepts_explicit_client() -> None:
    report = harness.run_suite(SUITES_DIR / "classify.yaml", client=FakeModelClient())
    assert report.meets_threshold


# ---------------------------------------------------------------------------
# prompts loader
# ---------------------------------------------------------------------------


def test_prompts_loader_parses_version(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("# version: 3\n\nHello {name}\n")
    prompt = _loader.load_prompt(p)
    assert prompt.version == 3
    assert "Hello {name}" in prompt.body
    assert prompt.name == "t"


def test_prompts_loader_rejects_missing_header(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("just a prompt\n")
    with pytest.raises(ValueError, match="version"):
        _loader.load_prompt(p)


def test_prompts_loader_rejects_blank_file(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("\n\n")
    with pytest.raises(ValueError, match="version"):
        _loader.load_prompt(p)


def test_prompts_loader_load_all(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("# version: 1\nA\n")
    (tmp_path / "b.md").write_text("# version: 2\nB\n")
    (tmp_path / "ignore.json").write_text("{}\n")
    prompts = _loader.load_all(tmp_path)
    assert set(prompts) == {"a", "b"}
    assert prompts["b"].version == 2


def test_parse_version_accepts_whitespace_variants() -> None:
    assert _loader.parse_version("#version:7\n") == 7
    assert _loader.parse_version("   #   Version :  4   \n\nbody") == 4


# ---------------------------------------------------------------------------
# pytest marker for ollama-backed cases (issue AC: excluded by default)
# ---------------------------------------------------------------------------


@pytest.mark.ollama
def test_ollama_marked_case_is_skipped_by_default() -> None:  # pragma: no cover
    raise AssertionError("ollama-marked cases must be opt-in via `pytest -m ollama`")
