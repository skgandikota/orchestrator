"""The pipeline ``consolidate`` step.

Gathers the user message, recent conversation history, a coarse workspace
summary, and the latest durable job-state snapshot into a single
:class:`Bundle`. The bundle is checkpointed to the durable state store via
``append_pipeline_event`` *before* it is returned, so a crash mid-pipeline
is recoverable.

This step performs no LLM calls and reads no file contents -- it is a pure
context-gathering step.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

import pathspec

from ..workspace import WorkspaceLike
from .bundle import (
    Bundle,
    ClassifyResult,
    FileEntry,
    JobStateSnapshot,
    Message,
    WorkspaceSummary,
)

__all__ = [
    "DEFAULT_MAX_FILES",
    "DEFAULT_RECENT_MESSAGES",
    "EXCLUDED_DIRS",
    "MAX_FILE_SIZE_BYTES",
    "PipelineState",
    "consolidate",
]

#: Directories that are never useful to the pipeline summary.
EXCLUDED_DIRS: frozenset[str] = frozenset({".git", "node_modules", "__pycache__", ".venv"})

#: Maximum size of a single file before it's excluded from the summary.
MAX_FILE_SIZE_BYTES: int = 1 * 1024 * 1024  # 1 MiB

#: Default cap on the number of files in :class:`WorkspaceSummary`.
DEFAULT_MAX_FILES: int = 500

#: Default number of recent messages to include in the bundle.
DEFAULT_RECENT_MESSAGES: int = 20


class PipelineState(Protocol):
    """Subset of the durable state store used by the consolidate step."""

    def recent_messages(self, job_id: str, limit: int) -> Iterable[Message]: ...

    def get_job_state(self, job_id: str) -> JobStateSnapshot: ...

    def append_pipeline_event(
        self, job_id: str, *, step: str, payload: dict[str, object]
    ) -> None: ...


def _path_excluded_by_dirs(rel_path: str) -> bool:
    parts = rel_path.split("/")
    return any(part in EXCLUDED_DIRS for part in parts)


def _build_gitignore_matcher(text: str | None) -> pathspec.PathSpec | None:
    if not text:
        return None
    return pathspec.PathSpec.from_lines("gitwildmatch", text.splitlines())


def _summarise_workspace(
    workspace: WorkspaceLike,
    *,
    max_files: int,
    max_file_size_bytes: int,
) -> WorkspaceSummary:
    matcher = _build_gitignore_matcher(workspace.read_gitignore())
    entries: list[FileEntry] = []
    truncated = False

    for stat in workspace.walk_files():
        if _path_excluded_by_dirs(stat.path):
            continue
        if stat.size_bytes > max_file_size_bytes:
            continue
        if matcher is not None and matcher.match_file(stat.path):
            continue
        if len(entries) >= max_files:
            truncated = True
            break
        entries.append(FileEntry(path=stat.path, size_bytes=stat.size_bytes))

    entries.sort(key=lambda e: e.path)
    return WorkspaceSummary(root=workspace.root, files=entries, truncated=truncated)


def _truncate_history(
    messages: Iterable[Message],
    *,
    limit: int,
    token_budget: int | None,
) -> list[Message]:
    items = list(messages)
    if limit >= 0:
        items = items[-limit:] if limit else []
    if token_budget is None or token_budget < 0:
        return items
    # Rough heuristic: 1 token ~= 4 chars. Drop oldest until we fit.
    budget_chars = token_budget * 4
    total = sum(len(m.content) for m in items)
    while items and total > budget_chars:
        dropped = items.pop(0)
        total -= len(dropped.content)
    return items


def consolidate(
    job_id: str,
    user_msg: str,
    classification: ClassifyResult,
    *,
    state: PipelineState,
    workspace: WorkspaceLike,
    recent_messages_limit: int = DEFAULT_RECENT_MESSAGES,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_size_bytes: int = MAX_FILE_SIZE_BYTES,
    history_token_budget: int | None = None,
) -> Bundle:
    """Build a :class:`Bundle` and checkpoint it before returning.

    Args:
        job_id: ID of the job this pipeline run belongs to.
        user_msg: The latest user message that triggered the pipeline.
        classification: Output of the upstream ``classify`` step.
        state: Durable state-store handle (see :class:`PipelineState`).
        workspace: Workspace abstraction used to summarise the project tree.
        recent_messages_limit: Max number of recent messages to carry forward.
            Defaults to :data:`DEFAULT_RECENT_MESSAGES`.
        max_files: Cap on entries in the workspace summary.
        max_file_size_bytes: Files larger than this are excluded.
        history_token_budget: Optional rough token budget for the recent
            messages. When set, oldest messages are dropped first until the
            estimated total fits within ``budget * 4`` characters.

    Raises:
        ValueError: If ``job_id`` or ``user_msg`` is empty.
    """

    if not job_id:
        raise ValueError("job_id must not be empty")
    if not user_msg:
        raise ValueError("user_msg must not be empty")

    history = _truncate_history(
        state.recent_messages(job_id, recent_messages_limit),
        limit=recent_messages_limit,
        token_budget=history_token_budget,
    )
    job_state = state.get_job_state(job_id)
    summary = _summarise_workspace(
        workspace,
        max_files=max_files,
        max_file_size_bytes=max_file_size_bytes,
    )

    bundle = Bundle(
        job_id=job_id,
        user_msg=user_msg,
        recent_messages=history,
        workspace_summary=summary,
        recent_job_state=job_state,
        classification=classification,
    )

    state.append_pipeline_event(
        job_id,
        step="consolidate",
        payload=bundle.model_dump(mode="json"),
    )
    return bundle
