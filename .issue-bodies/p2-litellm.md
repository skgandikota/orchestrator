## Context

Part of the **Phase 2 — Big-AI providers** epic (#2).
See [`docs/PLAN.md` § Phase 2](../blob/main/docs/PLAN.md#phase-2--big-ai-providers).

The orchestrator's `deep` and `research` pipelines hand the consolidated/refined prompt off to a "big AI" planner. We want one Python entry point that can talk to several free-tier API providers (Gemini, Groq, Ollama Cloud) via [`litellm`](https://github.com/BerriAI/litellm), respect a caller-supplied preference order, and track per-provider quota in memory so we can fail fast when a free tier is exhausted. This issue covers the API-only router; browser fallback (#p2-browser) and the unified entry point with retry/backoff (#p2-fallback) build on top.

## Acceptance Criteria

- [ ] `orchestrator/models/big_ai/litellm_router.py` exports `LitellmRouter` with `complete(messages: list[dict], prefer: list[str] | None = None, **kw) -> ChatCompletionResponse`
- [ ] Supports providers: `gemini` (default model `gemini-1.5-pro`), `groq` (default model `llama-3.3-70b-versatile`), `ollama-cloud` (default model configurable)
- [ ] A `Provider` Pydantic config (name, litellm model id, api key, optional base_url, monthly/daily request quota) is loaded from `settings.toml` (`[providers.gemini]`, `[providers.groq]`, `[providers.ollama_cloud]`)
- [ ] Env-var override: `GEMINI_API_KEY`, `GROQ_API_KEY`, `OLLAMA_CLOUD_API_KEY` win over `settings.toml`
- [ ] `orchestrator/models/big_ai/quota.py` exposes a thread-safe in-memory `QuotaTracker` (per-provider counter + reset window); router calls `tracker.check_and_increment(provider)` and raises `ProviderQuotaExceeded` from `orchestrator.models.big_ai.errors` when the limit is hit
- [ ] Router raises `ProviderUnavailable` on connection / 5xx errors and `ProviderQuotaExceeded` on 429 / litellm `RateLimitError`
- [ ] If `prefer` is omitted, falls back to the order configured in `settings.toml` (`[providers].order`)
- [ ] `orchestrator/models/big_ai/__init__.py` re-exports `LitellmRouter`, `QuotaTracker`, `ProviderQuotaExceeded`, `ProviderUnavailable`
- [ ] Unit tests in `tests/models/big_ai/test_litellm_router.py` use `litellm`'s mock backend (`mock_response=...`) or `monkeypatch` of `litellm.completion`; **no live API calls in CI**
- [ ] Tests cover: happy path per provider, quota-exhausted raises `ProviderQuotaExceeded`, unknown provider rejected, env-var overrides settings.toml
- [ ] Type hints on all public surfaces; passes `ruff check`

## Files / paths to touch
- `orchestrator/models/big_ai/__init__.py` (new, re-exports)
- `orchestrator/models/big_ai/litellm_router.py` (new)
- `orchestrator/models/big_ai/quota.py` (new)
- `orchestrator/models/big_ai/errors.py` (new — `ProviderQuotaExceeded`, `ProviderUnavailable`)
- `orchestrator/config/settings.toml` (extend with `[providers.*]` sections + sample keys placeholder)
- `tests/models/big_ai/__init__.py` (new)
- `tests/models/big_ai/test_litellm_router.py` (new)
- `pyproject.toml` (add `litellm` dependency)

## Suggested approach

Define a `Provider` Pydantic model (`name`, `litellm_model`, `api_key`, `base_url: str | None`, `daily_quota: int | None`). On startup, `LitellmRouter` reads `settings.toml`, layers env-var overrides, and builds a `dict[str, Provider]`. `complete()` walks `prefer` (or the configured default order), and for each provider: calls `quota.check_and_increment(name)`, then `litellm.completion(model=provider.litellm_model, api_key=provider.api_key, messages=messages, **kw)`. Translate `litellm.exceptions.RateLimitError` → `ProviderQuotaExceeded` and `APIConnectionError`/timeouts → `ProviderUnavailable`. Re-raise so the caller (Phase 2 fallback wrapper) decides whether to advance.

Keep `QuotaTracker` simple — `dict[str, int]` guarded by a `threading.Lock`, with a `reset_at: datetime` per provider for daily windows. Phase 7 will swap this for SQLite persistence; document the seam clearly.

For tests, use `litellm`'s built-in `mock_response` argument or `monkeypatch.setattr("litellm.completion", fake)`. Parametrize across providers. Verify quota errors bubble up as `ProviderQuotaExceeded`, not the underlying litellm class.

## Dependencies
- Blocked by todo `p1-smoke` (Phase 1 foundation — settings loader, logging, project skeleton must exist first)

## Definition of Done
- [ ] Acceptance Criteria met
- [ ] Tests added and passing
- [ ] `ruff check` clean
- [ ] Type hints on public surfaces
- [ ] PR body contains `Closes #<this-issue>`
- [ ] Browser drivers run in **separate subprocess** (architectural rule)
