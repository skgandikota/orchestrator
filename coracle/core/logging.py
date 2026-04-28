"""Structured logging configuration via structlog.

Idempotent: ``configure_logging`` may be called multiple times safely.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

__all__ = ["configure_logging", "is_configured", "reset_for_testing"]

_configured: bool = False


def is_configured() -> bool:
    """Whether ``configure_logging`` has been called since process start."""
    return _configured


def reset_for_testing() -> None:
    """Reset the idempotency flag and structlog state. Test helper."""
    global _configured
    _configured = False
    structlog.reset_defaults()


def configure_logging(level: str = "INFO", json: bool = False) -> None:
    """Configure structlog + stdlib logging.

    Args:
        level: Log level name (e.g., ``"INFO"``).
        json: If True, emit JSON; otherwise human-friendly console output.
    """
    global _configured
    if _configured:
        return

    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        raise ValueError(f"Unknown log level: {level!r}")

    logging.basicConfig(
        format="%(message)s",
        level=numeric_level,
        force=True,
    )

    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True
