"""Coracle package: local-first agent runtime (skeleton)."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import structlog

try:
    __version__ = version("coracle")
except PackageNotFoundError:  # pragma: no cover - source checkout w/o install
    __version__ = "0.0.0+local"

logger = structlog.get_logger("coracle")

__all__ = ["__version__", "logger"]
