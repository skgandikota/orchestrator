#!/usr/bin/env bash
# Run the live RAM swap-cycle smoke test against a real Ollama daemon.
# Pre-req: `ollama pull qwen2.5:7b qwen2.5-coder:7b` and `ollama serve` running.
set -euo pipefail
cd "$(dirname "$0")/.."
pytest -m live --live tests/smoke/test_ram_swap.py -s "$@"
