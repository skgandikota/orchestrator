"""Exceptions raised by the big-AI provider router."""

from __future__ import annotations

__all__ = [
    "BigAIError",
    "ProviderQuotaExceeded",
    "ProviderUnavailable",
    "UnknownProvider",
]


class BigAIError(RuntimeError):
    """Base class for errors raised by the big-AI router."""


class ProviderQuotaExceeded(BigAIError):
    """A provider rejected the call because its free-tier quota is exhausted.

    Raised on:
      * In-process quota tracker hits the configured daily limit, or
      * The upstream provider returns ``429`` / ``litellm.RateLimitError``.
    """

    def __init__(self, provider: str, message: str | None = None) -> None:
        self.provider = provider
        super().__init__(message or f"quota exceeded for provider {provider!r}")


class ProviderUnavailable(BigAIError):
    """A provider could not be reached or returned a 5xx / connection error."""

    def __init__(self, provider: str, message: str | None = None) -> None:
        self.provider = provider
        super().__init__(message or f"provider {provider!r} unavailable")


class UnknownProvider(BigAIError):
    """The caller asked for a provider that is not configured."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"unknown provider {provider!r}")
