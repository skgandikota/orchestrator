"""OpenAI-compatible interface helpers.

This module is the integration point between the OpenAI-shaped HTTP surface
and the in-process model profile registry in :mod:`model_profiles`. It is
deliberately framework-free: the functions here return plain ``dict``s and
raise :class:`ProfileNotFoundError`, leaving HTTP serialization and status
code mapping to whichever framework eventually wires this up.

Two responsibilities live here:

* :func:`list_models_response` -- builds the OpenAI ``/v1/models`` list
  payload, with the default ``orchestrator`` entry first.
* :func:`submit_job` -- the pre-job-submission resolver. It looks up the
  requested profile, decides whether the classifier should run, and records
  the resolved ``force_class`` on the persisted ``Job`` row. The actual
  scheduler call is injected so this stays trivially testable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .model_profiles import (
    DEFAULT_PROFILE_NAME,
    ForceClass,
    ModelProfile,
    ProfileNotFoundError,
    list_profiles,
    resolve_profile,
)

__all__ = [
    "JobRow",
    "JobSubmitter",
    "ProfileNotFoundError",
    "list_models_response",
    "submit_job",
]

OPENAI_MODEL_OBJECT = "model"
OPENAI_LIST_OBJECT = "list"
OPENAI_OWNED_BY = "orchestrator"


@dataclass(frozen=True)
class JobRow:
    """The slice of the persisted ``Job`` record this module touches.

    The full ``Job`` schema lives in the state-store layer; this projection
    captures only the fields the profile resolver writes.
    """

    job_id: str
    model: str
    force_class: ForceClass | None


# A submitter is "give me a class to run, I'll persist a job and return its
# id". Decoupling it like this keeps this module free of any database or
# scheduler dependency.
JobSubmitter = Callable[[str, ForceClass | None], str]


def _model_to_openai(profile: ModelProfile, *, created: int) -> dict[str, object]:
    return {
        "id": profile.name,
        "object": OPENAI_MODEL_OBJECT,
        "created": created,
        "owned_by": OPENAI_OWNED_BY,
        "description": profile.description,
        "force_class": profile.force_class,
    }


def list_models_response(*, now: Callable[[], float] = time.time) -> dict[str, object]:
    """Return the OpenAI-shaped ``/v1/models`` response payload.

    The default ``orchestrator`` profile is always first in ``data``.
    """

    created = int(now())
    profiles = list_profiles()
    # Defensive: enforce default-first ordering even if the registry is
    # reshuffled later. Stable order otherwise.
    profiles.sort(key=lambda p: 0 if p.name == DEFAULT_PROFILE_NAME else 1)
    return {
        "object": OPENAI_LIST_OBJECT,
        "data": [_model_to_openai(p, created=created) for p in profiles],
    }


def submit_job(
    model_name: str,
    *,
    submitter: JobSubmitter,
    classifier: Callable[[], ForceClass] | None = None,
) -> JobRow:
    """Resolve ``model_name`` and submit a job through ``submitter``.

    Behaviour:

    * If the resolved profile carries a ``force_class``, the classifier is
      **not** invoked and that class is used directly.
    * If ``force_class`` is ``None`` (the default ``orchestrator`` profile),
      ``classifier`` is invoked to choose a class. ``classifier`` must be
      provided in that case.
    * Architectural rule: ``submitter`` is expected to enqueue and return
      the ``job_id`` immediately; this function never blocks waiting for
      the scheduler.

    Raises:
        ProfileNotFoundError: ``model_name`` is not a registered profile.
        ValueError: the resolved profile needs the classifier but none was
            supplied.
    """

    profile = resolve_profile(model_name)

    if profile.force_class is not None:
        chosen: ForceClass = profile.force_class
    else:
        if classifier is None:
            raise ValueError("classifier is required when the resolved profile has no force_class")
        chosen = classifier()

    job_id = submitter(profile.name, chosen)
    # The Job row records the *resolved* class (whether forced or
    # classifier-picked) plus the originally requested model name so
    # operators can audit which profile a given job came from.
    return JobRow(job_id=job_id, model=profile.name, force_class=chosen)
