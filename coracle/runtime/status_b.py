"""Status mode B: snapshot + optional narrator gloss (issue #14).

Mode B is a thin layer on top of mode A. It always reads the structured
:class:`~coracle.runtime.status.Snapshot` first (no LLM required),
then — *only when ``[status] narrator_enabled = true``* — passes that
payload through a small qwen2.5:1.5b narrator for a 1-2 sentence
natural-language gloss.

Failure modes are intentionally soft:

* Narrator disabled or absent → mode A payload + ``narrator_disabled``.
* Narrator raises → mode A payload + ``narrator_error`` (HTTP 200, not
  500). This preserves the architectural rule that status queries must
  always succeed without depending on an LLM.

This module is kept separate from :mod:`coracle.runtime.status` so
mode C (issue #16, p6-status-c) can land in parallel without merge
conflicts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from coracle.runtime.status import RamReading, Snapshot, snapshot

__all__ = ["status_b"]


def status_b(
    job: Any,
    *,
    narrator: Any | None = None,
    ram_sampler: Callable[[], RamReading] | None = None,
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Build a status mode B payload for ``job``.

    Always computes a mode A :class:`Snapshot` first. If ``narrator`` is
    provided and reports :attr:`enabled`, its narration is appended as
    ``narration``; otherwise the payload carries ``narrator_disabled``.
    Any exception raised by the narrator is captured in
    ``narrator_error`` and the function still returns successfully.

    Args:
        job: The job-like object accepted by
            :func:`coracle.runtime.status.snapshot`.
        narrator: Optional :class:`~coracle.models.narrator.Narrator`
            (or any object exposing ``enabled`` and ``narrate``).
        ram_sampler: Forwarded to :func:`snapshot` for tests.
        now: Forwarded to :func:`snapshot` for tests.
    """
    snap: Snapshot = snapshot(job, ram_sampler=ram_sampler, now=now)
    payload: dict[str, Any] = snap.to_dict()
    payload["mode"] = "b"

    if narrator is None or not getattr(narrator, "enabled", False):
        payload["narrator_disabled"] = True
        return payload

    try:
        payload["narration"] = narrator.narrate(snap)
    except Exception as exc:
        payload["narrator_error"] = repr(exc)
    return payload
