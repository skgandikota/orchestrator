#!/usr/bin/env pwsh
# Bootstrap a local development environment.
$ErrorActionPreference = "Stop"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
pre-commit install --install-hooks
Write-Host "Dev environment ready. Activate with: .\.venv\Scripts\Activate.ps1"
