"""Tests for orchestrator.core.logging."""

from __future__ import annotations

import io
import json

import pytest
import structlog

from orchestrator.core.logging import (
    configure_logging,
    is_configured,
    reset_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_logging():
    reset_for_testing()
    yield
    reset_for_testing()


def test_configure_logging_idempotent() -> None:
    assert is_configured() is False
    configure_logging(level="INFO", json=False)
    assert is_configured() is True
    # Second call must be a no-op (no exception, still configured).
    configure_logging(level="DEBUG", json=True)
    assert is_configured() is True


def test_json_flag_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json=True)
    log = structlog.get_logger("test.json")
    log.info("hello", extra_key="extra_val")
    out = capsys.readouterr().out.strip().splitlines()
    assert out, "expected at least one log line"
    payload = json.loads(out[-1])
    assert payload["event"] == "hello"
    assert payload["extra_key"] == "extra_val"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_console_flag_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json=False)
    log = structlog.get_logger("test.console")
    log.info("hi-there")
    out = capsys.readouterr().out
    assert "hi-there" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip().splitlines()[-1])


def test_invalid_level_raises() -> None:
    with pytest.raises(ValueError, match="Unknown log level"):
        configure_logging(level="NOPE")


def test_level_threshold_filters(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", json=True)
    log = structlog.get_logger("test.threshold")
    log.info("filtered-out")
    log.warning("kept")
    out = [line for line in capsys.readouterr().out.strip().splitlines() if line]
    parsed = [json.loads(line) for line in out]
    events = [p["event"] for p in parsed]
    assert "filtered-out" not in events
    assert "kept" in events


def test_logger_can_redirect_to_buffer() -> None:
    buf = io.StringIO()
    configure_logging(level="INFO", json=True)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=buf))
    log = structlog.get_logger("test.buf")
    log.info("buffered")
    assert "buffered" in buf.getvalue()
