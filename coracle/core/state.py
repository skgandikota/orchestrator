"""SQLite-backed durable job state store.

This module is the persistence layer for the coracle. It exposes a
:class:`StateStore` that wraps a :class:`sqlite3.Connection` configured with
WAL journaling, ``foreign_keys=ON``, and ``row_factory=sqlite3.Row``.

The schema is created and evolved by :meth:`StateStore.migrate`, which runs
every ``*.sql`` file in :mod:`coracle.core.migrations` in lexical order
exactly once -- applied versions are tracked in a ``schema_migrations`` table,
so calling ``migrate`` repeatedly is a no-op.

The CRUD surface is intentionally small and additive (see Phase 1 plan):
jobs, steps, messages, and artifacts. All reads return typed ``pydantic``
models so that raw :class:`sqlite3.Row` objects never leak past the boundary,
and all writes use parameterized queries.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "Artifact",
    "Job",
    "JobStatus",
    "Message",
    "StateStore",
    "Step",
    "StepStatus",
]


class JobStatus(StrEnum):
    """Lifecycle states for a job."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    """Lifecycle states for an individual step within a job."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class Job(BaseModel):
    """Typed read-model for a row in ``jobs``."""

    id: str
    status: JobStatus
    kind: str
    created_at: str
    updated_at: str
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None


class Step(BaseModel):
    """Typed read-model for a row in ``steps``."""

    id: int
    job_id: str
    idx: int
    name: str
    status: StepStatus
    started_at: str | None = None
    finished_at: str | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: str | None = None


class Message(BaseModel):
    """Typed read-model for a row in ``messages``."""

    id: int
    job_id: str
    role: str
    content: str
    created_at: str


class Artifact(BaseModel):
    """Typed read-model for a row in ``artifacts``."""

    id: int
    job_id: str
    kind: str
    path: str | None = None
    content: str | None = None
    created_at: str


_MIGRATIONS_PACKAGE = "coracle.core.migrations"


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return json.loads(value)


class StateStore:
    """SQLite-backed durable state store.

    The connection is opened in the constructor with
    ``check_same_thread=False`` so that later phases can use the store from a
    thread pool. Mutations are wrapped in short transactions via the
    ``with self._conn:`` context manager, which commits on success and rolls
    back on exceptions.
    """

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = path
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level="DEFERRED",
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        # PRAGMAs must be issued outside an explicit transaction.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = NORMAL")

    # ------------------------------------------------------------------ infra

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the underlying connection (escape hatch for advanced use)."""
        return self._conn

    @property
    def db_path(self) -> Path:
        """Return the on-disk path backing this store."""
        return self._db_path

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- migrations

    def migrate(self) -> list[str]:
        """Apply any un-applied migrations; idempotent.

        Returns the list of migration versions newly applied during this call
        (empty if the database is already up to date).
        """
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  version    TEXT PRIMARY KEY,"
                "  applied_at TEXT NOT NULL"
                ")"
            )

        applied: set[str] = {
            row["version"] for row in self._conn.execute("SELECT version FROM schema_migrations")
        }

        newly_applied: list[str] = []
        for version, sql in self._iter_migrations():
            if version in applied:
                continue
            with self._conn:
                self._conn.executescript(sql)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _utcnow_iso()),
                )
            newly_applied.append(version)
        return newly_applied

    @staticmethod
    def _iter_migrations() -> list[tuple[str, str]]:
        files = [
            f for f in resources.files(_MIGRATIONS_PACKAGE).iterdir() if f.name.endswith(".sql")
        ]
        files.sort(key=lambda f: f.name)
        return [(f.name, f.read_text(encoding="utf-8")) for f in files]

    # --------------------------------------------------------------- jobs CRUD

    def create_job(self, kind: str, request: dict[str, Any]) -> str:
        """Insert a new ``pending`` job, returning the generated UUID hex id."""
        job_id = uuid.uuid4().hex
        now = _utcnow_iso()
        with self._conn:
            self._conn.execute(
                "INSERT INTO jobs("
                "  id, status, kind, created_at, updated_at, request_json"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    JobStatus.PENDING.value,
                    kind,
                    now,
                    now,
                    _dumps(request) or "{}",
                ),
            )
        return job_id

    def get_job(self, job_id: str) -> Job | None:
        """Fetch a job by id, or ``None`` if it does not exist."""
        row = self._conn.execute(
            "SELECT id, status, kind, created_at, updated_at,"
            "       request_json, result_json, error"
            "  FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return Job(
            id=row["id"],
            status=JobStatus(row["status"]),
            kind=row["kind"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            request=_loads(row["request_json"]) or {},
            result=_loads(row["result_json"]),
            error=row["error"],
        )

    def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Update a job's status, refreshing ``updated_at``.

        Raises :class:`KeyError` if the job does not exist.
        """
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE jobs"
                "   SET status = ?,"
                "       updated_at = ?,"
                "       result_json = COALESCE(?, result_json),"
                "       error = COALESCE(?, error)"
                " WHERE id = ?",
                (status.value, now, _dumps(result), error, job_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"job not found: {job_id}")

    # -------------------------------------------------------------- steps CRUD

    def append_step(self, job_id: str, name: str, input: dict[str, Any]) -> int:
        """Append a new ``pending`` step to ``job_id``; returns its 0-based idx."""
        with self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(idx) + 1, 0) AS next_idx  FROM steps WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            idx = int(row["next_idx"])
            now = _utcnow_iso()
            self._conn.execute(
                "INSERT INTO steps("
                "  job_id, idx, name, status, started_at, input_json"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    idx,
                    name,
                    StepStatus.RUNNING.value,
                    now,
                    _dumps(input),
                ),
            )
        return idx

    def finish_step(
        self,
        job_id: str,
        idx: int,
        status: StepStatus,
        *,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Mark step ``idx`` of ``job_id`` finished. Raises ``KeyError`` if missing."""
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE steps"
                "   SET status = ?,"
                "       finished_at = ?,"
                "       output_json = COALESCE(?, output_json),"
                "       error = COALESCE(?, error)"
                " WHERE job_id = ? AND idx = ?",
                (status.value, now, _dumps(output), error, job_id, idx),
            )
            if cur.rowcount == 0:
                raise KeyError(f"step not found: job={job_id} idx={idx}")

    def list_steps(self, job_id: str) -> list[Step]:
        """Return all steps for ``job_id`` ordered by ``idx`` ascending."""
        rows = self._conn.execute(
            "SELECT id, job_id, idx, name, status, started_at, finished_at,"
            "       input_json, output_json, error"
            "  FROM steps WHERE job_id = ? ORDER BY idx ASC",
            (job_id,),
        ).fetchall()
        return [
            Step(
                id=row["id"],
                job_id=row["job_id"],
                idx=row["idx"],
                name=row["name"],
                status=StepStatus(row["status"]),
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                input=_loads(row["input_json"]),
                output=_loads(row["output_json"]),
                error=row["error"],
            )
            for row in rows
        ]

    # ----------------------------------------------------------- messages CRUD

    def append_message(self, job_id: str, role: str, content: str) -> None:
        """Append a chat-style message attached to ``job_id``."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO messages(job_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, role, content, _utcnow_iso()),
            )

    def list_messages(self, job_id: str) -> list[Message]:
        """Return all messages for ``job_id`` in insertion order."""
        rows = self._conn.execute(
            "SELECT id, job_id, role, content, created_at"
            "  FROM messages WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
        return [
            Message(
                id=row["id"],
                job_id=row["job_id"],
                role=row["role"],
                content=row["content"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ---------------------------------------------------------- artifacts CRUD

    def add_artifact(
        self,
        job_id: str,
        kind: str,
        *,
        path: str | None = None,
        content: str | None = None,
    ) -> None:
        """Record an artifact (file path or inline content) for ``job_id``."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO artifacts(job_id, kind, path, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job_id, kind, path, content, _utcnow_iso()),
            )

    def list_artifacts(self, job_id: str) -> list[Artifact]:
        """Return all artifacts for ``job_id`` in insertion order."""
        rows = self._conn.execute(
            "SELECT id, job_id, kind, path, content, created_at"
            "  FROM artifacts WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
        return [
            Artifact(
                id=row["id"],
                job_id=row["job_id"],
                kind=row["kind"],
                path=row["path"],
                content=row["content"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def delete_job(self, job_id: str) -> bool:
        """Delete a job and (via ``ON DELETE CASCADE``) all of its children."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cur.rowcount > 0
