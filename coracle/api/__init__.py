"""HTTP API surfaces for the coracle (OpenAI-compatible)."""

from coracle.api.app import create_app
from coracle.api.openai_compat import (
    ChatBackend,
    PipelineEvent,
    build_router,
    set_backend,
)

__all__ = [
    "ChatBackend",
    "PipelineEvent",
    "build_router",
    "create_app",
    "set_backend",
]
