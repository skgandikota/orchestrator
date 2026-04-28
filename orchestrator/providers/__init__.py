"""Provider fallback chain with per-provider circuit breaker.

The :mod:`orchestrator.providers.fallback` module is the public surface; this
package exists so the chain can grow alongside concrete provider adapters
(LiteLLM router from #8, Playwright browser driver from #9) without breaking
imports.
"""

from orchestrator.providers.fallback import (
    AllProvidersFailed,
    AuthError,
    BrowserDriverCrashed,
    BrowserProvider,
    CircuitBreaker,
    CircuitState,
    FallbackChain,
    InvalidRequest,
    Provider,
    ProviderUnavailable,
    QuotaExceeded,
)

__all__ = [
    "AllProvidersFailed",
    "AuthError",
    "BrowserDriverCrashed",
    "BrowserProvider",
    "CircuitBreaker",
    "CircuitState",
    "FallbackChain",
    "InvalidRequest",
    "Provider",
    "ProviderUnavailable",
    "QuotaExceeded",
]
