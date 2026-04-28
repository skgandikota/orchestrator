"""Command-line entry point: ``python -m evals run <suite.yaml>``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from evals.runner import EvalReport, EvalRunner, FakeModelClient, ModelClient, load_suite


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals", description="Run prompt eval suites.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run a YAML eval suite")
    run.add_argument("suite", type=Path, help="Path to a YAML suite definition")
    run.add_argument(
        "--fake-client",
        action="store_true",
        help="Use the deterministic FakeModelClient (default in offline runs)",
    )
    run.add_argument("--out-json", type=Path, help="Write JSON report to this path")
    run.add_argument("--out-md", type=Path, help="Write Markdown report to this path")
    return parser


def _resolve_client(use_fake: bool) -> ModelClient:
    if use_fake:
        return FakeModelClient()
    # Real clients are constructed by the integrating application; in
    # CLI context we currently only support the fake client to keep the
    # harness model-agnostic and side-effect free.
    return FakeModelClient()


def _print_report(report: EvalReport) -> None:
    print(f"Suite: {report.suite.name} (v{report.suite.version})")
    for r in report.results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.case.name}")
        for failure in r.failures:
            print(f"      - {failure}")
    print(
        f"Summary: {report.passed}/{report.total} "
        f"({report.pass_rate:.0%}, threshold {report.suite.min_pass_rate:.0%})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    suite = load_suite(args.suite)
    client = _resolve_client(bool(args.fake_client))
    runner = EvalRunner(client=client)
    report = runner.run(suite)

    _print_report(report)

    if args.out_json:
        args.out_json.write_text(report.to_json(), encoding="utf-8")
    if args.out_md:
        args.out_md.write_text(report.to_markdown(), encoding="utf-8")

    return 0 if report.meets_threshold else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
