"""Tests for :mod:`orchestrator.runtime.quota`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestrator.runtime.quota import (
    DEFAULT_QUOTAS_PATH,
    QuotaLimits,
    QuotaTracker,
    QuotaUsage,
    load_default_limits,
)


class FakeClock:
    """Mutable clock for deterministic window tests."""

    def __init__(self, now: int = 1_700_000_000) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "quota.sqlite3"


@pytest.fixture
def limits() -> dict[str, QuotaLimits]:
    return {
        "anthropic": QuotaLimits(rpm=2, tpm=100, rpd=5, tpd=1_000),
        "gemini": QuotaLimits(rpm=0, tpm=0, rpd=0, tpd=0),  # unlimited
    }


# --- defaults / config ------------------------------------------------------


def test_default_quotas_toml_parses() -> None:
    parsed = load_default_limits()
    assert "anthropic" in parsed
    assert "gemini" in parsed
    assert isinstance(parsed["anthropic"], QuotaLimits)
    assert parsed["anthropic"].rpm > 0


def test_load_default_limits_custom_path(tmp_path: Path) -> None:
    p = tmp_path / "q.toml"
    p.write_text("[foo]\nrpm = 7\ntpm = 1\nrpd = 2\ntpd = 3\n")
    out = load_default_limits(p)
    assert out["foo"] == QuotaLimits(rpm=7, tpm=1, rpd=2, tpd=3)


def test_default_path_constant() -> None:
    assert DEFAULT_QUOTAS_PATH.name == "quotas.toml"


# --- core tracker behaviour --------------------------------------------------


def test_consume_increments_both_windows(db_path: Path, limits: dict[str, QuotaLimits]) -> None:
    clock = FakeClock()
    with QuotaTracker(db_path, limits=limits, clock=clock) as q:
        q.consume("anthropic", tokens=20, requests=1)
        usage = q.available("anthropic")
        assert usage.requests_minute == 1
        assert usage.tokens_minute == 20
        assert usage.requests_day == 1
        assert usage.tokens_day == 20
        assert usage.exhausted is False


def test_pre_attempt_check_true_when_idle(db_path: Path, limits: dict[str, QuotaLimits]) -> None:
    with QuotaTracker(db_path, limits=limits) as q:
        assert q.pre_attempt_check("anthropic") is True


def test_pre_attempt_check_false_when_rpm_hit(
    db_path: Path, limits: dict[str, QuotaLimits]
) -> None:
    clock = FakeClock()
    with QuotaTracker(db_path, limits=limits, clock=clock) as q:
        q.consume("anthropic", tokens=1, requests=1)
        q.consume("anthropic", tokens=1, requests=1)
        assert q.pre_attempt_check("anthropic") is False
        usage = q.available("anthropic")
        assert usage.exhausted is True


def test_tpm_limit_triggers_exhaustion(db_path: Path, limits: dict[str, QuotaLimits]) -> None:
    with QuotaTracker(db_path, limits=limits) as q:
        q.consume("anthropic", tokens=100, requests=1)
        assert q.pre_attempt_check("anthropic") is False


def test_rpd_limit_triggers_exhaustion(db_path: Path) -> None:
    lim = {"x": QuotaLimits(rpm=0, tpm=0, rpd=2, tpd=0)}
    clock = FakeClock()
    with QuotaTracker(db_path, limits=lim, clock=clock) as q:
        q.consume("x", requests=1)
        clock.advance(120)  # roll minute window
        q.consume("x", requests=1)
        assert q.pre_attempt_check("x") is False


def test_tpd_limit_triggers_exhaustion(db_path: Path) -> None:
    lim = {"x": QuotaLimits(rpm=0, tpm=0, rpd=0, tpd=10)}
    with QuotaTracker(db_path, limits=lim) as q:
        q.consume("x", tokens=10, requests=1)
        assert q.pre_attempt_check("x") is False


def test_unknown_provider_is_unlimited(db_path: Path) -> None:
    with QuotaTracker(db_path) as q:
        assert q.limits_for("nope") == QuotaLimits()
        for _ in range(50):
            q.consume("nope", tokens=10_000)
        assert q.pre_attempt_check("nope") is True


def test_minute_window_rolls_over(db_path: Path, limits: dict[str, QuotaLimits]) -> None:
    clock = FakeClock(now=1_700_000_000)
    with QuotaTracker(db_path, limits=limits, clock=clock) as q:
        q.consume("anthropic", tokens=1, requests=1)
        q.consume("anthropic", tokens=1, requests=1)
        assert q.pre_attempt_check("anthropic") is False
        clock.advance(61)
        usage = q.available("anthropic")
        assert usage.requests_minute == 0
        assert usage.requests_day == 2  # day still tracking
        assert q.pre_attempt_check("anthropic") is True


def test_day_window_rolls_over(db_path: Path) -> None:
    lim = {"x": QuotaLimits(rpd=1)}
    clock = FakeClock()
    with QuotaTracker(db_path, limits=lim, clock=clock) as q:
        q.consume("x", requests=1)
        assert q.pre_attempt_check("x") is False
        clock.advance(86_400 + 1)
        assert q.pre_attempt_check("x") is True


def test_consume_negative_rejected(db_path: Path) -> None:
    with QuotaTracker(db_path) as q:
        with pytest.raises(ValueError):
            q.consume("x", tokens=-1)
        with pytest.raises(ValueError):
            q.consume("x", requests=-1)


# --- 429 cooldown -----------------------------------------------------------


def test_record_429_blocks_until_cooldown(db_path: Path) -> None:
    clock = FakeClock()
    with QuotaTracker(db_path, clock=clock, cooldown_seconds=30) as q:
        q.record_429("anthropic")
        usage = q.available("anthropic")
        assert usage.cooldown_remaining > 0
        assert q.pre_attempt_check("anthropic") is False
        clock.advance(31)
        assert q.pre_attempt_check("anthropic") is True


def test_record_429_overwrites_previous(db_path: Path) -> None:
    clock = FakeClock()
    with QuotaTracker(db_path, clock=clock, cooldown_seconds=30) as q:
        q.record_429("p")
        clock.advance(20)
        q.record_429("p")  # refreshed
        clock.advance(20)
        assert q.pre_attempt_check("p") is False
        clock.advance(20)
        assert q.pre_attempt_check("p") is True


# --- persistence across "restarts" ------------------------------------------


def test_counters_persist_across_reopen(db_path: Path, limits: dict[str, QuotaLimits]) -> None:
    clock = FakeClock()
    with QuotaTracker(db_path, limits=limits, clock=clock) as q1:
        q1.consume("anthropic", tokens=10, requests=1)

    with QuotaTracker(db_path, limits=limits, clock=clock) as q2:
        usage = q2.available("anthropic")
        assert usage.requests_minute == 1
        assert usage.tokens_minute == 10


def test_cooldown_persists_across_reopen(db_path: Path) -> None:
    clock = FakeClock()
    with QuotaTracker(db_path, clock=clock, cooldown_seconds=120) as q1:
        q1.record_429("p")

    with QuotaTracker(db_path, clock=clock, cooldown_seconds=120) as q2:
        assert q2.pre_attempt_check("p") is False


def test_schema_created(db_path: Path) -> None:
    QuotaTracker(db_path).close()
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_quota'"
    ).fetchall()
    con.close()
    assert rows


# --- usage dataclass --------------------------------------------------------


def test_quota_usage_exhausted_property() -> None:
    lim = QuotaLimits(rpm=10, tpm=100, rpd=100, tpd=1000)
    fresh = QuotaUsage(
        provider="x",
        requests_minute=0,
        tokens_minute=0,
        requests_day=0,
        tokens_day=0,
        cooldown_remaining=0.0,
        limits=lim,
    )
    assert fresh.exhausted is False
    cooling = QuotaUsage("x", 0, 0, 0, 0, 5.0, lim)
    assert cooling.exhausted is True


# --- fallback integration ---------------------------------------------------


def test_fallback_chain_skips_exhausted_provider(
    db_path: Path, limits: dict[str, QuotaLimits]
) -> None:
    from orchestrator.providers.fallback import FallbackChain, QuotaExceeded

    class StubProvider:
        def __init__(self, pid: str, value: str) -> None:
            self.id = pid
            self.value = value
            self.calls = 0

        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return self.value

    a = StubProvider("anthropic", "A")
    g = StubProvider("gemini", "G")

    with QuotaTracker(db_path, limits=limits) as q:
        # Exhaust anthropic.
        q.consume("anthropic", tokens=1, requests=1)
        q.consume("anthropic", tokens=1, requests=1)
        chain = FallbackChain([a, g], pre_attempt_check=q.pre_attempt_check)
        result = chain.complete([{"role": "user", "content": "hi"}])
        assert result == "G"
        assert a.calls == 0
        assert g.calls == 1

    # No-check chain still works.
    chain2 = FallbackChain([a, g])
    assert chain2.complete([]) == "A"
    # Sanity: QuotaExceeded is the error class we re-use.
    assert issubclass(QuotaExceeded, Exception)
