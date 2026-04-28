"""Observability primitives: structured audit log + optional OTel exporter."""

from __future__ import annotations

from coracle.observability.audit import (
    MAX_PAYLOAD_BYTES,
    AuditEvent,
    AuditLog,
    configure_default_log,
    get_default_log,
    query,
    record,
    reset_default_log,
)
from coracle.observability.otel import OTelExporter, otel_available

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "AuditEvent",
    "AuditLog",
    "OTelExporter",
    "configure_default_log",
    "get_default_log",
    "otel_available",
    "query",
    "record",
    "reset_default_log",
]
