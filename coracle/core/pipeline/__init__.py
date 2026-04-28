"""Pipeline steps that turn a user message into an execution plan.

This package hosts the deterministic, persistable steps of the Phase 3
pipeline. Only the *consolidate* step lives here today -- ``classify``,
``refine``, and ``plan`` land in their own issues.
"""

from __future__ import annotations

from .bundle import (
    Bundle,
    ClassifyResult,
    FileEntry,
    JobStateSnapshot,
    Message,
    WorkspaceSummary,
)
from .consolidate import (
    DEFAULT_MAX_FILES,
    DEFAULT_RECENT_MESSAGES,
    EXCLUDED_DIRS,
    MAX_FILE_SIZE_BYTES,
    PipelineState,
    consolidate,
)

__all__ = [
    "DEFAULT_MAX_FILES",
    "DEFAULT_RECENT_MESSAGES",
    "EXCLUDED_DIRS",
    "MAX_FILE_SIZE_BYTES",
    "Bundle",
    "ClassifyResult",
    "FileEntry",
    "JobStateSnapshot",
    "Message",
    "PipelineState",
    "WorkspaceSummary",
    "consolidate",
]
