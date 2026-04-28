"""CLI wrapper for running eval suites.

Usage::

    python scripts/run_evals.py classify
    python scripts/run_evals.py --all

Writes a Markdown report to ``reports/evals-<timestamp>.md`` and exits
non-zero if any suite drops below its declared ``min_pass_rate``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.runner import EvalRunner, FakeModelClient, load_suite  # noqa: E402

SUITES_DIR = REPO_ROOT / "evals" / "suites"
REPORTS_DIR = REPO_ROOT / "reports"


def _suite_paths(suite: str | None, run_all: bool) -> list[Path]:
    if run_all:
        return sorted(SUITES_DIR.glob("*.yaml"))
    if not suite:
        raise SystemExit("specify a suite name or pass --all")
    candidate = SUITES_DIR / f"{suite}.yaml"
    if not candidate.exists():
        raise SystemExit(f"suite not found: {candidate}")
    return [candidate]


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run coracle eval suites.")
    parser.add_argument("suite", nargs="?", help="Suite name (e.g. classify)")
    parser.add_argument("--all", action="store_true", help="Run every suite")
    parser.add_argument(
        "--reports-dir", type=Path, default=REPORTS_DIR, help="Where to write reports"
    )
    args = parser.parse_args(argv)

    paths = _suite_paths(args.suite, args.all)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.reports_dir / f"evals-{_timestamp()}.md"

    md_chunks: list[str] = []
    overall_ok = True
    for path in paths:
        suite = load_suite(path)
        runner = EvalRunner(client=FakeModelClient())
        report = runner.run(suite)
        print(f"{suite.name}: {report.passed}/{report.total} (v{suite.version})")
        for r in report.results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.case.name}")
        if not report.meets_threshold:
            overall_ok = False
        md_chunks.append(report.to_markdown())

    report_path.write_text("\n---\n\n".join(md_chunks), encoding="utf-8")
    print(f"Report: {report_path}")
    return 0 if overall_ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
