"""Operator-facing helpers for recoverable jobs.

Surfaces jobs that the startup recovery pass (see
:mod:`orchestrator.core.recovery`) flipped to ``recoverable`` so the user can
choose to ``resume`` or ``cancel`` each one.
"""

from __future__ import annotations

from collections.abc import Iterable

from orchestrator.core.recovery import Job, StateStore, cancel, resume

__all__ = ["cancel_job", "list_recoverable", "resume_job"]


def list_recoverable(state: StateStore) -> list[Job]:
    """Return every job currently in the ``recoverable`` state."""

    jobs: Iterable[Job] = state.list_recoverable()
    return list(jobs)


def resume_job(state: StateStore, job_id: str) -> None:
    resume(state, job_id)


def cancel_job(state: StateStore, job_id: str) -> None:
    cancel(state, job_id)
