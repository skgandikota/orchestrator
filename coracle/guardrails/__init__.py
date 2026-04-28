"""Local-only guardrails package.

Lightweight, dependency-free input/output checks that run before any
outbound LLM call (input side) and before any content is returned to the
caller (output side). Each rule is a small module exposing a ``check`` /
``scan`` function returning a :class:`GuardrailResult`. The
:mod:`pipeline` module chains them into a single
:class:`GuardrailPipeline` callable.

No external network calls; regex + heuristics only.
"""

from __future__ import annotations

from .pipeline import (
    GuardrailDecision,
    GuardrailPipeline,
    GuardrailResult,
    Severity,
    build_default_pipeline,
)

__all__ = [
    "GuardrailDecision",
    "GuardrailPipeline",
    "GuardrailResult",
    "Severity",
    "build_default_pipeline",
]
