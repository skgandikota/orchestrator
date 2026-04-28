"""FastAPI application factory for the orchestrator HTTP surface."""

from __future__ import annotations

from fastapi import FastAPI

from orchestrator.api.openai_compat import build_router

__all__ = ["create_app"]


def create_app() -> FastAPI:
    """Create and return the FastAPI app with all routers mounted."""
    app = FastAPI(
        title="orchestrator",
        version="0.1.0",
        description="Local-first agent orchestrator (OpenAI-compatible API).",
    )
    app.include_router(build_router())
    return app
