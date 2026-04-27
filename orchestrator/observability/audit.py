"""SQLite-backed append-only audit log with a background writer thread.

The :class:`AuditLog` is the single sink for every observable decision the
orchestrator makes (classifier verdicts, model swaps, tool calls, big-AI
calls, errors). The public :func:`record` API is *fast* — it serialises the
event in the caller's thread, drops it onto a bounded in-memory queue and
returns. A daemon writer thread flushes batches to SQLite (and, optionally,
to an OpenTelemetry exporter).

Design notes
------------
* **Append-only** — there is no UPDATE/DELETE in the public API. Tests use
  :meth:`AuditLog.purge_for_tests` to keep DB sizes small.
* **Bounded queue, drop-oldest** — when the queue is full the oldest
  pending event is dropped so the producer never blocks. The drop count
  is exposed via :attr:`AuditLog.dropped` and emitted as a synthetic
  ``audit.queue_overflow`` event so the loss is visible downstream.
* **Best-effort OTel** — the exporter is optional; if the user does not
  pass one (or has not installed the ``[otel]`` extra) the audit log
  silently writes to SQLite only.
"""

from __future__ import annotations

import collections
import contextlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "AuditEvent",
    "AuditLog",
    "Exporter",
    "configure_default_log",
    "get_default_log",
    "query",
    "record",
    "reset_default_log",
]

MAX_PAYLOAD_BYTES = 8 * 1024
_DEFAULT_QUEUE_SIZE = 10_000
_DEFAULT_BATCH_SIZE = 256
_DEFAULT_FLUSH_INTERVAL = 0.05


class Exporter(Protocol):
    """Anything with a synchronous ``export`` method that takes an event."""

    def export(self, event: AuditEvent) -> None:  # pragma: no cover - protocol
        ...


class AuditEvent(BaseModel):
    """A single immutable record of something the orchestrator did."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: str
    action: str
    target: str | None = None
    status: str = "ok"
    latency_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_estimate_usd: float | None = None
    payload_json: str | None = None

    @field_validator("payload_json")
    @classmethod
    def _truncate_payload(cls, v: str | None) -> str | None:
        if v is None:
            return None
        encoded = v.encode("utf-8")
        if len(encoded) <= MAX_PAYLOAD_BYTES:
            return v
        return encoded[:MAX_PAYLOAD_BYTES].decode("utf-8", errors="ignore") + "...[truncated]"

    @field_validator("ts")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v.astimezone(UTC)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    status TEXT NOT NULL,
    latency_ms REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_estimate_usd REAL,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor_action ON audit_events(actor, action);
"""

_INSERT = (
    "INSERT OR IGNORE INTO audit_events "
    "(id, ts, actor, action, target, status, latency_ms, tokens_in, "
    "tokens_out, cost_estimate_usd, payload_json) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _coerce_payload(payload: Any) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return json.dumps({"repr": repr(payload)})


class AuditLog:
    """SQLite-backed audit log with a background writer thread."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
        exporter: Exporter | None = None,
        start: bool = True,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self.db_path = str(db_path)
        self._queue: collections.deque[AuditEvent] = collections.deque(maxlen=queue_size)
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._batch_size = max(1, batch_size)
        self._flush_interval = max(0.0, flush_interval)
        self._exporter = exporter
        self._thread: threading.Thread | None = None
        self.dropped = 0
        self._init_schema()
        if start:
            self.start()

    # --- lifecycle --------------------------------------------------------

    def _init_schema(self) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audit-writer", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        # Final drain for anything queued after the thread exit signal.
        while True:
            with self._cond:
                remaining = self._drain_locked()
            if not remaining:
                break
            self._flush(remaining)

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- producer side ----------------------------------------------------

    def record(
        self,
        actor: str,
        action: str,
        target: str | None = None,
        *,
        status: str = "ok",
        latency_ms: float | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_estimate_usd: float | None = None,
        payload: Any = None,
    ) -> AuditEvent:
        event = AuditEvent(
            actor=actor,
            action=action,
            target=target,
            status=status,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_estimate_usd=cost_estimate_usd,
            payload_json=_coerce_payload(payload),
        )
        self._enqueue(event)
        return event

    def _enqueue(self, event: AuditEvent) -> None:
        with self._cond:
            if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
                self.dropped += 1
                # deque automatically drops the leftmost (oldest) on append.
            self._queue.append(event)
            self._cond.notify()

    def flush(self, timeout: float = 2.0) -> None:
        """Block until the queue is empty (test/diagnostic helper)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cond:
                if not self._queue:
                    return
            time.sleep(0.005)

    # --- consumer side ----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._wait_batch()
            if batch:
                self._flush(batch)
            self._maybe_emit_overflow()

    def _wait_batch(self) -> list[AuditEvent]:
        with self._cond:
            if not self._queue:
                self._cond.wait(timeout=self._flush_interval)
            return self._drain_locked()

    def _drain_locked(self) -> list[AuditEvent]:
        items: list[AuditEvent] = []
        while self._queue and len(items) < self._batch_size:
            items.append(self._queue.popleft())
        return items

    def _flush(self, batch: Iterable[AuditEvent]) -> None:
        rows = [
            (
                e.id,
                e.ts.isoformat(),
                e.actor,
                e.action,
                e.target,
                e.status,
                e.latency_ms,
                e.tokens_in,
                e.tokens_out,
                e.cost_estimate_usd,
                e.payload_json,
            )
            for e in batch
        ]
        con = sqlite3.connect(self.db_path)
        try:
            con.executemany(_INSERT, rows)
            con.commit()
        finally:
            con.close()
        if self._exporter is not None:
            for event in batch:
                with contextlib.suppress(Exception):
                    self._exporter.export(event)

    def _maybe_emit_overflow(self) -> None:
        # Emit a single synthetic event each time the dropped counter advances
        # so downstream tooling can alarm on sustained overflow.
        if self.dropped and getattr(self, "_overflow_reported", 0) != self.dropped:
            self._overflow_reported = self.dropped
            event = AuditEvent(
                actor="audit",
                action="queue_overflow",
                target=None,
                status="warn",
                payload_json=json.dumps({"dropped_total": self.dropped}),
            )
            con = sqlite3.connect(self.db_path)
            try:
                con.execute(
                    _INSERT,
                    (
                        event.id,
                        event.ts.isoformat(),
                        event.actor,
                        event.action,
                        event.target,
                        event.status,
                        event.latency_ms,
                        event.tokens_in,
                        event.tokens_out,
                        event.cost_estimate_usd,
                        event.payload_json,
                    ),
                )
                con.commit()
            finally:
                con.close()

    # --- query ------------------------------------------------------------

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if actor is not None:
            clauses.append("actor = ?")
            params.append(actor)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until.astimezone(UTC).isoformat())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, ts, actor, action, target, status, latency_ms, "
            "tokens_in, tokens_out, cost_estimate_usd, payload_json "
            f"FROM audit_events {where} ORDER BY ts DESC, id DESC LIMIT ?"
        )
        params.append(int(limit))
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute(sql, params)
            rows = cur.fetchall()
        finally:
            con.close()
        return [
            AuditEvent(
                id=row[0],
                ts=datetime.fromisoformat(row[1]),
                actor=row[2],
                action=row[3],
                target=row[4],
                status=row[5],
                latency_ms=row[6],
                tokens_in=row[7],
                tokens_out=row[8],
                cost_estimate_usd=row[9],
                payload_json=row[10],
            )
            for row in rows
        ]

    def purge_for_tests(self) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("DELETE FROM audit_events")
            con.commit()
        finally:
            con.close()


# --- module-level default singleton ------------------------------------------

_default_lock = threading.Lock()
_default_log: AuditLog | None = None


def configure_default_log(log: AuditLog) -> AuditLog:
    """Install ``log`` as the process-wide default; closes any prior log."""
    global _default_log
    with _default_lock:
        if _default_log is not None and _default_log is not log:
            _default_log.close()
        _default_log = log
    return log


def reset_default_log() -> None:
    """Tear down the default log (used between tests)."""
    global _default_log
    with _default_lock:
        if _default_log is not None:
            _default_log.close()
        _default_log = None


def get_default_log() -> AuditLog:
    """Return the default :class:`AuditLog`, creating an in-memory one if needed."""
    global _default_log
    with _default_lock:
        if _default_log is None:
            _default_log = AuditLog(":memory:")
        return _default_log


def record(actor: str, action: str, target: str | None = None, **fields: Any) -> AuditEvent:
    """Record an event on the default audit log."""
    return get_default_log().record(actor, action, target, **fields)


def query(**kwargs: Any) -> list[AuditEvent]:
    """Query the default audit log."""
    return get_default_log().query(**kwargs)
