#!/usr/bin/env bash
# Bootstrap a local development environment.
set -euo pipefail
python -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
pre-commit install --install-hooks
echo "Dev environment ready. Activate with: source .venv/bin/activate"
