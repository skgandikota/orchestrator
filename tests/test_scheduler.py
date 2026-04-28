"""Tests for the single-LLM-slot scheduler."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pytest

from coracle.core.scheduler import (
    InsufficientRamError,
    LlmSlotScheduler,
    SlotHandle,
    SwapError,
)


@dataclass
class _Calls:
    load: list[str] = field(default_factory=list)
    unload: list[str] = field(default_factory=list)
    verify_loaded: list[str] = field(default_factory=list)
    verify_unloaded: list[str] = field(default_factory=list)
    order: list[tuple[str, str]] = field(default_factory=list)


def _fake_adapter(
    calls: _Calls,
    *,
    load_raises: BaseException | None = None,
    unload_raises: BaseException | None = None,
    verify_loaded_returns: bool = True,
    verify_unloaded_returns: bool = True,
):
    def load(model_id: str) -> None:
        calls.load.append(model_id)
        calls.order.append(("load", model_id))
        if load_raises is not None:
            raise load_raises

    def unload(model_id: str) -> None:
        calls.unload.append(model_id)
        calls.order.append(("unload", model_id))
        if unload_raises is not None:
            raise unload_raises

    def verify_loaded(model_id: str) -> bool:
        calls.verify_loaded.append(model_id)
        calls.order.append(("verify_loaded", model_id))
        return verify_loaded_returns

    def verify_unloaded(model_id: str) -> bool:
        calls.verify_unloaded.append(model_id)
        calls.order.append(("verify_unloaded", model_id))
        return verify_unloaded_returns

    return load, unload, verify_loaded, verify_unloaded


@dataclass
class _Snap:
    available_mb: float


class _FakeRam:
    def __init__(self, available_mb: float) -> None:
        self.available_mb = available_mb

    def current_snapshot(self) -> _Snap:
        return _Snap(self.available_mb)


def _register(sched: LlmSlotScheduler, model_id: str, calls: _Calls, **kw) -> None:
    load, unload, vl, vu = _fake_adapter(calls, **kw)
    sched.register_adapter(model_id, load=load, unload=unload, verify_loaded=vl, verify_unloaded=vu)


# ---------------------------------------------------------------------------


def test_initial_state_is_empty() -> None:
    sched = LlmSlotScheduler()
    assert sched.current_resident is None


def test_unknown_model_raises_keyerror() -> None:
    sched = LlmSlotScheduler()
    with pytest.raises(KeyError, match="unknown model_id"):
        sched.acquire("nope")


def test_first_acquire_loads_and_verifies() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)
    handle = sched.acquire("a")
    assert isinstance(handle, SlotHandle)
    assert handle.model_id == "a"
    assert sched.current_resident == "a"
    assert calls.load == ["a"]
    assert calls.verify_loaded == ["a"]
    assert calls.unload == []
    handle.release()


def test_same_model_reacquire_is_noop_no_swap_calls() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)
    with sched.acquire("a"):
        pass
    with sched.acquire("a"):
        pass
    # second acquire should not have called any adapter method again
    assert calls.load == ["a"]
    assert calls.verify_loaded == ["a"]
    assert calls.unload == []
    assert calls.verify_unloaded == []
    assert sched.current_resident == "a"


def test_different_model_swap_calls_in_order() -> None:
    sched = LlmSlotScheduler()
    calls_a = _Calls()
    calls_b = _Calls()
    _register(sched, "a", calls_a)
    _register(sched, "b", calls_b)
    with sched.acquire("a"):
        pass
    with sched.acquire("b"):
        pass
    assert calls_a.unload == ["a"]
    assert calls_a.verify_unloaded == ["a"]
    assert calls_b.load == ["b"]
    assert calls_b.verify_loaded == ["b"]
    # combined ordering: unload(a) -> verify_unloaded(a) -> load(b) -> verify_loaded(b)
    combined = calls_a.order[-2:] + calls_b.order
    assert combined == [
        ("unload", "a"),
        ("verify_unloaded", "a"),
        ("load", "b"),
        ("verify_loaded", "b"),
    ]
    assert sched.current_resident == "b"


def test_handle_exit_does_not_unload() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)
    with sched.acquire("a"):
        pass
    assert sched.current_resident == "a"
    assert calls.unload == []


def test_double_release_is_safe() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)
    handle = sched.acquire("a")
    handle.release()
    handle.release()  # idempotent


def test_explicit_release_method() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)
    sched.acquire("a")  # do not use as ctx mgr
    sched.release()
    # a follow-up acquire would deadlock if release was a no-op.
    h = sched.acquire("a", timeout_s=1.0)
    h.release()


def test_load_failure_leaves_slot_empty_and_unlocked() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls, load_raises=RuntimeError("boom"))
    with pytest.raises(SwapError, match="load"):
        sched.acquire("a")
    assert sched.current_resident is None
    # A second acquire for a different (working) model must succeed -> mutex was freed.
    calls_b = _Calls()
    _register(sched, "b", calls_b)
    with sched.acquire("b"):
        assert sched.current_resident == "b"


def test_unload_failure_during_swap_leaves_slot_empty() -> None:
    sched = LlmSlotScheduler()
    calls_a = _Calls()
    calls_b = _Calls()
    _register(sched, "a", calls_a, unload_raises=RuntimeError("stuck"))
    _register(sched, "b", calls_b)
    with sched.acquire("a"):
        pass
    with pytest.raises(SwapError, match="unload"):
        sched.acquire("b")
    assert sched.current_resident is None


def test_verify_unloaded_false_raises_swap_error() -> None:
    sched = LlmSlotScheduler()
    calls_a = _Calls()
    calls_b = _Calls()
    _register(sched, "a", calls_a, verify_unloaded_returns=False)
    _register(sched, "b", calls_b)
    with sched.acquire("a"):
        pass
    with pytest.raises(SwapError, match="verify_unloaded"):
        sched.acquire("b")
    assert sched.current_resident is None


def test_verify_loaded_false_raises_swap_error() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls, verify_loaded_returns=False)
    with pytest.raises(SwapError, match="verify_loaded"):
        sched.acquire("a")
    assert sched.current_resident is None


def test_two_threads_serialize_via_mutex() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)

    barrier = threading.Barrier(2)
    inside_count = 0
    inside_lock = threading.Lock()
    max_inside = 0
    results: list[str] = []

    def worker(name: str) -> None:
        nonlocal inside_count, max_inside
        barrier.wait()
        with sched.acquire("a", timeout_s=5.0):
            with inside_lock:
                inside_count += 1
                max_inside = max(max_inside, inside_count)
            # Hold the slot briefly so any concurrent acquire would race.
            for _ in range(10000):
                pass
            with inside_lock:
                inside_count -= 1
            results.append(name)

    t1 = threading.Thread(target=worker, args=("t1",))
    t2 = threading.Thread(target=worker, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert max_inside == 1, "mutex must serialize acquires"
    assert sorted(results) == ["t1", "t2"]
    # Second acquire on same model must NOT have triggered another load.
    assert calls.load == ["a"]


def test_acquire_timeout_when_lock_held() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)
    sched.acquire("a")  # hold the lock; no release
    try:
        with pytest.raises(TimeoutError, match="could not acquire"):
            sched.acquire("a", timeout_s=0.05)
    finally:
        sched.release()


def test_default_timeout_used_when_not_specified() -> None:
    sched = LlmSlotScheduler(acquire_timeout_s=0.05)
    calls = _Calls()
    _register(sched, "a", calls)
    sched.acquire("a")
    try:
        with pytest.raises(TimeoutError):
            sched.acquire("b") if False else sched.acquire("a")
    finally:
        sched.release()


def test_negative_timeout_blocks_until_acquired() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)

    holder_done = threading.Event()
    waiter_acquired = threading.Event()

    def holder() -> None:
        h = sched.acquire("a")
        # release after a short delay
        threading.Timer(0.05, lambda: (h.release(), holder_done.set())).start()

    def waiter() -> None:
        with sched.acquire("a", timeout_s=-1):
            waiter_acquired.set()

    holder()
    t = threading.Thread(target=waiter)
    t.start()
    t.join(timeout=5)
    assert waiter_acquired.is_set()
    assert holder_done.wait(timeout=2)


def test_preflight_ram_check_refuses_when_low() -> None:
    ram = _FakeRam(available_mb=100)
    sched = LlmSlotScheduler(ram_monitor=ram, min_free_mb_for_load=5500)
    calls = _Calls()
    _register(sched, "a", calls)
    with pytest.raises(InsufficientRamError, match="available_mb"):
        sched.acquire("a")
    assert sched.current_resident is None
    assert calls.load == []  # never attempted
    # mutex should be released after the failure
    ram.available_mb = 9000
    with sched.acquire("a", timeout_s=1.0):
        assert sched.current_resident == "a"


def test_preflight_ram_check_passes_when_enough() -> None:
    ram = _FakeRam(available_mb=9000)
    sched = LlmSlotScheduler(ram_monitor=ram, min_free_mb_for_load=5500)
    calls = _Calls()
    _register(sched, "a", calls)
    with sched.acquire("a"):
        assert sched.current_resident == "a"


def test_force_unload_clears_state_and_calls_adapter() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)
    with sched.acquire("a"):
        pass
    assert sched.current_resident == "a"
    sched.force_unload()
    assert sched.current_resident is None
    assert calls.unload == ["a"]


def test_force_unload_when_empty_is_noop() -> None:
    sched = LlmSlotScheduler()
    sched.force_unload()  # should not raise
    assert sched.current_resident is None


def test_force_unload_swallows_adapter_errors() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls, unload_raises=RuntimeError("stuck"))
    with sched.acquire("a"):
        pass
    sched.force_unload()  # must not propagate
    assert sched.current_resident is None


def test_force_unload_when_adapter_missing_is_safe() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)
    with sched.acquire("a"):
        pass
    # simulate an adapter being unregistered out from under the scheduler
    sched._adapters.pop("a")
    sched.force_unload()
    assert sched.current_resident is None


def test_kill_switch_is_force_unload() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)
    with sched.acquire("a"):
        pass
    sched.kill_switch()
    assert sched.current_resident is None
    assert calls.unload == ["a"]


def test_concurrent_acquire_release_stress() -> None:
    sched = LlmSlotScheduler()
    calls = _Calls()
    _register(sched, "a", calls)

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(50):
                with sched.acquire("a", timeout_s=5.0):
                    pass
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert errors == []
    # Same model the whole time -> exactly one load.
    assert calls.load == ["a"]
    assert sched.current_resident == "a"
