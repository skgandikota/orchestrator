"""FastAPI application factory for the orchestrator HTTP surface.

This module is intentionally small. Other interface routers (e.g. the
OpenAI-compatible router from #11, native task API from #15) mount
themselves here too; keep edits idempotent and additive so the routers
can be merged without conflict.
"""

from __future__ import annotations

from fastapi import FastAPI

from orchestrator.api.embeddings import router as embeddings_router
from orchestrator.api.openai_compat import build_router
from orchestrator.api.tasks import router as tasks_router

__all__ = ["app", "create_app"]


def create_app() -> FastAPI:
    """Create and return the FastAPI app with all routers mounted."""
    app = FastAPI(
        title="orchestrator",
        version="0.1.0",
        description="Local-first agent orchestrator (OpenAI-compatible API).",
    )
    app.include_router(build_router())
    app.include_router(embeddings_router, prefix="/v1")
    app.include_router(tasks_router)
    return app


app = create_app()
