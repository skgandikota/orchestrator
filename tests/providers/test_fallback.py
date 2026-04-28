"""Tests for :mod:`coracle.providers.fallback`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from coracle.observability import audit
from coracle.providers.fallback import (
    AllProvidersFailed,
    AuthError,
    BrowserDriverCrashed,
    CircuitBreaker,
    CircuitState,
    FallbackChain,
    InvalidRequest,
    ProviderUnavailable,
    QuotaExceeded,
)

# --- Test doubles -----------------------------------------------------------


class StubProvider:
    """A scriptable provider whose ``complete`` returns/raises in sequence.

    Each entry in ``script`` is either a value to return or an exception to
    raise. The provider records every call in ``calls`` for assertions.
    """

    def __init__(self, provider_id: str, script: list[Any]) -> None:
        self.id = provider_id
        self.script = list(script)
        self.calls: list[tuple[Sequence[dict[str, Any]], dict[str, Any]]] = []

    def complete(self, messages: Sequence[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append((messages, kwargs))
        if not self.script:
            raise AssertionError(f"{self.id} called more than scripted")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClock:
    """Manually advanced monotonic clock for deterministic breaker tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_audit_log(tmp_path: Any) -> Any:
    """Each test gets its own audit log so events do not bleed between tests.

    Uses a tmp-file SQLite database (not ``:memory:``) because the audit
    writer thread opens its own connection and ``:memory:`` databases are
    per-connection.
    """
    log = audit.AuditLog(str(tmp_path / "audit.db"))
    audit.configure_default_log(log)
    try:
        yield log
    finally:
        audit.reset_default_log()


# --- CircuitBreaker ---------------------------------------------------------


class TestCircuitBreaker:
    def test_validates_args(self) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(failure_threshold=0)
        with pytest.raises(ValueError):
            CircuitBreaker(cooldown_seconds=-1)

    def test_starts_closed_and_allows(self) -> None:
        cb = CircuitBreaker()
        assert cb.state is CircuitState.CLOSED
        assert cb.allow() is True
        assert cb.failures == 0

    def test_opens_after_threshold(self) -> None:
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, clock=clock)
        cb.record_failure()
        assert cb.state is CircuitState.CLOSED
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        assert cb.allow() is False

    def test_half_open_after_cooldown_then_closes_on_success(self) -> None:
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=clock)
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        assert cb.allow() is False

        clock.advance(5)
        assert cb.allow() is True
        assert cb.state is CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state is CircuitState.CLOSED
        assert cb.failures == 0

    def test_half_open_failure_reopens(self) -> None:
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=clock)
        cb.record_failure()
        clock.advance(5)
        cb.allow()
        assert cb.state is CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        assert cb.allow() is False


# --- FallbackChain ----------------------------------------------------------


def _msgs() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "hi"}]


class TestFallbackChain:
    def test_requires_at_least_one_provider(self) -> None:
        with pytest.raises(ValueError):
            FallbackChain([])

    def test_first_provider_wins(self, _isolated_audit_log: audit.AuditLog) -> None:
        a = StubProvider("a", ["alpha"])
        b = StubProvider("b", ["beta"])
        chain = FallbackChain([a, b])

        result = chain.complete(_msgs())

        assert result == "alpha"
        assert len(a.calls) == 1
        assert b.calls == []
        _isolated_audit_log.close()
        successes = _isolated_audit_log.query(action="provider_success")
        assert any(e.target == "a" for e in successes)

    def test_fallthrough_on_quota(self, _isolated_audit_log: audit.AuditLog) -> None:
        a = StubProvider("a", [QuotaExceeded("rate limit")])
        b = StubProvider("b", ["beta"])
        chain = FallbackChain([a, b])

        result = chain.complete(_msgs())

        assert result == "beta"
        assert len(a.calls) == 1
        assert len(b.calls) == 1
        _isolated_audit_log.close()
        ft = _isolated_audit_log.query(action="provider_fallthrough")
        assert any(e.target == "a" for e in ft)

    def test_fallthrough_on_5xx_timeout_network(self) -> None:
        a = StubProvider("a", [ProviderUnavailable("503")])
        b = StubProvider("b", [TimeoutError("slow")])
        c = StubProvider("c", [ConnectionError("reset")])
        d = StubProvider("d", ["ok"])
        chain = FallbackChain([a, b, c, d])

        assert chain.complete(_msgs()) == "ok"

    def test_all_fail_bubbles_up(self, _isolated_audit_log: audit.AuditLog) -> None:
        a = StubProvider("a", [QuotaExceeded("q")])
        b = StubProvider("b", [ProviderUnavailable("u")])
        chain = FallbackChain([a, b])

        with pytest.raises(AllProvidersFailed) as excinfo:
            chain.complete(_msgs())

        ids = [pid for pid, _ in excinfo.value.failures]
        assert ids == ["a", "b"]
        # human readable
        assert "a:" in str(excinfo.value) and "b:" in str(excinfo.value)
        _isolated_audit_log.close()
        assert _isolated_audit_log.query(action="chain_failed")

    def test_empty_failures_str(self) -> None:
        err = AllProvidersFailed()
        assert "no providers" in str(err)

    def test_fail_fast_on_auth_error(self) -> None:
        a = StubProvider("a", [AuthError("401")])
        b = StubProvider("b", ["never"])
        chain = FallbackChain([a, b])

        with pytest.raises(AuthError):
            chain.complete(_msgs())

        assert b.calls == []  # second provider must not be tried

    def test_fail_fast_on_invalid_request(self) -> None:
        a = StubProvider("a", [InvalidRequest("400")])
        b = StubProvider("b", ["never"])
        chain = FallbackChain([a, b])

        with pytest.raises(InvalidRequest):
            chain.complete(_msgs())
        assert b.calls == []

    def test_browser_fallback_called_when_enabled(self) -> None:
        a = StubProvider("a", [QuotaExceeded("q")])
        browser = StubProvider("browser", ["from-browser"])
        chain = FallbackChain([a], browser_provider=browser)

        assert chain.complete(_msgs()) == "from-browser"
        assert len(browser.calls) == 1

    def test_browser_skipped_when_flag_off(self) -> None:
        a = StubProvider("a", [QuotaExceeded("q")])
        browser = StubProvider("browser", ["never"])
        chain = FallbackChain([a], browser_provider=browser, browser_as_last_resort=False)

        with pytest.raises(AllProvidersFailed):
            chain.complete(_msgs())
        assert browser.calls == []

    def test_browser_invoked_after_all_apis_fail_with_crash(self) -> None:
        a = StubProvider("a", [ProviderUnavailable("503")])
        b = StubProvider("b", [QuotaExceeded("q")])
        browser = StubProvider("browser", [BrowserDriverCrashed("oops"), "ok"])
        chain = FallbackChain([a, b], browser_provider=browser)

        with pytest.raises(AllProvidersFailed) as excinfo:
            chain.complete(_msgs())
        assert [pid for pid, _ in excinfo.value.failures] == ["a", "b", "browser"]

    def test_circuit_breaker_opens_then_skips_then_half_open(self) -> None:
        clock = FakeClock()
        a = StubProvider(
            "a",
            [
                ProviderUnavailable("1"),
                ProviderUnavailable("2"),
                ProviderUnavailable("3"),
                "ok-after-recovery",
            ],
        )
        b = StubProvider("b", ["beta-1", "beta-2", "beta-3", "beta-4"])
        chain = FallbackChain(
            [a, b],
            failure_threshold=3,
            cooldown_seconds=10,
            clock=clock,
        )

        # Drive `a` to OPEN by failing 3x; each call falls through to `b`.
        for _ in range(3):
            assert chain.complete(_msgs()).startswith("beta")
        assert chain.breaker("a").state is CircuitState.OPEN

        # While OPEN, `a` is skipped without being called.
        a_calls_before = len(a.calls)
        assert chain.complete(_msgs()).startswith("beta")
        assert len(a.calls) == a_calls_before  # not invoked

        # After cooldown, `a` is given a half-open trial which succeeds.
        clock.advance(10)
        result = chain.complete(_msgs())
        assert result == "ok-after-recovery"
        assert chain.breaker("a").state is CircuitState.CLOSED

    def test_circuit_breaker_half_open_failure_reopens(self) -> None:
        clock = FakeClock()
        a = StubProvider(
            "a",
            [ProviderUnavailable("x")] * 4,
        )
        b = StubProvider("b", ["beta"] * 5)
        chain = FallbackChain([a, b], failure_threshold=2, cooldown_seconds=5, clock=clock)

        # Trip the breaker.
        chain.complete(_msgs())
        chain.complete(_msgs())
        assert chain.breaker("a").state is CircuitState.OPEN

        # Cooldown -> half-open trial fails -> re-opens.
        clock.advance(5)
        chain.complete(_msgs())
        assert chain.breaker("a").state is CircuitState.OPEN

    def test_breaker_skip_recorded_in_failures_when_only_provider(self) -> None:
        clock = FakeClock()
        a = StubProvider("a", [ProviderUnavailable("x")])
        chain = FallbackChain([a], failure_threshold=1, cooldown_seconds=60, clock=clock)

        # First call fails and trips the breaker.
        with pytest.raises(AllProvidersFailed):
            chain.complete(_msgs())

        # Second call: breaker is OPEN, no providers attempted, raises with a
        # synthetic ProviderUnavailable explaining the skip.
        with pytest.raises(AllProvidersFailed) as excinfo:
            chain.complete(_msgs())
        assert len(excinfo.value.failures) == 1
        pid, exc = excinfo.value.failures[0]
        assert pid == "a"
        assert isinstance(exc, ProviderUnavailable)
        assert "circuit breaker OPEN" in str(exc)

    def test_kwargs_forwarded_to_provider(self) -> None:
        a = StubProvider("a", ["ok"])
        chain = FallbackChain([a])
        chain.complete(_msgs(), temperature=0.7, model="x")
        assert a.calls[0][1] == {"temperature": 0.7, "model": "x"}

    def test_provider_ids_property(self) -> None:
        chain = FallbackChain([StubProvider("a", []), StubProvider("b", [])])
        assert chain.provider_ids == ("a", "b")

    def test_chain_with_only_browser_provider_allowed(self) -> None:
        browser = StubProvider("browser", ["only"])
        chain = FallbackChain([], browser_provider=browser)
        assert chain.complete(_msgs()) == "only"

    def test_httpx_error_falls_through(self) -> None:
        httpx = pytest.importorskip("httpx")
        a = StubProvider("a", [httpx.ReadTimeout("slow")])
        b = StubProvider("b", ["ok"])
        chain = FallbackChain([a, b])
        assert chain.complete(_msgs()) == "ok"
