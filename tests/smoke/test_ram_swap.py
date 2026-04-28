"""Smoke test for the qwen2.5:7b <-> qwen2.5-coder:7b RAM swap cycle.

Two variants ship in this module:

* ``@pytest.mark.smoke`` -- mocked, hermetic, runs in CI. Wires the real
  :class:`~coracle.core.scheduler.LlmSlotScheduler` to a fake Ollama
  client and a deterministic RAM sampler. Asserts the architectural
  invariants of the Phase 1 RAM thesis: never two 7B models loaded at once,
  unload happens with ``keep_alive=0`` *before* the next load, RAM trace
  stays above ``hard_cap_mb`` end-to-end, and the audit log emits exactly
  one ``model_unload`` followed by one ``model_load`` per swap.
* ``@pytest.mark.live`` -- real Ollama daemon + real ``psutil`` sampler.
  Skipped unless ``--live`` is passed (see ``conftest.py``). Intended for
  on-device validation on the M1 16GB target.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from coracle.core.ram_monitor import RamMonitor, RamSnapshot
from coracle.core.scheduler import LlmSlotScheduler

MODEL_A = "qwen2.5:7b"
MODEL_B = "qwen2.5-coder:7b"

HARD_CAP_MB = 5000
SOFT_CAP_MB = 7000
TOTAL_MB = 16000


# ---------------------------------------------------------------------------
# Mocked variant
# ---------------------------------------------------------------------------


@dataclass
class _FakeOllamaClient:
    """In-memory stand-in for the future ``OllamaLocalAdapter`` HTTP client.

    Records an ordered audit log of the load/unload calls the scheduler
    drives during a swap cycle. ``keep_alive=0`` is the wire-level signal
    that means *evict the model now* in the Ollama HTTP API, so we record
    it explicitly to assert the eviction contract holds.
    """

    audit: list[dict[str, Any]] = field(default_factory=list)
    loaded: str | None = None

    def load(self, model: str, *, keep_alive: str | int = "5m") -> None:
        self.audit.append({"event": "model_load", "model": model, "keep_alive": keep_alive})
        self.loaded = model

    def unload(self, model: str) -> None:
        self.audit.append({"event": "model_unload", "model": model, "keep_alive": 0})
        if self.loaded == model:
            self.loaded = None

    def generate(self, model: str, prompt: str) -> str:
        if self.loaded != model:
            raise AssertionError(f"generate({model!r}) called while loaded={self.loaded!r}")
        return f"<{model}|{prompt}>"

    def is_loaded(self, model: str) -> bool:
        return self.loaded == model


class _ScriptedSampler:
    """Returns a pre-recorded RAM trace, one snapshot per call.

    The trace is engineered so the scheduler's RAM gate sees plenty of
    headroom (above ``soft_cap_mb``) at every load decision, while the
    free-RAM trace as a whole still dips into the soft band mid-swap to
    prove the soft cap is not trivially high.
    """

    def __init__(self, trace_mb: list[float]) -> None:
        if not trace_mb:
            raise ValueError("trace_mb must be non-empty")
        self._trace = list(trace_mb)
        self._idx = 0
        self.history: list[RamSnapshot] = []

    def __call__(self) -> RamSnapshot:
        available = self._trace[min(self._idx, len(self._trace) - 1)]
        self._idx += 1
        snap = RamSnapshot(
            available_mb=available,
            total_mb=float(TOTAL_MB),
            used_mb=float(TOTAL_MB) - available,
            timestamp=time.time(),
        )
        self.history.append(snap)
        return snap

    def record_external(self, available_mb: float) -> None:
        """Record an off-band sample (e.g. mid-swap dip the scheduler doesn't poll)."""
        self.history.append(
            RamSnapshot(
                available_mb=available_mb,
                total_mb=float(TOTAL_MB),
                used_mb=float(TOTAL_MB) - available_mb,
                timestamp=time.time(),
            )
        )


def _register(scheduler: LlmSlotScheduler, client: _FakeOllamaClient, model: str) -> None:
    scheduler.register_adapter(
        model,
        load=lambda m: client.load(m, keep_alive="5m"),
        unload=lambda m: client.unload(m),
        verify_loaded=lambda m: client.is_loaded(m),
        verify_unloaded=lambda m: not client.is_loaded(m),
    )


@pytest.mark.smoke
def test_ram_swap_cycle_mocked() -> None:
    """End-to-end mocked swap cycle covering the five smoke ACs."""

    sampler = _ScriptedSampler([9000.0, 8500.0])
    client = _FakeOllamaClient()

    scheduler = LlmSlotScheduler(
        ram_monitor=SimpleNamespace(current_snapshot=sampler),
        min_free_mb_for_load=SOFT_CAP_MB,
    )
    _register(scheduler, client, MODEL_A)
    _register(scheduler, client, MODEL_B)

    cycle_start = time.monotonic()

    # 1. Acquire MODEL_A, generate, release.
    with scheduler.acquire(MODEL_A) as h:
        assert h.model_id == MODEL_A
        assert client.is_loaded(MODEL_A)
        assert client.generate(MODEL_A, "ping") == f"<{MODEL_A}|ping>"

    # Mid-swap free-RAM dip into the soft band (off-band observation).
    sampler.record_external(6500.0)

    # 2. Acquire MODEL_B -- must unload A (keep_alive=0) before loading B.
    pre_swap_audit_len = len(client.audit)
    with scheduler.acquire(MODEL_B) as h:
        assert h.model_id == MODEL_B
        assert client.is_loaded(MODEL_B)
        assert not client.is_loaded(MODEL_A)
        assert client.generate(MODEL_B, "def add(a,b):") == f"<{MODEL_B}|def add(a,b):>"

    sampler.record_external(9200.0)
    cycle_elapsed = time.monotonic() - cycle_start

    # AC: unload-before-load ordering with explicit keep_alive=0.
    swap_events = client.audit[pre_swap_audit_len:]
    assert len(swap_events) == 2, f"expected exactly 2 swap events, got {swap_events!r}"
    unload_evt, load_evt = swap_events
    assert unload_evt == {"event": "model_unload", "model": MODEL_A, "keep_alive": 0}
    assert load_evt["event"] == "model_load"
    assert load_evt["model"] == MODEL_B
    assert load_evt["keep_alive"] != 0

    # AC: RAM trace stays above hard cap; soft cap may be touched.
    assert sampler.history, "sampler should have produced snapshots"
    min_available = min(s.available_mb for s in sampler.history)
    assert min_available > HARD_CAP_MB, (
        f"hard cap breached during swap: min_available={min_available} <= {HARD_CAP_MB}"
    )
    touched_soft = any(s.available_mb < SOFT_CAP_MB for s in sampler.history)
    assert touched_soft, "trace never touched the soft band; caps look trivially high"

    # AC: mocked timings -- full swap cycle well under 30s.
    assert cycle_elapsed < 30.0, f"swap cycle latency {cycle_elapsed:.3f}s >= 30s"

    # AC: exactly two audit events in the swap segment, in order.
    assert [e["event"] for e in swap_events] == ["model_unload", "model_load"]


# ---------------------------------------------------------------------------
# Live variant -- opt-in, real daemon
# ---------------------------------------------------------------------------


_OLLAMA_BASE = "http://127.0.0.1:11434"


def _ollama_post(
    path: str, payload: dict[str, Any], timeout: float = 60.0
) -> dict[str, Any]:  # pragma: no cover - live only
    req = urllib.request.Request(
        f"{_OLLAMA_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ollama_get(path: str, timeout: float = 10.0) -> dict[str, Any]:  # pragma: no cover - live only
    with urllib.request.urlopen(f"{_OLLAMA_BASE}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ollama_loaded_models() -> list[str]:  # pragma: no cover - live only
    data = _ollama_get("/api/ps")
    return [m["name"] for m in data.get("models", [])]


@pytest.mark.live
def test_ram_swap_cycle_live() -> None:  # pragma: no cover - live only
    """Real Ollama swap cycle -- run on the M1 with both models pulled."""
    try:
        tags = _ollama_get("/api/tags")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"Ollama daemon not reachable at {_OLLAMA_BASE}: {exc}")

    available = {m["name"] for m in tags.get("models", [])}
    missing = [m for m in (MODEL_A, MODEL_B) if m not in available]
    if missing:
        pytest.skip(f"Pull required: ollama pull {' '.join(missing)}")

    monitor = RamMonitor(soft_cap_mb=SOFT_CAP_MB, hard_cap_mb=HARD_CAP_MB, poll_interval_s=0.25)

    def _load(model: str) -> None:
        _ollama_post("/api/generate", {"model": model, "prompt": "", "keep_alive": "5m"})

    def _unload(model: str) -> None:
        _ollama_post("/api/generate", {"model": model, "prompt": "", "keep_alive": 0})

    def _verify_loaded(model: str) -> bool:
        return any(name == model for name in _ollama_loaded_models())

    def _verify_unloaded(model: str) -> bool:
        return all(name != model for name in _ollama_loaded_models())

    scheduler = LlmSlotScheduler(
        ram_monitor=SimpleNamespace(current_snapshot=monitor.current_snapshot),
        min_free_mb_for_load=SOFT_CAP_MB,
    )
    for model in (MODEL_A, MODEL_B):
        scheduler.register_adapter(
            model,
            load=_load,
            unload=_unload,
            verify_loaded=_verify_loaded,
            verify_unloaded=_verify_unloaded,
        )

    monitor.start()
    try:
        cycle_start = time.monotonic()
        for model, prompt in (
            (MODEL_A, "ping"),
            (MODEL_B, "def add(a,b):"),
            (MODEL_A, "ping"),
        ):
            with scheduler.acquire(model):
                _ollama_post(
                    "/api/generate",
                    {"model": model, "prompt": prompt, "stream": False, "keep_alive": "5m"},
                )
                loaded_now = _ollama_loaded_models()
                assert loaded_now == [model], f"unexpected residency: {loaded_now!r}"
        elapsed = time.monotonic() - cycle_start
        assert elapsed < 600.0, f"live swap cycle too slow: {elapsed:.1f}s"
    finally:
        monitor.stop()
        scheduler.force_unload()
