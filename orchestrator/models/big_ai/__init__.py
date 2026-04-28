"""Big-AI provider router (Phase 2).

Public surface:
    - :class:`LitellmRouter`: routes chat completions across configured
      free-tier providers via the ``litellm`` library.
    - :class:`QuotaTracker`: in-memory per-provider request counter.
    - :class:`ProviderQuotaExceeded` / :class:`ProviderUnavailable`: typed
      errors that the Phase 2 fallback wrapper (#p2-fallback) reacts to.
"""

from __future__ import annotations

from .errors import (
    BigAIError,
    ProviderQuotaExceeded,
    ProviderUnavailable,
    UnknownProvider,
)
from .litellm_router import LitellmRouter, Provider, ProviderConfigError
from .quota import QuotaTracker

__all__ = [
    "BigAIError",
    "LitellmRouter",
    "Provider",
    "ProviderConfigError",
    "ProviderQuotaExceeded",
    "ProviderUnavailable",
    "QuotaTracker",
    "UnknownProvider",
]
