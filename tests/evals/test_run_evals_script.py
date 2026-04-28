"""Tests for ``scripts/run_evals.py`` and the package-level smoke run."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_script() -> object:
    path = REPO_ROOT / "scripts" / "run_evals.py"
    spec = importlib.util.spec_from_file_location("scripts_run_evals", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_script_runs_named_suite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _import_script()
    rc = mod.main(["classify", "--reports-dir", str(tmp_path)])  # type: ignore[attr-defined]
    assert rc == 0
    assert "classify:" in capsys.readouterr().out
    assert any(p.suffix == ".md" for p in tmp_path.iterdir())


def test_script_runs_all_suites(tmp_path: Path) -> None:
    mod = _import_script()
    rc = mod.main(["--all", "--reports-dir", str(tmp_path)])  # type: ignore[attr-defined]
    assert rc == 0


def test_script_requires_a_suite_name(tmp_path: Path) -> None:
    mod = _import_script()
    with pytest.raises(SystemExit):
        mod.main(["--reports-dir", str(tmp_path)])  # type: ignore[attr-defined]


def test_script_rejects_unknown_suite(tmp_path: Path) -> None:
    mod = _import_script()
    with pytest.raises(SystemExit):
        mod.main(["does-not-exist", "--reports-dir", str(tmp_path)])  # type: ignore[attr-defined]
