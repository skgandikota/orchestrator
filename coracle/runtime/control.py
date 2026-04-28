"""Cooperative pause/resume/cancel control for pipeline runs.

This module is intentionally tiny and dependency-free. It exposes
:class:`ControlFlag`, an asyncio-aware state machine that long-running
pipeline steps can poll between iterations to honour external
pause/resume/cancel requests without ever interrupting a step
mid-flight (which is unsafe at our LLM swap boundary).

Integration contract (for ``p6-status-a-coder`` and the scheduler):

* The scheduler / job manager owns one :class:`ControlFlag` per
  in-flight job. It does **not** need to wrap or refactor any existing
  state to use it -- the flag is purely additive.
* Pipeline steps are required to call
  ``await flag.checkpoint()`` (or the more granular
  ``await flag.wait_if_paused()`` plus ``flag.raise_if_cancelled()``)
  between iterations / steps. These are the only safe yield points.
* HTTP endpoints (``/jobs/{id}/pause``, ``/jobs/{id}/resume``,
  ``/jobs/{id}/cancel``) translate directly to
  :meth:`ControlFlag.request_pause`, :meth:`ControlFlag.request_resume`
  and :meth:`ControlFlag.request_cancel`. Endpoints never block.
* Invalid transitions (e.g. resuming a cancelled job) raise
  :class:`InvalidControlTransition`, which the HTTP layer should map
  to a 409.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

__all__ = [
    "CancelledControl",
    "ControlFlag",
    "ControlState",
    "InvalidControlTransition",
]


class ControlState(StrEnum):
    """Public lifecycle states for a :class:`ControlFlag`."""

    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


_TERMINAL: frozenset[ControlState] = frozenset({ControlState.CANCELLED})


class CancelledControl(Exception):
    """Raised by checkpoint helpers when a cancel has been requested.

    Pipeline code is expected to let this propagate so the runner can
    perform cleanup, mark the job ``cancelled`` and release the slot.
    """


class InvalidControlTransition(Exception):
    """Raised when an illegal state transition is requested.

    The HTTP layer maps this to a 409 Conflict response.
    """


class ControlFlag:
    """Asyncio-aware pause/resume/cancel flag for one job.

    The flag is a small state machine. External callers (HTTP handlers)
    *request* transitions; the pipeline *acknowledges* them at
    checkpoints. This split is what makes control cooperative -- a
    pause never tears down a step mid-flight, and a cancel always runs
    the step's cleanup hook.
    """

    def __init__(self) -> None:
        self._state: ControlState = ControlState.RUNNING
        self._resume_event = asyncio.Event()
        self._resume_event.set()

    @property
    def state(self) -> ControlState:
        """The current public state."""
        return self._state

    @property
    def is_paused(self) -> bool:
        """True iff the flag is in ``PAUSED`` (acknowledged) state."""
        return self._state is ControlState.PAUSED

    @property
    def is_cancelled(self) -> bool:
        """True iff the flag is in terminal ``CANCELLED`` state."""
        return self._state is ControlState.CANCELLED

    def request_pause(self) -> None:
        """Ask the running pipeline to pause at its next checkpoint.

        No-op if a pause/cancel has already been requested or
        acknowledged. Raises :class:`InvalidControlTransition` only
        when the job is already in a terminal state.
        """
        if self._state in _TERMINAL:
            raise InvalidControlTransition(f"cannot pause: job is {self._state.value}")
        if self._state in {
            ControlState.PAUSE_REQUESTED,
            ControlState.PAUSED,
            ControlState.CANCEL_REQUESTED,
        }:
            return
        self._state = ControlState.PAUSE_REQUESTED
        self._resume_event.clear()

    def request_resume(self) -> None:
        """Ask a paused (or pause-requested) pipeline to keep running.

        Resuming a job that is already running is a no-op. Resuming a
        cancelled or cancel-requested job raises
        :class:`InvalidControlTransition`.
        """
        if self._state in {ControlState.CANCEL_REQUESTED, ControlState.CANCELLED}:
            raise InvalidControlTransition(f"cannot resume: job is {self._state.value}")
        if self._state is ControlState.RUNNING:
            return
        self._state = ControlState.RUNNING
        self._resume_event.set()

    def request_cancel(self) -> None:
        """Ask the running pipeline to cancel at its next checkpoint.

        No-op if a cancel has already been requested or acknowledged.
        Cancel takes precedence over pause: a paused job can be
        cancelled and the resume event is unblocked so the loop wakes
        and observes the cancellation.
        """
        if self._state is ControlState.CANCELLED:
            return
        if self._state is ControlState.CANCEL_REQUESTED:
            return
        self._state = ControlState.CANCEL_REQUESTED
        # Wake any task currently parked in wait_if_paused so it can
        # observe the cancellation and exit cleanly.
        self._resume_event.set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`CancelledControl` if a cancel was requested.

        Acknowledges the request by transitioning to ``CANCELLED``.
        Idempotent: subsequent calls keep raising while the flag stays
        terminal.
        """
        if self._state in {ControlState.CANCEL_REQUESTED, ControlState.CANCELLED}:
            self._state = ControlState.CANCELLED
            raise CancelledControl("control flag cancelled")

    async def wait_if_paused(self) -> None:
        """Block until ``RUNNING`` again. Acknowledges pause requests.

        On entry, if a pause was requested, the flag transitions to
        ``PAUSED`` to signal the runner that the slot can be released.
        The coroutine then awaits a resume (or a cancel, which unblocks
        the wait so the caller can observe the cancel via
        :meth:`raise_if_cancelled`).
        """
        if self._state is ControlState.PAUSE_REQUESTED:
            self._state = ControlState.PAUSED
        if self._state is ControlState.PAUSED:
            await self._resume_event.wait()

    async def checkpoint(self) -> None:
        """Convenience: yield to pause, then check for cancel.

        This is the single call most pipeline steps want between
        iterations.
        """
        await self.wait_if_paused()
        self.raise_if_cancelled()
