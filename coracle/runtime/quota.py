"""Persistent per-provider quota tracking.

Free-tier "big AI" providers (Anthropic, Gemini, OpenAI free, Groq, ...) impose
rate limits on two axes:

* requests-per-minute / requests-per-day  (RPM / RPD)
* tokens-per-minute   / tokens-per-day    (TPM / TPD)

If we hammer an exhausted provider we either get a 429 (wasted round-trip) or
we burn through our daily budget on a single retry storm. To keep the fallback
chain honest across ``coracle serve`` restarts the counters live in
SQLite, keyed by ``(provider, window_kind)`` where ``window_kind`` is one of
``minute`` / ``day`` / ``cooldown`` and ``window_start_ts`` is the integer
timestamp of the start of the current rolling bucket.

Public API
----------

* :class:`QuotaLimits` - per-provider limits (RPM, TPM, RPD, TPD).
* :class:`QuotaTracker` - SQLite-backed counter with :meth:`consume`,
  :meth:`available`, :meth:`record_429`, and a :meth:`pre_attempt_check`
  callable suitable for plugging straight into
  :class:`coracle.providers.fallback.FallbackChain`.
* :func:`load_default_limits` - parse ``coracle/config/quotas.toml``.

The module is intentionally dependency-free (stdlib :mod:`sqlite3` +
:mod:`tomllib`) so it can be imported from any provider adapter without
pulling in litellm.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_QUOTAS_PATH",
    "QuotaLimits",
    "QuotaTracker",
    "QuotaUsage",
    "load_default_limits",
]


DEFAULT_QUOTAS_PATH = Path(__file__).resolve().parent.parent / "config" / "quotas.toml"

_MINUTE = 60
_DAY = 86_400
_DEFAULT_COOLDOWN = 60


@dataclass(frozen=True)
class QuotaLimits:
    """Per-provider limits. ``0`` means "unlimited on this axis"."""

    rpm: int = 0
    tpm: int = 0
    rpd: int = 0
    tpd: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, int]) -> QuotaLimits:
        return cls(
            rpm=int(raw.get("rpm", 0)),
            tpm=int(raw.get("tpm", 0)),
            rpd=int(raw.get("rpd", 0)),
            tpd=int(raw.get("tpd", 0)),
        )


@dataclass(frozen=True)
class QuotaUsage:
    """Snapshot returned by :meth:`QuotaTracker.available`."""

    provider: str
    requests_minute: int
    tokens_minute: int
    requests_day: int
    tokens_day: int
    cooldown_remaining: float
    limits: QuotaLimits

    @property
    def exhausted(self) -> bool:
        if self.cooldown_remaining > 0:
            return True
        lim = self.limits
        if lim.rpm and self.requests_minute >= lim.rpm:
            return True
        if lim.tpm and self.tokens_minute >= lim.tpm:
            return True
        if lim.rpd and self.requests_day >= lim.rpd:
            return True
        return bool(lim.tpd and self.tokens_day >= lim.tpd)


def load_default_limits(path: Path | None = None) -> dict[str, QuotaLimits]:
    """Parse the bundled ``quotas.toml`` (or *path*) into a dict."""
    target = path or DEFAULT_QUOTAS_PATH
    with target.open("rb") as fh:
        raw = tomllib.load(fh)
    return {name: QuotaLimits.from_mapping(section) for name, section in raw.items()}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_quota (
    provider          TEXT    NOT NULL,
    window_kind       TEXT    NOT NULL,
    window_start_ts   INTEGER NOT NULL,
    used_tokens       INTEGER NOT NULL DEFAULT 0,
    used_requests     INTEGER NOT NULL DEFAULT 0,
    last_429_ts       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(provider, window_kind)
);
"""


class QuotaTracker:
    """SQLite-backed quota counter with TTL windows.

    Args:
        db_path: Path to the SQLite file. Use a real on-disk path for
            persistence across restarts; ``":memory:"`` is fine in tests
            within one process.
        limits: Mapping of provider id -> :class:`QuotaLimits`. Providers not
            present here are treated as "unlimited" (``QuotaLimits()``).
        clock: Injectable wall-clock (seconds since epoch). Defaults to
            :func:`time.time`. Use a fake clock in tests to drive window
            rollover deterministically.
        cooldown_seconds: How long to skip a provider after a recorded 429.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        limits: Mapping[str, QuotaLimits] | None = None,
        clock: Callable[[], float] = time.time,
        cooldown_seconds: int = _DEFAULT_COOLDOWN,
    ) -> None:
        self.db_path = db_path
        self.limits: Mapping[str, QuotaLimits] = dict(limits or {})
        self.clock = clock
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    # --- lifecycle --------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> QuotaTracker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- public API -------------------------------------------------------

    def limits_for(self, provider: str) -> QuotaLimits:
        return self.limits.get(provider, QuotaLimits())

    def consume(self, provider: str, tokens: int = 0, requests: int = 1) -> None:
        """Record *tokens* / *requests* against *provider* in both windows."""
        if tokens < 0 or requests < 0:
            raise ValueError("tokens and requests must be non-negative")
        now = int(self.clock())
        with self._lock:
            self._increment("minute", provider, tokens, requests, now, _MINUTE)
            self._increment("day", provider, tokens, requests, now, _DAY)

    def record_429(self, provider: str) -> None:
        """Mark *provider* as cooling-down after a 429 / quota error."""
        now = int(self.clock())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO provider_quota(provider, window_kind, window_start_ts, last_429_ts)
                VALUES(?, 'cooldown', ?, ?)
                ON CONFLICT(provider, window_kind) DO UPDATE SET
                    window_start_ts = excluded.window_start_ts,
                    last_429_ts     = excluded.last_429_ts
                """,
                (provider, now, now),
            )

    def available(self, provider: str) -> QuotaUsage:
        """Return a fresh :class:`QuotaUsage` snapshot for *provider*."""
        now = int(self.clock())
        with self._lock:
            req_m, tok_m = self._read("minute", provider, now, _MINUTE)
            req_d, tok_d = self._read("day", provider, now, _DAY)
            cooldown_remaining = self._cooldown_remaining(provider, now)
        return QuotaUsage(
            provider=provider,
            requests_minute=req_m,
            tokens_minute=tok_m,
            requests_day=req_d,
            tokens_day=tok_d,
            cooldown_remaining=cooldown_remaining,
            limits=self.limits_for(provider),
        )

    def pre_attempt_check(self, provider: str) -> bool:
        """Return ``True`` if *provider* still has quota.

        Designed to be wired into :class:`FallbackChain` as a single
        callable parameter::

            FallbackChain(providers, pre_attempt_check=tracker.pre_attempt_check)
        """
        return not self.available(provider).exhausted

    # --- internals --------------------------------------------------------

    @staticmethod
    def _bucket_start(now: int, size: int) -> int:
        return now - (now % size)

    def _increment(
        self,
        kind: str,
        provider: str,
        tokens: int,
        requests: int,
        now: int,
        size: int,
    ) -> None:
        bucket = self._bucket_start(now, size)
        cur = self._conn.execute(
            "SELECT window_start_ts FROM provider_quota WHERE provider = ? AND window_kind = ?",
            (provider, kind),
        )
        row = cur.fetchone()
        if row is None or row[0] != bucket:
            self._conn.execute(
                """
                INSERT INTO provider_quota(provider, window_kind, window_start_ts,
                                           used_tokens, used_requests)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(provider, window_kind) DO UPDATE SET
                    window_start_ts = excluded.window_start_ts,
                    used_tokens     = excluded.used_tokens,
                    used_requests   = excluded.used_requests
                """,
                (provider, kind, bucket, tokens, requests),
            )
        else:
            self._conn.execute(
                "UPDATE provider_quota SET used_tokens = used_tokens + ?, "
                "used_requests = used_requests + ? "
                "WHERE provider = ? AND window_kind = ?",
                (tokens, requests, provider, kind),
            )

    def _read(self, kind: str, provider: str, now: int, size: int) -> tuple[int, int]:
        bucket = self._bucket_start(now, size)
        cur = self._conn.execute(
            "SELECT window_start_ts, used_requests, used_tokens FROM provider_quota "
            "WHERE provider = ? AND window_kind = ?",
            (provider, kind),
        )
        row = cur.fetchone()
        if row is None or row[0] != bucket:
            return 0, 0
        return int(row[1]), int(row[2])

    def _cooldown_remaining(self, provider: str, now: int) -> float:
        cur = self._conn.execute(
            "SELECT last_429_ts FROM provider_quota "
            "WHERE provider = ? AND window_kind = 'cooldown'",
            (provider,),
        )
        row = cur.fetchone()
        if row is None or not row[0]:
            return 0.0
        elapsed = now - int(row[0])
        remaining = self.cooldown_seconds - elapsed
        return float(remaining) if remaining > 0 else 0.0
