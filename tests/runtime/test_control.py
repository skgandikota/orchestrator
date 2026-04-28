"""Tests for ``coracle.runtime.control``."""

from __future__ import annotations

import asyncio

import pytest

from coracle.runtime.control import (
    CancelledControl,
    ControlFlag,
    ControlState,
    InvalidControlTransition,
)


def test_initial_state_is_running() -> None:
    flag = ControlFlag()
    assert flag.state is ControlState.RUNNING
    assert not flag.is_paused
    assert not flag.is_cancelled


def test_request_pause_marks_pause_requested() -> None:
    flag = ControlFlag()
    flag.request_pause()
    assert flag.state is ControlState.PAUSE_REQUESTED


def test_request_pause_is_idempotent() -> None:
    flag = ControlFlag()
    flag.request_pause()
    flag.request_pause()
    assert flag.state is ControlState.PAUSE_REQUESTED


def test_request_resume_on_running_is_noop() -> None:
    flag = ControlFlag()
    flag.request_resume()
    assert flag.state is ControlState.RUNNING


def test_request_cancel_marks_cancel_requested() -> None:
    flag = ControlFlag()
    flag.request_cancel()
    assert flag.state is ControlState.CANCEL_REQUESTED


def test_request_cancel_is_idempotent() -> None:
    flag = ControlFlag()
    flag.request_cancel()
    flag.request_cancel()
    assert flag.state is ControlState.CANCEL_REQUESTED


def test_raise_if_cancelled_transitions_to_cancelled() -> None:
    flag = ControlFlag()
    flag.request_cancel()
    with pytest.raises(CancelledControl):
        flag.raise_if_cancelled()
    assert flag.state is ControlState.CANCELLED
    assert flag.is_cancelled


def test_raise_if_cancelled_is_idempotent_after_cancel() -> None:
    flag = ControlFlag()
    flag.request_cancel()
    with pytest.raises(CancelledControl):
        flag.raise_if_cancelled()
    # Still terminal, still raises.
    with pytest.raises(CancelledControl):
        flag.raise_if_cancelled()


def test_raise_if_cancelled_is_noop_when_running() -> None:
    flag = ControlFlag()
    flag.raise_if_cancelled()
    assert flag.state is ControlState.RUNNING


def test_pause_after_cancel_is_invalid() -> None:
    flag = ControlFlag()
    flag.request_cancel()
    with pytest.raises(CancelledControl):
        flag.raise_if_cancelled()
    with pytest.raises(InvalidControlTransition):
        flag.request_pause()


def test_resume_of_cancel_requested_is_invalid() -> None:
    flag = ControlFlag()
    flag.request_cancel()
    with pytest.raises(InvalidControlTransition):
        flag.request_resume()


def test_resume_of_cancelled_is_invalid() -> None:
    flag = ControlFlag()
    flag.request_cancel()
    with pytest.raises(CancelledControl):
        flag.raise_if_cancelled()
    with pytest.raises(InvalidControlTransition):
        flag.request_resume()


def test_pause_during_cancel_requested_is_noop() -> None:
    flag = ControlFlag()
    flag.request_cancel()
    flag.request_pause()
    assert flag.state is ControlState.CANCEL_REQUESTED


@pytest.mark.asyncio
async def test_wait_if_paused_returns_immediately_when_running() -> None:
    flag = ControlFlag()
    await asyncio.wait_for(flag.wait_if_paused(), timeout=1.0)
    assert flag.state is ControlState.RUNNING


@pytest.mark.asyncio
async def test_wait_if_paused_acknowledges_and_blocks_until_resume() -> None:
    flag = ControlFlag()
    flag.request_pause()
    assert flag.state is ControlState.PAUSE_REQUESTED

    waiter = asyncio.create_task(flag.wait_if_paused())
    # Let the waiter run to the point where it acknowledges pause.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert flag.state is ControlState.PAUSED
    assert flag.is_paused
    assert not waiter.done()

    flag.request_resume()
    await asyncio.wait_for(waiter, timeout=1.0)
    assert flag.state is ControlState.RUNNING


@pytest.mark.asyncio
async def test_cancel_unblocks_a_paused_waiter() -> None:
    flag = ControlFlag()
    flag.request_pause()
    waiter = asyncio.create_task(flag.wait_if_paused())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert flag.is_paused

    flag.request_cancel()
    await asyncio.wait_for(waiter, timeout=1.0)
    # State after the wait returns is still PAUSED until the next
    # checkpoint runs raise_if_cancelled. Cancel was requested though.
    with pytest.raises(CancelledControl):
        flag.raise_if_cancelled()
    assert flag.state is ControlState.CANCELLED


@pytest.mark.asyncio
async def test_checkpoint_runs_pause_then_cancel() -> None:
    flag = ControlFlag()
    # No-op when running.
    await flag.checkpoint()

    # Pause + resume round trip via checkpoint.
    flag.request_pause()
    waiter = asyncio.create_task(flag.checkpoint())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert flag.is_paused
    flag.request_resume()
    await asyncio.wait_for(waiter, timeout=1.0)

    # Cancel via checkpoint.
    flag.request_cancel()
    with pytest.raises(CancelledControl):
        await flag.checkpoint()
    assert flag.is_cancelled


@pytest.mark.asyncio
async def test_cooperative_loop_round_trip() -> None:
    """End-to-end: a fake pipeline loop honours pause then cancel."""
    flag = ControlFlag()
    iterations = 0
    paused_seen = False

    async def loop() -> None:
        nonlocal iterations, paused_seen
        for _ in range(100):
            await flag.checkpoint()
            iterations += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(loop())
    await asyncio.sleep(0)
    flag.request_pause()
    # Drain enough scheduler ticks for the loop to acknowledge pause.
    for _ in range(10):
        await asyncio.sleep(0)
        if flag.is_paused:
            paused_seen = True
            break
    assert paused_seen
    iters_at_pause = iterations
    # While paused, no further iterations should run.
    for _ in range(5):
        await asyncio.sleep(0)
    assert iterations == iters_at_pause

    flag.request_resume()
    await asyncio.sleep(0)
    flag.request_cancel()
    with pytest.raises(CancelledControl):
        await task
    assert iterations > iters_at_pause


def test_resume_from_paused_returns_to_running() -> None:
    flag = ControlFlag()
    flag.request_pause()
    # Manually drive into PAUSED via the public state machine.
    flag._state = ControlState.PAUSED  # type: ignore[attr-defined]
    flag.request_resume()
    assert flag.state is ControlState.RUNNING
