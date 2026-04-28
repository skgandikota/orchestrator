"""LiteLLM-based router for big-AI providers (Gemini / Groq / Ollama Cloud).

This module is the API-only entry point used by the orchestrator's ``deep`` and
``research`` pipelines. Providers + models are configured in
``orchestrator/config/settings.toml`` under ``[providers.*]``; environment
variables (e.g. ``GEMINI_API_KEY``) override the file values.

The fallback chain (browser, retry/backoff) lives in a separate issue
(#p2-fallback). This module only exposes ``ProviderQuotaExceeded`` /
``ProviderUnavailable`` so callers can decide whether to advance.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import litellm
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.types.utils import ModelResponse
from pydantic import BaseModel, Field

from .errors import ProviderQuotaExceeded, ProviderUnavailable, UnknownProvider
from .quota import QuotaTracker

__all__ = ["LitellmRouter", "Provider", "ProviderConfigError"]


# Default litellm model ids per provider as specified in the issue AC.
_DEFAULT_LITELLM_MODELS: dict[str, str] = {
    "gemini": "gemini/gemini-1.5-pro",
    "groq": "groq/llama-3.3-70b-versatile",
    "ollama_cloud": "ollama/llama3.1",
}

# settings.toml key -> env var that wins over the file value.
_ENV_API_KEYS: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama_cloud": "OLLAMA_CLOUD_API_KEY",
}

_DEFAULT_ORDER: tuple[str, ...] = ("gemini", "groq", "ollama_cloud")


class ProviderConfigError(RuntimeError):
    """Raised when settings.toml is missing or malformed for the providers."""


class Provider(BaseModel):
    """Pydantic config for a single big-AI provider.

    Attributes:
        name: Logical name (``gemini``/``groq``/``ollama_cloud`` or custom).
        litellm_model: Fully qualified litellm model id, e.g.
            ``gemini/gemini-1.5-pro``.
        api_key: Resolved API key (env-var-overridden).
        base_url: Optional override for OpenAI-compatible endpoints.
        daily_quota: Optional in-process daily request cap.
    """

    name: str
    litellm_model: str
    api_key: str | None = None
    base_url: str | None = None
    daily_quota: int | None = Field(default=None, ge=0)


def _resolve_settings_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("ORCHESTRATOR_SETTINGS")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "config" / "settings.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ProviderConfigError(f"settings file not found: {path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


class LitellmRouter:
    """Route chat-completion calls to a configured big-AI provider via LiteLLM.

    The router does **not** retry across providers itself; it raises a typed
    error and lets the Phase 2 fallback wrapper (#p2-fallback) advance.
    """

    def __init__(
        self,
        providers: dict[str, Provider] | None = None,
        order: list[str] | None = None,
        quota_tracker: QuotaTracker | None = None,
        settings_path: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if providers is None:
            providers, file_order = self._load_from_settings(settings_path, env)
            order = order or file_order
        self._providers: dict[str, Provider] = providers
        self._order: list[str] = list(order or providers.keys())
        self._quota = quota_tracker or QuotaTracker()
        for name, prov in self._providers.items():
            self._quota.configure(name, prov.daily_quota)
        self._validate_order(self._order)

    @staticmethod
    def _load_from_settings(
        settings_path: Path | None,
        env: dict[str, str] | None,
    ) -> tuple[dict[str, Provider], list[str]]:
        env = dict(os.environ) if env is None else dict(env)
        path = _resolve_settings_path(settings_path)
        raw = _read_toml(path)
        providers_section = raw.get("providers")
        if not isinstance(providers_section, dict):
            raise ProviderConfigError(
                f"missing [providers] section in {path}",
            )
        order_value = providers_section.get("order")
        order: list[str]
        if isinstance(order_value, list) and all(isinstance(v, str) for v in order_value):
            order = list(order_value)
        else:
            order = list(_DEFAULT_ORDER)

        providers: dict[str, Provider] = {}
        for name, cfg in providers_section.items():
            if name == "order" or not isinstance(cfg, dict):
                continue
            litellm_model = cfg.get("litellm_model") or _DEFAULT_LITELLM_MODELS.get(name)
            if not litellm_model:
                raise ProviderConfigError(
                    f"provider {name!r} missing 'litellm_model' and has no default",
                )
            api_key = cfg.get("api_key")
            env_var = _ENV_API_KEYS.get(name) or f"{name.upper()}_API_KEY"
            api_key = env.get(env_var) or api_key
            providers[name] = Provider(
                name=name,
                litellm_model=str(litellm_model),
                api_key=str(api_key) if api_key else None,
                base_url=cfg.get("base_url"),
                daily_quota=cfg.get("daily_quota"),
            )
        if not providers:
            raise ProviderConfigError(f"no providers configured in {path}")
        return providers, order

    def _validate_order(self, order: list[str]) -> None:
        for name in order:
            if name not in self._providers:
                raise UnknownProvider(name)

    @property
    def providers(self) -> dict[str, Provider]:
        return dict(self._providers)

    @property
    def order(self) -> list[str]:
        return list(self._order)

    @property
    def quota(self) -> QuotaTracker:
        return self._quota

    def complete(
        self,
        messages: list[dict[str, Any]],
        prefer: list[str] | None = None,
        **kw: Any,
    ) -> ModelResponse:
        """Run a chat completion against the first preferred provider.

        Args:
            messages: OpenAI-style chat messages.
            prefer: Provider preference list. If ``None`` or empty, the
                ``[providers].order`` configured in settings is used.
            **kw: Forwarded to :func:`litellm.completion`.

        Returns:
            The :class:`litellm.types.utils.ModelResponse`.

        Raises:
            UnknownProvider: ``prefer`` references a provider that is not
                configured.
            ProviderQuotaExceeded: The local quota tracker or the upstream
                provider says we are out of quota for the chosen provider.
            ProviderUnavailable: The upstream provider returned a connection
                error / 5xx.
        """
        chosen_order = list(prefer) if prefer else list(self._order)
        if not chosen_order:
            raise UnknownProvider("<empty>")
        for name in chosen_order:
            if name not in self._providers:
                raise UnknownProvider(name)

        # Per the issue AC: the router calls the first preferred provider and
        # surfaces a typed error. Cross-provider fallback is the wrapper's job.
        target = chosen_order[0]
        provider = self._providers[target]
        self._quota.check_and_increment(target)

        call_kwargs: dict[str, Any] = dict(kw)
        if provider.api_key is not None:
            call_kwargs.setdefault("api_key", provider.api_key)
        if provider.base_url is not None:
            call_kwargs.setdefault("base_url", provider.base_url)

        try:
            return litellm.completion(  # type: ignore[no-any-return]
                model=provider.litellm_model,
                messages=messages,
                **call_kwargs,
            )
        except RateLimitError as exc:
            raise ProviderQuotaExceeded(target, str(exc)) from exc
        except (APIConnectionError, ServiceUnavailableError, Timeout) as exc:
            raise ProviderUnavailable(target, str(exc)) from exc
        except APIError as exc:
            status = getattr(exc, "status_code", None)
            if status is not None and 500 <= int(status) < 600:
                raise ProviderUnavailable(target, str(exc)) from exc
            raise

    def stream(
        self,
        messages: list[dict[str, Any]],
        prefer: list[str] | None = None,
        **kw: Any,
    ) -> Iterator[Any]:
        """Streaming variant of :meth:`complete`. Yields litellm chunks."""
        kw.setdefault("stream", True)
        result = self.complete(messages, prefer=prefer, **kw)
        return iter(result)  # type: ignore[arg-type]
