"""Status mode A: instant point-in-time job snapshot.

This module exposes a :class:`Snapshot` dataclass and a :func:`snapshot`
function that builds a high-level, low-cost view of a job's progress
without invoking any LLM. Inputs are duck-typed against the in-memory
``Job`` model used by the FastAPI router and the SQLite-backed state
store: any object exposing ``id``, ``status``, ``steps``, ``events``
and (optionally) ``total_steps`` / ``started_at`` / ``model`` works.

A snapshot bundles the job's *phase* (lifecycle status), the *current
step*, completion percentage, an ETA derived from observed step rate,
the current RAM reading, and the model in use. The RAM sampler is
injectable so tests don't depend on the host's real memory state.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import psutil

__all__ = ["RamReading", "Snapshot", "snapshot"]

_BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class RamReading:
    """A single sample of system memory, sizes in megabytes."""

    used_mb: float
    available_mb: float
    total_mb: float


@dataclass(frozen=True)
class Snapshot:
    """Point-in-time snapshot of a job for status mode A."""

    job_id: str
    phase: str
    current_step: str | None
    steps_done: int
    total_steps: int | None
    percent: float
    eta_seconds: float | None
    ram_used_mb: float
    ram_available_mb: float
    ram_total_mb: float
    model: str | None
    captured_at: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict representation."""
        return asdict(self)


def _default_ram_sampler() -> RamReading:
    vm = psutil.virtual_memory()
    return RamReading(
        used_mb=vm.used / _BYTES_PER_MB,
        available_mb=vm.available / _BYTES_PER_MB,
        total_mb=vm.total / _BYTES_PER_MB,
    )


def _phase(value: Any) -> str:
    if value is None:
        return "unknown"
    inner = getattr(value, "value", value)
    return str(inner)


def _started_at(job: Any) -> float | None:
    events = getattr(job, "events", None) or ()
    for ev in events:
        ts = getattr(ev, "ts", None)
        if ts is not None:
            return float(ts)
    started = getattr(job, "started_at", None)
    if started is None:
        return None
    return float(started)


def _current_step(job: Any) -> str | None:
    steps = getattr(job, "steps", None) or ()
    if steps:
        last = steps[-1]
        name = last.get("name") if isinstance(last, dict) else getattr(last, "name", None)
        if name:
            return str(name)
    events = getattr(job, "events", None) or ()
    if events:
        kind = getattr(events[-1], "kind", None)
        if kind:
            return str(kind)
    return None


def _percent(steps_done: int, total: int | None) -> float:
    if not total or total <= 0:
        return 0.0
    return round(min(100.0, 100.0 * steps_done / total), 2)


def _eta(
    steps_done: int,
    total: int | None,
    started: float | None,
    captured_at: float,
) -> float | None:
    if not total or steps_done <= 0 or started is None:
        return None
    remaining = total - steps_done
    if remaining <= 0:
        return 0.0
    elapsed = captured_at - started
    if elapsed <= 0:
        return None
    rate = elapsed / steps_done
    return round(rate * remaining, 3)


def snapshot(
    job: Any,
    *,
    ram_sampler: Callable[[], RamReading] | None = None,
    now: Callable[[], float] | None = None,
) -> Snapshot:
    """Build a :class:`Snapshot` for ``job``.

    Args:
        job: Any object exposing ``id``, ``status``, ``steps`` (sequence
            of dicts or objects with ``name``), ``events`` (objects with
            ``kind`` and ``ts``), and optional ``total_steps``,
            ``started_at`` and ``model`` attributes.
        ram_sampler: Optional callable returning a :class:`RamReading`.
            Defaults to a ``psutil`` sampler.
        now: Optional callable returning the current epoch time in
            seconds. Defaults to :func:`time.time`.
    """
    sampler = ram_sampler or _default_ram_sampler
    clock = now or time.time

    ram = sampler()
    captured_at = float(clock())

    steps = getattr(job, "steps", None) or ()
    steps_done = len(steps)
    raw_total = getattr(job, "total_steps", None)
    total_steps = int(raw_total) if raw_total is not None else None
    started = _started_at(job)

    return Snapshot(
        job_id=str(getattr(job, "id", "") or ""),
        phase=_phase(getattr(job, "status", None)),
        current_step=_current_step(job),
        steps_done=steps_done,
        total_steps=total_steps,
        percent=_percent(steps_done, total_steps),
        eta_seconds=_eta(steps_done, total_steps, started, captured_at),
        ram_used_mb=ram.used_mb,
        ram_available_mb=ram.available_mb,
        ram_total_mb=ram.total_mb,
        model=getattr(job, "model", None),
        captured_at=captured_at,
    )
