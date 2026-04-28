"""Pydantic models for the consolidate step.

These models are kept flat and JSON-serialisable so the resulting
:class:`Bundle` can be checkpointed straight into SQLite via
``model_dump_json()`` and round-trip back.

Other pipeline steps (``refine``, ``plan``) import their input shape from
this module so they don't have to take a transitive dependency on
``consolidate.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Bundle",
    "ClassifyResult",
    "FileEntry",
    "JobStateSnapshot",
    "Message",
    "WorkspaceSummary",
]


MessageRole = Literal["system", "user", "assistant", "tool"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Message(_Frozen):
    """A single chat-style message in the conversation history."""

    role: MessageRole
    content: str
    created_at: datetime | None = None


class FileEntry(_Frozen):
    """One entry in the workspace summary -- path + size only, no contents."""

    path: str
    size_bytes: int = Field(ge=0)


class WorkspaceSummary(_Frozen):
    """A coarse, content-free description of the workspace directory."""

    root: str
    files: list[FileEntry] = Field(default_factory=list)
    truncated: bool = False


class JobStateSnapshot(_Frozen):
    """A point-in-time projection of the durable job record."""

    job_id: str
    status: str
    attempt: int = Field(default=0, ge=0)
    last_step: str | None = None
    updated_at: datetime | None = None


class ClassifyResult(_Frozen):
    """Output of the ``classify`` step (#37) -- redefined here as the contract.

    The full implementation lives in the classify module; we only require the
    fields ``consolidate`` actually carries forward.
    """

    intent: str
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Bundle(BaseModel):
    """The deterministic, serialisable artefact produced by ``consolidate``."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    user_msg: str
    recent_messages: list[Message] = Field(default_factory=list)
    workspace_summary: WorkspaceSummary
    recent_job_state: JobStateSnapshot
    classification: ClassifyResult
