"""Optional OpenTelemetry exporter for :class:`AuditEvent`.

The OpenTelemetry SDK is *not* a hard dependency. The bridge below is
imported lazily and only attempted when the user actually constructs an
:class:`OTelExporter`. Tests can inject a fake transport via the
``transport`` parameter so they can run without the ``opentelemetry-*``
packages installed.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from orchestrator.observability.audit import AuditEvent

__all__ = ["OTelExporter", "otel_available"]


def otel_available() -> bool:
    """Return ``True`` iff the optional OpenTelemetry SDK can be imported."""
    try:
        importlib.import_module("opentelemetry.trace")
        importlib.import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        return True
    except ImportError:
        return False


class OTelExporter:
    """Best-effort OTLP/HTTP exporter for audit events.

    Parameters
    ----------
    endpoint:
        OTLP HTTP traces endpoint, e.g. ``http://localhost:4318/v1/traces``.
    transport:
        Optional callable ``(event) -> None`` used instead of the real OTLP
        client. Tests pass a fake here; production code leaves it ``None``
        and the real OTLP client is constructed lazily.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        transport: Callable[[AuditEvent], None] | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint must be a non-empty string")
        self.endpoint = endpoint
        self._transport = transport
        self._real_exporter: Any | None = None
        if transport is None:
            # Validate the optional dependency is available *now* so misconfig
            # surfaces at startup rather than on the first hot-path event.
            if not otel_available():
                raise RuntimeError(
                    "OTel exporter requested but the 'opentelemetry-*' packages "
                    "are not installed. Install with: pip install 'orchestrator[otel]'"
                )
            self._real_exporter = self._build_real_exporter(endpoint)

    @staticmethod
    def _build_real_exporter(endpoint: str) -> Any:  # pragma: no cover - needs extra
        module = importlib.import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        return module.OTLPSpanExporter(endpoint=endpoint)

    def export(self, event: AuditEvent) -> None:
        if self._transport is not None:
            self._transport(event)
            return
        self._export_real(event)  # pragma: no cover - needs extra installed

    def _export_real(self, event: AuditEvent) -> None:  # pragma: no cover
        # Build a minimal OTel-compatible attribute dict and ship it. The real
        # span construction depends on the user's tracer provider; we keep
        # this thin so the extra remains optional.
        attributes = {
            "audit.id": event.id,
            "audit.actor": event.actor,
            "audit.action": event.action,
            "audit.status": event.status,
        }
        if event.target is not None:
            attributes["audit.target"] = event.target
        if event.latency_ms is not None:
            attributes["audit.latency_ms"] = event.latency_ms
        self._real_exporter.export([attributes])  # type: ignore[union-attr]
