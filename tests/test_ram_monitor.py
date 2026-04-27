"""Tests for orchestrator.core.ram_monitor."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator

import pytest

from orchestrator.core.ram_monitor import (
    RamMonitor,
    RamSnapshot,
    RamState,
)


def _snap(available_mb: float, *, total_mb: float = 16000.0, ts: float = 0.0) -> RamSnapshot:
    return RamSnapshot(
        available_mb=available_mb,
        total_mb=total_mb,
        used_mb=total_mb - available_mb,
        timestamp=ts,
    )


def _scripted_sampler(snapshots: list[RamSnapshot]) -> tuple[object, list[int]]:
    """Sampler that walks through ``snapshots`` and then sticks on the last one."""
    idx = [0]

    def sampler() -> RamSnapshot:
        i = min(idx[0], len(snapshots) - 1)
        idx[0] += 1
        return snapshots[i]

    return sampler, idx


def _make_monitor(
    snapshots: list[RamSnapshot] | None = None,
    *,
    soft: int = 7000,
    hard: int = 5000,
    poll: float = 0.01,
) -> tuple[RamMonitor, list[int]]:
    snaps = snapshots or [_snap(10000.0)]
    sampler, idx = _scripted_sampler(snaps)
    monitor = RamMonitor(
        soft_cap_mb=soft,
        hard_cap_mb=hard,
        poll_interval_s=poll,
        sampler=sampler,  # type: ignore[arg-type]
    )
    return monitor, idx


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------


def test_rejects_hard_cap_not_strictly_less_than_soft() -> None:
    with pytest.raises(ValueError, match="hard_cap_mb"):
        RamMonitor(soft_cap_mb=5000, hard_cap_mb=5000, poll_interval_s=1.0)
    with pytest.raises(ValueError, match="hard_cap_mb"):
        RamMonitor(soft_cap_mb=5000, hard_cap_mb=6000, poll_interval_s=1.0)


def test_rejects_non_positive_poll_interval() -> None:
    with pytest.raises(ValueError, match="poll_interval_s"):
        RamMonitor(soft_cap_mb=7000, hard_cap_mb=5000, poll_interval_s=0)


def test_current_snapshot_before_sampling_raises() -> None:
    monitor, _ = _make_monitor()
    with pytest.raises(RuntimeError, match="no snapshot"):
        monitor.current_snapshot()


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_cold_start_state_is_ok_and_sample_returns_snapshot() -> None:
    monitor, _ = _make_monitor([_snap(10000.0)])
    snap = monitor.sample()
    assert monitor.state is RamState.OK
    assert snap.available_mb == 10000.0
    assert monitor.current_snapshot() is snap


def test_under_soft_cap_stays_ok_no_callbacks() -> None:
    seen: list[str] = []
    monitor, _ = _make_monitor([_snap(9000.0), _snap(8000.0)])
    monitor.on_soft_breach(lambda s: seen.append("soft"))
    monitor.on_hard_breach(lambda s: seen.append("hard"))
    monitor.on_recovery(lambda s: seen.append("recovery"))
    monitor.sample()
    monitor.sample()
    assert seen == []
    assert monitor.state is RamState.OK


def test_ok_to_soft_to_hard_fires_each_callback_once() -> None:
    seen: list[str] = []
    snaps = [
        _snap(9000.0),  # ok
        _snap(6500.0),  # -> soft
        _snap(6000.0),  # stay soft, no re-fire
        _snap(4000.0),  # -> hard
        _snap(3500.0),  # stay hard, no re-fire
    ]
    monitor, _ = _make_monitor(snaps)
    monitor.on_soft_breach(lambda s: seen.append(f"soft@{s.available_mb}"))
    monitor.on_hard_breach(lambda s: seen.append(f"hard@{s.available_mb}"))
    monitor.on_recovery(lambda s: seen.append("recovery"))

    for _ in snaps:
        monitor.sample()

    assert seen == ["soft@6500.0", "hard@4000.0"]
    assert monitor.state is RamState.HARD


def test_kill_switch_fires_before_hard_listeners() -> None:
    order: list[str] = []
    monitor, _ = _make_monitor([_snap(9000.0), _snap(4000.0)])
    monitor.register_kill_switch(lambda: order.append("kill"))
    monitor.on_hard_breach(lambda s: order.append("hard"))

    monitor.sample()
    monitor.sample()

    assert order == ["kill", "hard"]


def test_kill_switch_only_fires_on_hard_not_soft() -> None:
    calls: list[str] = []
    monitor, _ = _make_monitor([_snap(6500.0), _snap(6400.0)])
    monitor.register_kill_switch(lambda: calls.append("kill"))
    monitor.sample()
    monitor.sample()
    assert calls == []
    assert monitor.state is RamState.SOFT


def test_recovery_hard_to_soft_to_ok_fires_recovery_once() -> None:
    seen: list[str] = []
    snaps = [
        _snap(4000.0),  # -> hard
        _snap(6500.0),  # -> soft
        _snap(9000.0),  # -> ok
        _snap(9500.0),  # stay ok
    ]
    monitor, _ = _make_monitor(snaps)
    monitor.on_soft_breach(lambda s: seen.append("soft"))
    monitor.on_hard_breach(lambda s: seen.append("hard"))
    monitor.on_recovery(lambda s: seen.append("recovery"))

    for _ in snaps:
        monitor.sample()

    assert seen == ["hard", "soft", "recovery"]
    assert monitor.state is RamState.OK


def test_hard_to_ok_directly_fires_recovery_once() -> None:
    seen: list[str] = []
    monitor, _ = _make_monitor([_snap(4000.0), _snap(9000.0)])
    monitor.on_hard_breach(lambda s: seen.append("hard"))
    monitor.on_recovery(lambda s: seen.append("recovery"))
    monitor.sample()
    monitor.sample()
    assert seen == ["hard", "recovery"]


def test_no_recovery_at_cold_start_when_already_ok() -> None:
    seen: list[str] = []
    monitor, _ = _make_monitor([_snap(10000.0)])
    monitor.on_recovery(lambda s: seen.append("recovery"))
    monitor.sample()
    assert seen == []


def test_oscillation_fires_each_transition() -> None:
    seen: list[str] = []
    snaps = [
        _snap(9000.0),  # ok
        _snap(6500.0),  # soft
        _snap(9000.0),  # ok (recovery)
        _snap(6500.0),  # soft again
        _snap(4000.0),  # hard
        _snap(6500.0),  # soft (no recovery yet)
        _snap(9000.0),  # ok (recovery)
    ]
    monitor, _ = _make_monitor(snaps)
    monitor.on_soft_breach(lambda s: seen.append("soft"))
    monitor.on_hard_breach(lambda s: seen.append("hard"))
    monitor.on_recovery(lambda s: seen.append("recovery"))
    for _ in snaps:
        monitor.sample()
    assert seen == ["soft", "recovery", "soft", "hard", "soft", "recovery"]


# ---------------------------------------------------------------------------
# Listener resilience
# ---------------------------------------------------------------------------


def test_listener_exception_does_not_break_other_listeners_or_state() -> None:
    seen: list[str] = []

    def bad(_: RamSnapshot) -> None:
        raise RuntimeError("boom")

    monitor, _ = _make_monitor([_snap(9000.0), _snap(6500.0)])
    monitor.on_soft_breach(bad)
    monitor.on_soft_breach(lambda s: seen.append("good"))
    monitor.sample()
    monitor.sample()
    assert seen == ["good"]
    assert monitor.state is RamState.SOFT


def test_kill_switch_exception_is_swallowed_and_hard_listeners_still_fire() -> None:
    seen: list[str] = []

    def bad_kill() -> None:
        raise RuntimeError("kill boom")

    monitor, _ = _make_monitor([_snap(4000.0)])
    monitor.register_kill_switch(bad_kill)
    monitor.on_hard_breach(lambda s: seen.append("hard"))
    monitor.sample()
    assert seen == ["hard"]
    assert monitor.state is RamState.HARD


def test_register_kill_switch_replaces_previous() -> None:
    calls: list[str] = []
    monitor, _ = _make_monitor([_snap(4000.0)])
    monitor.register_kill_switch(lambda: calls.append("first"))
    monitor.register_kill_switch(lambda: calls.append("second"))
    monitor.sample()
    assert calls == ["second"]


# ---------------------------------------------------------------------------
# Thread lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def stoppable_monitor() -> Iterator[RamMonitor]:
    monitor, _ = _make_monitor([_snap(9000.0)] * 100, poll=0.005)
    try:
        yield monitor
    finally:
        monitor.stop(timeout=2.0)


def test_start_then_stop_is_clean(stoppable_monitor: RamMonitor) -> None:
    stoppable_monitor.start()
    deadline = time.time() + 1.0
    while time.time() < deadline:
        try:
            stoppable_monitor.current_snapshot()
            break
        except RuntimeError:
            time.sleep(0.01)
    else:
        pytest.fail("monitor never produced a snapshot")
    stoppable_monitor.stop(timeout=2.0)
    assert stoppable_monitor._thread is None


def test_start_is_idempotent(stoppable_monitor: RamMonitor) -> None:
    stoppable_monitor.start()
    first = stoppable_monitor._thread
    stoppable_monitor.start()
    assert stoppable_monitor._thread is first


def test_stop_without_start_is_safe() -> None:
    monitor, _ = _make_monitor()
    monitor.stop()  # must not raise


def test_sampler_exception_does_not_kill_poller() -> None:
    calls = {"n": 0}
    delivered: list[RamSnapshot] = []

    def flaky() -> RamSnapshot:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first sample fails")
        snap = _snap(9000.0, ts=float(calls["n"]))
        delivered.append(snap)
        return snap

    monitor = RamMonitor(
        soft_cap_mb=7000,
        hard_cap_mb=5000,
        poll_interval_s=0.005,
        sampler=flaky,
    )
    monitor.start()
    deadline = time.time() + 1.0
    while time.time() < deadline and len(delivered) < 2:
        time.sleep(0.01)
    monitor.stop(timeout=2.0)
    assert len(delivered) >= 2


def test_context_manager_starts_and_stops() -> None:
    monitor, _ = _make_monitor([_snap(9000.0)] * 50, poll=0.005)
    with monitor as m:
        assert m is monitor
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                m.current_snapshot()
                break
            except RuntimeError:
                time.sleep(0.01)
    assert monitor._thread is None


# ---------------------------------------------------------------------------
# Default psutil sampler
# ---------------------------------------------------------------------------


def test_default_sampler_uses_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.core import ram_monitor as rm

    class FakeVM:
        available = 8 * 1024 * 1024 * 1024
        total = 16 * 1024 * 1024 * 1024
        used = 8 * 1024 * 1024 * 1024

    monkeypatch.setattr(rm.psutil, "virtual_memory", lambda: FakeVM())
    snap = rm._psutil_sampler()
    assert snap.available_mb == pytest.approx(8192.0)
    assert snap.total_mb == pytest.approx(16384.0)
    assert snap.used_mb == pytest.approx(8192.0)
    assert snap.timestamp > 0


def test_default_constructor_uses_psutil_sampler(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.core import ram_monitor as rm

    class FakeVM:
        available = 10 * 1024 * 1024 * 1024
        total = 16 * 1024 * 1024 * 1024
        used = 6 * 1024 * 1024 * 1024

    monkeypatch.setattr(rm.psutil, "virtual_memory", lambda: FakeVM())
    monitor = RamMonitor(soft_cap_mb=7000, hard_cap_mb=5000, poll_interval_s=1.0)
    snap = monitor.sample()
    assert snap.available_mb == pytest.approx(10240.0)
    assert monitor.state is RamState.OK


# ---------------------------------------------------------------------------
# Concurrency sanity
# ---------------------------------------------------------------------------


def test_state_reads_are_thread_safe() -> None:
    monitor, _ = _make_monitor([_snap(9000.0)] * 200, poll=0.001)
    monitor.start()
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            for _ in range(50):
                _ = monitor.state
                with contextlib.suppress(RuntimeError):
                    monitor.current_snapshot()
        except BaseException as exc:  # pragma: no cover - safety net
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monitor.stop(timeout=2.0)
    assert errors == []
