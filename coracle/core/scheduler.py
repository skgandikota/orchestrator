"""Single-LLM-slot scheduler.

Architectural rule #1 from ``CONTRIBUTING.md``: never load two 7B models
simultaneously. This module is the gate that makes that rule true. Exactly
one model holds the slot at any given time. Switching models is a strict
``unload current -> verify gone -> load next -> verify loaded`` dance.

The scheduler itself is dumb about *what* a model is. It just calls four
registered callables per model id (the adapter). The Ollama adapter (#35)
is what actually makes Ollama load and unload happen. This separation keeps
tests pure: fakes implement the four callbacks with counters and assertions.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

__all__ = [
    "InsufficientRamError",
    "LlmSlotScheduler",
    "RamMonitorProtocol",
    "SlotHandle",
    "SwapError",
]


class SwapError(RuntimeError):
    """Raised when a model swap fails at any verification step."""


class InsufficientRamError(RuntimeError):
    """Raised when the pre-flight RAM check refuses to load a new model."""


class _RamSnapshot(Protocol):
    available_mb: float


class RamMonitorProtocol(Protocol):
    """Minimal surface the scheduler needs from a RAM monitor (#33)."""

    def current_snapshot(self) -> _RamSnapshot: ...


@dataclass(frozen=True)
class _Adapter:
    load: Callable[[str], None]
    unload: Callable[[str], None]
    verify_loaded: Callable[[str], bool]
    verify_unloaded: Callable[[str], bool]


class SlotHandle:
    """Context-managed handle to the single LLM slot.

    ``__exit__`` releases the mutex but **does not** unload the model.
    Residency persists across handles to avoid load thrash when consecutive
    steps want the same model.
    """

    def __init__(self, scheduler: LlmSlotScheduler, model_id: str) -> None:
        self._scheduler = scheduler
        self.model_id = model_id
        self._released = False

    def __enter__(self) -> SlotHandle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._scheduler._release_locked_by_handle()


class LlmSlotScheduler:
    """Single-mutex scheduler that owns the only LLM slot in the process."""

    def __init__(
        self,
        *,
        ram_monitor: RamMonitorProtocol | None = None,
        acquire_timeout_s: float = 120.0,
        min_free_mb_for_load: int = 5500,
    ) -> None:
        self._lock = threading.Lock()
        self._adapters: dict[str, _Adapter] = {}
        self._current_resident: str | None = None
        self._ram_monitor = ram_monitor
        self._default_timeout_s = acquire_timeout_s
        self._min_free_mb_for_load = min_free_mb_for_load

    @property
    def current_resident(self) -> str | None:
        """The id of the model currently holding the slot, or ``None``."""
        return self._current_resident

    def register_adapter(
        self,
        model_id: str,
        *,
        load: Callable[[str], None],
        unload: Callable[[str], None],
        verify_loaded: Callable[[str], bool],
        verify_unloaded: Callable[[str], bool],
    ) -> None:
        """Register the four callables that drive a given model id."""
        self._adapters[model_id] = _Adapter(
            load=load,
            unload=unload,
            verify_loaded=verify_loaded,
            verify_unloaded=verify_unloaded,
        )

    def acquire(self, model_id: str, *, timeout_s: float | None = None) -> SlotHandle:
        """Block until the slot is free, then no-op or perform a swap.

        Raises:
            KeyError: ``model_id`` was never registered.
            TimeoutError: The mutex could not be acquired within ``timeout_s``.
            InsufficientRamError: Pre-flight RAM check refused the load.
            SwapError: Unload/load/verify failed; slot is left empty.
        """
        if model_id not in self._adapters:
            raise KeyError(f"unknown model_id: {model_id!r}")

        wait = self._default_timeout_s if timeout_s is None else timeout_s
        if wait is None or wait < 0:
            acquired = self._lock.acquire()
        else:
            acquired = self._lock.acquire(timeout=wait)
        if not acquired:
            raise TimeoutError(f"could not acquire LLM slot for {model_id!r} within {wait}s")

        try:
            if self._current_resident == model_id:
                return SlotHandle(self, model_id)
            self._perform_swap(model_id)
            return SlotHandle(self, model_id)
        except BaseException:
            self._current_resident = None
            self._lock.release()
            raise

    def _perform_swap(self, model_id: str) -> None:
        if self._current_resident is not None:
            current = self._current_resident
            current_adapter = self._adapters[current]
            try:
                current_adapter.unload(current)
            except Exception as exc:
                raise SwapError(f"unload({current!r}) raised: {exc!r}") from exc
            if not current_adapter.verify_unloaded(current):
                raise SwapError(f"verify_unloaded({current!r}) returned False")
            self._current_resident = None

        if self._ram_monitor is not None:
            snapshot = self._ram_monitor.current_snapshot()
            available = float(snapshot.available_mb)
            if available < self._min_free_mb_for_load:
                raise InsufficientRamError(
                    f"available_mb={available:.0f} < "
                    f"min_free_mb_for_load={self._min_free_mb_for_load}"
                )

        new_adapter = self._adapters[model_id]
        try:
            new_adapter.load(model_id)
        except Exception as exc:
            raise SwapError(f"load({model_id!r}) raised: {exc!r}") from exc
        if not new_adapter.verify_loaded(model_id):
            raise SwapError(f"verify_loaded({model_id!r}) returned False")
        self._current_resident = model_id

    def _release_locked_by_handle(self) -> None:
        # Defensive: only release if currently held. Handles a buggy double
        # release without crashing the caller.
        if self._lock.locked():
            with contextlib.suppress(RuntimeError):  # pragma: no cover - defensive
                self._lock.release()

    def release(self) -> None:
        """Explicit release of the mutex without dropping residency."""
        self._release_locked_by_handle()

    def force_unload(self) -> None:
        """Drop the resident model immediately and clear state.

        Intended for the RAM monitor kill-switch path: it does **not** wait
        on the mutex, because the holder may itself be the thread the kill
        switch is trying to interrupt. Adapter ``unload`` errors are
        swallowed -- a kill switch must always succeed in clearing state.
        """
        current = self._current_resident
        self._current_resident = None
        if current is None:
            return
        adapter = self._adapters.get(current)
        if adapter is None:
            return
        # Kill-switch path: state is already cleared; we cannot block on a
        # misbehaving adapter.
        with contextlib.suppress(Exception):
            adapter.unload(current)

    def kill_switch(self) -> None:
        """Bound callable to register with the RAM monitor."""
        self.force_unload()
