"""Thread-safe in-memory per-provider quota tracker.

Phase 7 will swap this for a SQLite-backed implementation; the public
``check_and_increment`` / ``reset`` surface is the seam to preserve.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .errors import ProviderQuotaExceeded

__all__ = ["QuotaState", "QuotaTracker"]


@dataclass
class QuotaState:
    """Mutable per-provider quota state."""

    count: int = 0
    reset_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(days=1))


class QuotaTracker:
    """Thread-safe per-provider request counter with a rolling reset window.

    Each provider has an independent counter and ``reset_at`` timestamp.
    When ``now() >= reset_at``, the counter is rolled over before the next
    increment is checked.
    """

    def __init__(self, window: timedelta = timedelta(days=1)) -> None:
        self._window = window
        self._lock = threading.Lock()
        self._state: dict[str, QuotaState] = {}
        self._limits: dict[str, int | None] = {}

    def configure(self, provider: str, daily_quota: int | None) -> None:
        """Register or update the limit for ``provider``.

        ``None`` means unlimited.
        """
        with self._lock:
            self._limits[provider] = daily_quota
            self._state.setdefault(provider, QuotaState(reset_at=self._next_reset()))

    def _next_reset(self) -> datetime:
        return datetime.now(UTC) + self._window

    def _maybe_roll(self, state: QuotaState) -> None:
        if datetime.now(UTC) >= state.reset_at:
            state.count = 0
            state.reset_at = self._next_reset()

    def check_and_increment(self, provider: str) -> int:
        """Increment the counter for ``provider`` and return the new value.

        Raises:
            ProviderQuotaExceeded: If the configured ``daily_quota`` would be
                exceeded by this call.
        """
        with self._lock:
            state = self._state.setdefault(provider, QuotaState(reset_at=self._next_reset()))
            self._maybe_roll(state)
            limit = self._limits.get(provider)
            if limit is not None and state.count >= limit:
                raise ProviderQuotaExceeded(
                    provider,
                    f"daily quota of {limit} exhausted for {provider!r}",
                )
            state.count += 1
            return state.count

    def snapshot(self, provider: str) -> QuotaState:
        """Return a copy of the current state for ``provider``."""
        with self._lock:
            state = self._state.setdefault(provider, QuotaState(reset_at=self._next_reset()))
            return QuotaState(count=state.count, reset_at=state.reset_at)

    def reset(self, provider: str | None = None) -> None:
        """Reset counters. With no argument, all providers are reset."""
        with self._lock:
            targets = [provider] if provider is not None else list(self._state)
            for name in targets:
                self._state[name] = QuotaState(reset_at=self._next_reset())
