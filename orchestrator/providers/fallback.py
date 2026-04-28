"""Provider fallback chain with per-provider circuit breaker.

This module implements the architectural rule from ``docs/PLAN.md`` §7:

    Provider fallback is automatic: API quota exhausted -> next API
    -> browser driver as last resort.

Design
------

* :class:`FallbackChain` wraps an ordered list of providers (matching the
  :class:`Provider` :class:`typing.Protocol`) and tries each in turn until one
  returns a result or the chain is exhausted.
* Each provider has an independent :class:`CircuitBreaker`. The breaker opens
  after ``failure_threshold`` consecutive failures; while open the provider is
  skipped until the ``cooldown`` elapses, after which it transitions to
  ``HALF_OPEN`` and is given exactly one trial request. A success closes the
  breaker; a failure re-opens it.
* Failure classification is deliberate. **Transient** failures
  (:class:`QuotaExceeded`, :class:`ProviderUnavailable`,
  :class:`BrowserDriverCrashed`, :class:`TimeoutError`,
  :class:`ConnectionError`, ``httpx.HTTPError`` if installed) cause fall-through
  to the next provider. **Terminal** failures (:class:`AuthError`,
  :class:`InvalidRequest`) abort the chain immediately - retrying them on a
  different provider would just leak the same bad credentials / bad payload.
* Browser providers (:class:`BrowserProvider`) are only attempted after every
  API provider has failed, and only when ``browser_as_last_resort=True``. They
  are kept behind a :class:`typing.Protocol` so this module does not depend on
  Playwright or the in-flight #9 implementation.
* Every attempt, success, fall-through, and terminal failure is emitted as an
  :mod:`orchestrator.observability.audit` event so operators can see the chain
  in production.

The provider interface is a :class:`typing.Protocol` so the unit tests
intentionally do **not** depend on the LiteLLM router landing in #8; integration
happens on rebase or in a follow-up.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from orchestrator.observability import audit

__all__ = [
    "AllProvidersFailed",
    "AuthError",
    "BrowserDriverCrashed",
    "BrowserProvider",
    "CircuitBreaker",
    "CircuitState",
    "FallbackChain",
    "InvalidRequest",
    "Provider",
    "ProviderUnavailable",
    "QuotaExceeded",
]


# --- Errors -----------------------------------------------------------------


class ProviderError(Exception):
    """Base class for all provider-related errors raised by this package."""


class QuotaExceeded(ProviderError):
    """The provider rejected the request because a rate or quota limit was hit.

    Mirrors the error type defined by the LiteLLM router in #8 so callers can
    catch a single name once both PRs land.
    """


class ProviderUnavailable(ProviderError):
    """The provider failed transiently (5xx, network blip, timeout)."""


class BrowserDriverCrashed(ProviderError):
    """The browser-driven provider subprocess died mid-request."""


class AuthError(ProviderError):
    """Authentication / authorization failed (401, 403). Do **not** retry."""


class InvalidRequest(ProviderError):
    """The request itself was malformed (400). Do **not** retry."""


@dataclass
class AllProvidersFailed(ProviderError):
    """Raised when the chain is exhausted without a successful response.

    Attributes:
        failures: Ordered list of ``(provider_id, exception)`` tuples, one per
            provider that was attempted.
    """

    failures: list[tuple[str, BaseException]] = field(default_factory=list)

    def __str__(self) -> str:
        if not self.failures:
            return "AllProvidersFailed: no providers were attempted"
        parts = [f"{pid}: {type(exc).__name__}: {exc}" for pid, exc in self.failures]
        return "AllProvidersFailed: " + "; ".join(parts)


# Failure classes that should cause the chain to advance to the next provider.
# ``httpx.HTTPError`` is added dynamically below if httpx is importable so we
# do not hard-depend on it in environments that strip it.
_FALLTHROUGH_ERRORS: tuple[type[BaseException], ...] = (
    QuotaExceeded,
    ProviderUnavailable,
    BrowserDriverCrashed,
    TimeoutError,
    ConnectionError,
)

try:
    import httpx as _httpx

    _FALLTHROUGH_ERRORS = (*_FALLTHROUGH_ERRORS, _httpx.HTTPError)
except ImportError:  # pragma: no cover - defensive
    pass

# Failure classes that should abort the chain immediately.
_TERMINAL_ERRORS: tuple[type[BaseException], ...] = (AuthError, InvalidRequest)


# --- Provider protocols -----------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """Minimum interface a provider must satisfy to participate in a chain.

    The :attr:`id` is used for audit/log correlation and as the circuit-breaker
    key; it must be stable and unique within a chain.
    """

    id: str

    def complete(self, messages: Sequence[dict[str, Any]], **kwargs: Any) -> Any:
        """Run a chat completion. Raise a :class:`ProviderError` on failure."""


@runtime_checkable
class BrowserProvider(Provider, Protocol):
    """Marker protocol for providers driven by a browser subprocess (#9)."""


# --- Circuit breaker --------------------------------------------------------


class CircuitState(enum.StrEnum):
    """Three-state circuit breaker as described in Nygard, *Release It!*."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-provider failure counter that short-circuits broken providers.

    The breaker is **closed** in steady state - all calls go through. After
    ``failure_threshold`` consecutive failures it transitions to **open**;
    :meth:`allow` returns ``False`` for the next ``cooldown`` seconds, causing
    :class:`FallbackChain` to skip the provider entirely. Once the cooldown has
    elapsed the next :meth:`allow` call flips the breaker to **half-open** and
    permits a single trial request: a success on that trial closes the breaker
    and resets the failure counter, while a failure immediately re-opens it for
    another full cooldown.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Any = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._state: CircuitState = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    def allow(self) -> bool:
        """Return ``True`` if a request should be attempted right now."""
        if self._state is CircuitState.CLOSED:
            return True
        if self._state is CircuitState.OPEN:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()


# --- Fallback chain ---------------------------------------------------------


class FallbackChain:
    """Ordered chain of providers with per-provider circuit breakers.

    Args:
        providers: Ordered list of :class:`Provider` instances. The first one
            that succeeds wins.
        browser_provider: Optional :class:`BrowserProvider` invoked only after
            every entry in ``providers`` has failed.
        browser_as_last_resort: If ``False`` the ``browser_provider`` is never
            invoked even when set; matches the ``[providers]`` settings flag.
        failure_threshold: Consecutive failures required to trip a breaker.
        cooldown_seconds: How long a tripped breaker stays open.
        actor: Audit-log ``actor`` field; defaults to ``"providers.fallback"``.
        clock: Monotonic clock injected for deterministic tests.
    """

    def __init__(
        self,
        providers: Sequence[Provider],
        *,
        browser_provider: BrowserProvider | None = None,
        browser_as_last_resort: bool = True,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        actor: str = "providers.fallback",
        clock: Any = time.monotonic,
    ) -> None:
        if not providers and browser_provider is None:
            raise ValueError("FallbackChain requires at least one provider")
        self._providers: tuple[Provider, ...] = tuple(providers)
        self._browser_provider = browser_provider
        self._browser_as_last_resort = browser_as_last_resort
        self._actor = actor
        self._breakers: dict[str, CircuitBreaker] = {
            p.id: CircuitBreaker(
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
                clock=clock,
            )
            for p in self._providers
        }
        if browser_provider is not None:
            self._breakers[browser_provider.id] = CircuitBreaker(
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
                clock=clock,
            )

    # --- introspection ----------------------------------------------------

    def breaker(self, provider_id: str) -> CircuitBreaker:
        """Return the circuit breaker for ``provider_id`` (test helper)."""
        return self._breakers[provider_id]

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(p.id for p in self._providers)

    # --- main entry point -------------------------------------------------

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """Try each provider in order; return the first successful response.

        Raises:
            AuthError / InvalidRequest: Re-raised immediately - these are
                terminal and would fail on every provider in the chain.
            AllProvidersFailed: All providers failed transiently; the
                ``failures`` attribute carries one ``(id, exc)`` tuple per
                provider that was attempted (skipped-by-breaker entries are
                included with a synthetic :class:`ProviderUnavailable`).
        """
        failures: list[tuple[str, BaseException]] = []

        for provider in self._providers:
            outcome = self._attempt(provider, messages, kwargs, failures)
            if outcome is not _MISS:
                return outcome

        if self._browser_provider is not None and self._browser_as_last_resort:
            outcome = self._attempt(self._browser_provider, messages, kwargs, failures)
            if outcome is not _MISS:
                return outcome

        err = AllProvidersFailed(failures=failures)
        audit.record(
            actor=self._actor,
            action="chain_failed",
            target=None,
            status="error",
            payload={"failures": [(pid, repr(exc)) for pid, exc in failures]},
        )
        raise err

    # --- internals --------------------------------------------------------

    def _attempt(
        self,
        provider: Provider,
        messages: Sequence[dict[str, Any]],
        kwargs: dict[str, Any],
        failures: list[tuple[str, BaseException]],
    ) -> Any:
        breaker = self._breakers[provider.id]
        if not breaker.allow():
            skip_exc = ProviderUnavailable(f"circuit breaker OPEN for {provider.id!r}")
            failures.append((provider.id, skip_exc))
            audit.record(
                actor=self._actor,
                action="provider_skipped",
                target=provider.id,
                status="warn",
                payload={"reason": "circuit_open"},
            )
            return _MISS

        audit.record(
            actor=self._actor,
            action="provider_attempt",
            target=provider.id,
            status="ok",
            payload={"breaker_state": breaker.state.value},
        )
        try:
            result = provider.complete(messages, **kwargs)
        except _TERMINAL_ERRORS as exc:
            audit.record(
                actor=self._actor,
                action="provider_failed_terminal",
                target=provider.id,
                status="error",
                payload={"error": type(exc).__name__, "message": str(exc)},
            )
            raise
        except _FALLTHROUGH_ERRORS as exc:
            breaker.record_failure()
            failures.append((provider.id, exc))
            audit.record(
                actor=self._actor,
                action="provider_fallthrough",
                target=provider.id,
                status="warn",
                payload={
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "breaker_state": breaker.state.value,
                },
            )
            return _MISS

        breaker.record_success()
        audit.record(
            actor=self._actor,
            action="provider_success",
            target=provider.id,
            status="ok",
        )
        return result


# Sentinel returned by :meth:`FallbackChain._attempt` to signal "no result yet,
# keep walking the chain". ``None`` is a perfectly valid provider response so we
# need a dedicated marker.
_MISS: Any = object()
