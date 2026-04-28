# Development Guide

This guide describes how to set up a local development environment and the
common workflows for working on the coracle.

## Prerequisites

- Python 3.11 or newer
- `git` with commit signing configured (the repo enforces signed commits)
- GNU Make (optional but recommended)

## One-line bootstrap

From a fresh clone:

```bash
# macOS / Linux
./scripts/dev_setup.sh
```

```powershell
# Windows PowerShell
.\scripts\dev_setup.ps1
```

The script creates a `.venv`, installs the package in editable mode with the
`dev` extras, and installs the pre-commit hooks.

## Manual setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pre-commit install --install-hooks
```

## Common Makefile targets

| Target          | What it does                                       |
| --------------- | -------------------------------------------------- |
| `make help`     | List all available targets (default).              |
| `make install`  | Install dev deps and pre-commit hooks.             |
| `make lint`     | Run `ruff check` and `ruff format --check`.        |
| `make format`   | Auto-format and apply safe lint fixes.             |
| `make test`     | Run the test suite.                                |
| `make cov`      | Run tests with the 95% coverage gate.              |
| `make precommit`| Run all pre-commit hooks across the repo.          |
| `make clean`    | Remove caches and build artifacts.                 |

## Pre-commit hooks

Configured in `.pre-commit-config.yaml`:

- `pre-commit-hooks`: trailing whitespace, EOF fixer, YAML / TOML / merge
  conflict / large file checks.
- `ruff`: lint (with `--fix`) and format.
- `gitleaks`: scan for accidentally committed secrets.

Run them on demand with:

```bash
pre-commit run --all-files
```

## Type checking

`mypy` is configured in strict mode for the `coracle/` package; tests and
helper scripts under `.github/scripts/` are excluded. Run it with:

```bash
mypy coracle
```

## Editor configuration

`.editorconfig` enforces UTF-8, LF line endings, trimmed trailing whitespace,
4-space indentation for Python and 2-space indentation for YAML / JSON / MD.
