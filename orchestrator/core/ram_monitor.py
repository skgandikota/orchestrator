"""Background RAM monitor with soft/hard caps and a kill-switch.

This module is the safety net behind the scheduler. A daemon thread polls
available system memory at a configurable interval and dispatches callbacks
when free memory crosses the configured *soft* and *hard* thresholds (the
caps represent **minimum free RAM**, so a smaller available value is worse).

The monitor is built around a pure ``_evaluate`` function so the threshold
state machine can be unit tested without spawning threads or touching
``psutil``. The default sampler uses ``psutil.virtual_memory`` but can be
swapped out via the constructor.
"""

from __future__ import annotations

import enum
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psutil
import structlog

__all__ = [
    "RamMonitor",
    "RamSnapshot",
    "RamState",
]

_log = structlog.get_logger(__name__)

_BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class RamSnapshot:
    """Point-in-time view of system memory, all sizes in megabytes."""

    available_mb: float
    total_mb: float
    used_mb: float
    timestamp: float


class RamState(enum.Enum):
    """Coarse health states for the threshold state machine."""

    OK = "ok"
    SOFT = "soft"
    HARD = "hard"


Callback = Callable[[RamSnapshot], None]
KillSwitch = Callable[[], None]


def _psutil_sampler() -> RamSnapshot:
    vm = psutil.virtual_memory()
    return RamSnapshot(
        available_mb=vm.available / _BYTES_PER_MB,
        total_mb=vm.total / _BYTES_PER_MB,
        used_mb=vm.used / _BYTES_PER_MB,
        timestamp=time.time(),
    )


@dataclass
class _Listeners:
    soft: list[Callback] = field(default_factory=list)
    hard: list[Callback] = field(default_factory=list)
    recovery: list[Callback] = field(default_factory=list)


class RamMonitor:
    """Polls system memory and fires callbacks on threshold transitions.

    Args:
        soft_cap_mb: Minimum free RAM (MB) before a *soft* breach fires.
        hard_cap_mb: Minimum free RAM (MB) before a *hard* breach fires.
            Must be strictly less than ``soft_cap_mb``.
        poll_interval_s: Seconds between samples while the poller runs.
        sampler: Callable returning the current :class:`RamSnapshot`.
            Override in tests to inject deterministic readings.
    """

    def __init__(
        self,
        soft_cap_mb: int,
        hard_cap_mb: int,
        poll_interval_s: float = 1.0,
        *,
        sampler: Callable[[], RamSnapshot] = _psutil_sampler,
    ) -> None:
        if hard_cap_mb >= soft_cap_mb:
            raise ValueError("hard_cap_mb must be < soft_cap_mb (caps are minimum free RAM)")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be > 0")

        self._soft_cap_mb = soft_cap_mb
        self._hard_cap_mb = hard_cap_mb
        self._poll_interval_s = poll_interval_s
        self._sampler = sampler

        self._listeners = _Listeners()
        self._kill_switch: KillSwitch | None = None

        self._state: RamState = RamState.OK
        self._snapshot: RamSnapshot | None = None
        self._lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- Registration ---------------------------------------------------

    def on_soft_breach(self, cb: Callback) -> None:
        """Register a callback fired on each ``ok|hard -> soft`` transition."""
        self._listeners.soft.append(cb)

    def on_hard_breach(self, cb: Callback) -> None:
        """Register a callback fired on each ``* -> hard`` transition."""
        self._listeners.hard.append(cb)

    def on_recovery(self, cb: Callback) -> None:
        """Register a callback fired when state returns to ``ok``."""
        self._listeners.recovery.append(cb)

    def register_kill_switch(self, cb: KillSwitch) -> None:
        """Register the synchronous kill-switch invoked first on hard breach.

        Only one kill-switch can be registered; later calls replace earlier
        ones. The kill-switch is intended to unload models / cancel in-flight
        loads before any normal listener runs.
        """
        self._kill_switch = cb

    # -- Read-side ------------------------------------------------------

    def current_snapshot(self) -> RamSnapshot:
        """Return the most recently sampled snapshot.

        Raises:
            RuntimeError: If no sample has been taken yet (call :meth:`sample`
                or :meth:`start` first).
        """
        with self._lock:
            snap = self._snapshot
        if snap is None:
            raise RuntimeError("RamMonitor has no snapshot yet; call sample() or start()")
        return snap

    @property
    def state(self) -> RamState:
        with self._lock:
            return self._state

    # -- Core logic -----------------------------------------------------

    def _evaluate(self, snapshot: RamSnapshot, prev: RamState) -> RamState:
        """Pure threshold classifier; returns the new state for ``snapshot``."""
        if snapshot.available_mb < self._hard_cap_mb:
            return RamState.HARD
        if snapshot.available_mb < self._soft_cap_mb:
            return RamState.SOFT
        return RamState.OK

    def sample(self) -> RamSnapshot:
        """Take one sample, dispatch transition callbacks, return the snapshot."""
        snapshot = self._sampler()
        with self._lock:
            prev = self._state
            new_state = self._evaluate(snapshot, prev)
            self._snapshot = snapshot
            self._state = new_state

        if new_state is not prev:
            self._dispatch(prev, new_state, snapshot)
        return snapshot

    def _dispatch(self, prev: RamState, new: RamState, snapshot: RamSnapshot) -> None:
        if new is RamState.HARD:
            self._fire_kill_switch()
            for cb in list(self._listeners.hard):
                self._safe_call(cb, snapshot, "on_hard_breach")
        elif new is RamState.SOFT:
            for cb in list(self._listeners.soft):
                self._safe_call(cb, snapshot, "on_soft_breach")
        elif new is RamState.OK:
            for cb in list(self._listeners.recovery):
                self._safe_call(cb, snapshot, "on_recovery")

    def _fire_kill_switch(self) -> None:
        cb = self._kill_switch
        if cb is None:
            return
        try:
            cb()
        except Exception:
            _log.exception("ram_monitor.kill_switch_failed")

    @staticmethod
    def _safe_call(cb: Callback, snapshot: RamSnapshot, label: str) -> None:
        try:
            cb(snapshot)
        except Exception:
            _log.exception("ram_monitor.listener_failed", listener=label)

    # -- Thread lifecycle ----------------------------------------------

    def start(self) -> None:
        """Start the daemon poller thread. Idempotent: a second call is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="ram-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Signal the poller to stop and join the thread."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.sample()
            except Exception:
                _log.exception("ram_monitor.sample_failed")
            self._stop_event.wait(self._poll_interval_s)

    # -- Context manager sugar -----------------------------------------

    def __enter__(self) -> RamMonitor:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
