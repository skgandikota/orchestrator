"""Tests for ``coracle.observability``."""

from __future__ import annotations

import builtins
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from coracle import observability
from coracle.observability import (
    MAX_PAYLOAD_BYTES,
    AuditEvent,
    AuditLog,
    OTelExporter,
    configure_default_log,
    get_default_log,
    otel_available,
    query,
    record,
    reset_default_log,
)
from coracle.observability import audit as audit_mod
from coracle.observability import otel as otel_mod


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.sqlite"


@pytest.fixture()
def log(db_path: Path):
    log = AuditLog(db_path, queue_size=64, batch_size=8, flush_interval=0.01)
    yield log
    log.close()


# --- AuditEvent schema -------------------------------------------------------


def test_audit_event_truncates_oversized_payload():
    big = "x" * (MAX_PAYLOAD_BYTES + 100)
    ev = AuditEvent(actor="a", action="b", payload_json=big)
    assert ev.payload_json is not None
    assert ev.payload_json.endswith("...[truncated]")
    assert len(ev.payload_json.encode("utf-8")) <= MAX_PAYLOAD_BYTES + len("...[truncated]")


def test_audit_event_keeps_small_payload():
    ev = AuditEvent(actor="a", action="b", payload_json="hello")
    assert ev.payload_json == "hello"
    ev_none = AuditEvent(actor="a", action="b")
    assert ev_none.payload_json is None


def test_audit_event_normalises_naive_ts_to_utc():
    naive = datetime(2024, 1, 1, 12, 0, 0)
    ev = AuditEvent(actor="a", action="b", ts=naive)
    assert ev.ts.tzinfo is UTC
    aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    ev2 = AuditEvent(actor="a", action="b", ts=aware)
    assert ev2.ts.utcoffset() == timedelta(0)


# --- record + flush ----------------------------------------------------------


def test_record_persists_to_sqlite(log: AuditLog, db_path: Path):
    log.record(
        "scheduler",
        "model_swap",
        target="llama3:8b",
        latency_ms=12.5,
        tokens_in=10,
        tokens_out=20,
        cost_estimate_usd=0.0,
        payload={"reason": "ram_pressure"},
    )
    log.flush()
    time.sleep(0.05)

    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT actor, action, target, latency_ms FROM audit_events").fetchall()
    con.close()
    assert ("scheduler", "model_swap", "llama3:8b", 12.5) in rows


def test_record_synchronous_returns_event_immediately(log: AuditLog):
    ev = log.record("scheduler", "tick")
    assert isinstance(ev, AuditEvent)
    assert ev.actor == "scheduler"
    assert ev.status == "ok"


def test_query_filters_by_actor_action_status_window(log: AuditLog):
    base = datetime.now(UTC)
    log.record("a1", "act1", status="ok")
    log.record("a1", "act2", status="error")
    log.record("a2", "act1", status="ok")
    log.flush()
    time.sleep(0.05)

    assert {e.actor for e in log.query(actor="a1")} == {"a1"}
    assert {e.action for e in log.query(action="act1")} == {"act1"}
    assert {e.status for e in log.query(status="error")} == {"error"}
    since = base - timedelta(minutes=1)
    until = base + timedelta(minutes=1)
    assert len(log.query(since=since, until=until, limit=10)) >= 3


def test_query_limit_is_respected(log: AuditLog):
    for i in range(5):
        log.record("bulk", "evt", payload={"i": i})
    log.flush()
    time.sleep(0.05)
    assert len(log.query(actor="bulk", limit=2)) == 2


# --- coercion + non-json payload --------------------------------------------


def test_record_coerces_non_serialisable_payload(log: AuditLog):
    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    ev = log.record("x", "y", payload=Weird())
    assert ev.payload_json is not None
    assert "weird" in ev.payload_json


def test_record_passes_through_string_payload(log: AuditLog):
    ev = log.record("x", "y", payload="raw")
    assert ev.payload_json == "raw"


# --- queue overflow / drop-oldest -------------------------------------------


def test_queue_overflow_drops_oldest_and_emits_metric(db_path: Path):
    # Don't auto-start the writer so we can deliberately overflow.
    log = AuditLog(db_path, queue_size=4, batch_size=8, flush_interval=0.01, start=False)
    try:
        for i in range(10):
            log.record("flood", "evt", payload={"i": i})
        assert log.dropped == 6
        log.start()
        log.flush()
        time.sleep(0.1)
        events = log.query(actor="flood", limit=20)
        # Only the last 4 events should have survived the drop-oldest queue.
        assert len(events) == 4
        ids = sorted(json.loads(e.payload_json or "{}").get("i", -1) for e in events)
        assert ids == [6, 7, 8, 9]
        overflow = log.query(actor="audit", action="queue_overflow", limit=5)
        assert overflow, "an overflow marker event should be persisted"
        assert json.loads(overflow[0].payload_json or "{}")["dropped_total"] == 6
    finally:
        log.close()


def test_invalid_queue_size_rejected(db_path: Path):
    with pytest.raises(ValueError):
        AuditLog(db_path, queue_size=0)


# --- background writer lifecycle --------------------------------------------


def test_close_drains_remaining_events(db_path: Path):
    log = AuditLog(db_path, queue_size=32, batch_size=4, flush_interval=0.01)
    for i in range(20):
        log.record("late", "evt", payload={"i": i})
    log.close()
    persisted = AuditLog(db_path).query(actor="late", limit=50)
    assert len(persisted) == 20


def test_start_is_idempotent(log: AuditLog):
    log.start()
    log.start()
    assert log._thread is not None and log._thread.is_alive()


def test_context_manager_closes(db_path: Path):
    with AuditLog(db_path) as log:
        log.record("ctx", "evt")
    # Re-open and verify the event survived.
    persisted = AuditLog(db_path).query(actor="ctx")
    assert any(e.action == "evt" for e in persisted)


# --- exporter integration ---------------------------------------------------


def test_exporter_called_for_each_event(db_path: Path):
    seen: list[AuditEvent] = []

    class FakeExporter:
        def export(self, event: AuditEvent) -> None:
            seen.append(event)

    log = AuditLog(
        db_path, queue_size=16, batch_size=4, flush_interval=0.01, exporter=FakeExporter()
    )
    try:
        log.record("a", "x")
        log.record("a", "y")
        log.flush()
        time.sleep(0.1)
    finally:
        log.close()
    assert {e.action for e in seen} == {"x", "y"}


def test_exporter_failures_dont_kill_writer(db_path: Path):
    class Boom:
        def export(self, event: AuditEvent) -> None:
            raise RuntimeError("nope")

    log = AuditLog(db_path, queue_size=4, batch_size=2, flush_interval=0.01, exporter=Boom())
    try:
        log.record("a", "x")
        log.flush()
        time.sleep(0.05)
        assert log.query(actor="a")  # event still persisted
    finally:
        log.close()


# --- OTel exporter gating ---------------------------------------------------


def test_otel_exporter_uses_injected_transport():
    sent: list[AuditEvent] = []
    exp = OTelExporter("http://localhost:4318/v1/traces", transport=sent.append)
    ev = AuditEvent(actor="a", action="b", target="t", latency_ms=1.0)
    exp.export(ev)
    assert sent == [ev]
    assert exp.endpoint.endswith("/v1/traces")


def test_otel_exporter_rejects_empty_endpoint():
    with pytest.raises(ValueError):
        OTelExporter("", transport=lambda _ev: None)


def test_otel_exporter_raises_when_extra_missing(monkeypatch):
    """Without a transport AND without the extra installed, construction must fail."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("simulated missing extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Drop any cached OTel modules so the import path actually re-executes.
    for k in [k for k in list(sys.modules) if k.startswith("opentelemetry")]:
        sys.modules.pop(k, None)

    assert otel_mod.otel_available() is False
    with pytest.raises(RuntimeError, match="opentelemetry"):
        OTelExporter("http://localhost:4318/v1/traces")


def test_otel_available_handles_present_modules(monkeypatch):
    # Stand-in modules so importlib.import_module succeeds.
    import types

    fake_trace = types.ModuleType("opentelemetry.trace")
    fake_pkg = types.ModuleType("opentelemetry")
    fake_exp = types.ModuleType("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_pkg)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        fake_exp,
    )
    assert otel_available() is True


# --- module-level default ---------------------------------------------------


def test_default_log_round_trip(tmp_path: Path):
    reset_default_log()
    try:
        log = AuditLog(tmp_path / "default.sqlite")
        configure_default_log(log)
        assert get_default_log() is log
        record("default", "evt", target="t", payload={"k": 1})
        log.flush()
        time.sleep(0.05)
        results = query(actor="default")
        assert any(e.action == "evt" for e in results)
    finally:
        reset_default_log()


def test_get_default_log_creates_in_memory_log_lazily():
    reset_default_log()
    try:
        log = get_default_log()
        assert log.db_path == ":memory:"
        # Calling again returns the same instance.
        assert get_default_log() is log
    finally:
        reset_default_log()


def test_configure_default_log_closes_previous(tmp_path: Path):
    reset_default_log()
    try:
        first = AuditLog(tmp_path / "a.sqlite")
        configure_default_log(first)
        second = AuditLog(tmp_path / "b.sqlite")
        configure_default_log(second)
        # First should be closed (its writer thread joined).
        assert first._thread is None
        assert get_default_log() is second
    finally:
        reset_default_log()


def test_purge_for_tests_clears_table(log: AuditLog):
    log.record("a", "b")
    log.flush()
    time.sleep(0.05)
    assert log.query(actor="a")
    log.purge_for_tests()
    assert log.query(actor="a") == []


def test_module_reexports_match_audit_module():
    # Sanity: the public API is what we advertise.
    assert observability.AuditEvent is audit_mod.AuditEvent
    assert observability.record is audit_mod.record
