"""Pipeline steps that transform user input into frontier-model calls."""

from .refine import (
    ConsolidatedBrief,
    ModelClient,
    RefinedPrompt,
    RefineError,
    refine,
)

__all__ = [
    "ConsolidatedBrief",
    "ModelClient",
    "RefineError",
    "RefinedPrompt",
    "refine",
"""Pipeline package: ordered AI-driven steps."""

from __future__ import annotations

from .verify import (
    ExecutableStep,
    OllamaClient,
    Plan,
    PlanStep,
    StateRecorder,
    VerifyDecision,
    verify,
)

__all__ = [
    "ExecutableStep",
    "OllamaClient",
    "Plan",
    "PlanStep",
    "StateRecorder",
    "VerifyDecision",
    "verify",
]
