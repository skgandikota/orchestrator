"""Unit tests for :mod:`orchestrator.models.big_ai.litellm_router`.

Tests use either litellm's ``mock_response=`` argument or ``monkeypatch`` of
``litellm.completion`` so that no real network calls are made.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import litellm
import pytest
from litellm.exceptions import APIConnectionError, APIError, RateLimitError, Timeout

from orchestrator.models.big_ai import (
    BigAIError,
    LitellmRouter,
    Provider,
    ProviderConfigError,
    ProviderQuotaExceeded,
    ProviderUnavailable,
    QuotaTracker,
    UnknownProvider,
)
from orchestrator.models.big_ai.litellm_router import _resolve_settings_path

SAMPLE_SETTINGS = """\
[providers]
order = ["gemini", "groq", "ollama_cloud"]

[providers.gemini]
litellm_model = "gemini/gemini-1.5-pro"
api_key = "from-file-gemini"
daily_quota = 5

[providers.groq]
litellm_model = "groq/llama-3.3-70b-versatile"
api_key = "from-file-groq"
daily_quota = 3

[providers.ollama_cloud]
litellm_model = "ollama/llama3.1"
api_key = "from-file-ollama"
base_url = "https://ollama.example.com"
"""


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    p = tmp_path / "settings.toml"
    p.write_text(SAMPLE_SETTINGS, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# QuotaTracker
# ---------------------------------------------------------------------------


def test_quota_tracker_increments_until_limit() -> None:
    q = QuotaTracker()
    q.configure("gemini", 2)
    assert q.check_and_increment("gemini") == 1
    assert q.check_and_increment("gemini") == 2
    with pytest.raises(ProviderQuotaExceeded):
        q.check_and_increment("gemini")


def test_quota_tracker_unlimited_when_none() -> None:
    q = QuotaTracker()
    q.configure("groq", None)
    for _ in range(20):
        q.check_and_increment("groq")
    assert q.snapshot("groq").count == 20


def test_quota_tracker_reset_rolls_window() -> None:
    q = QuotaTracker(window=timedelta(seconds=0))
    q.configure("groq", 1)
    q.check_and_increment("groq")
    # window=0 means the next call should roll the counter.
    assert q.check_and_increment("groq") == 1


def test_quota_tracker_reset_specific_and_all() -> None:
    q = QuotaTracker()
    q.configure("a", 5)
    q.configure("b", 5)
    q.check_and_increment("a")
    q.check_and_increment("b")
    q.reset("a")
    assert q.snapshot("a").count == 0
    assert q.snapshot("b").count == 1
    q.reset()
    assert q.snapshot("b").count == 0


# ---------------------------------------------------------------------------
# Settings loading
# ---------------------------------------------------------------------------


def test_loads_providers_from_settings(settings_file: Path) -> None:
    router = LitellmRouter(settings_path=settings_file, env={})
    assert set(router.providers) == {"gemini", "groq", "ollama_cloud"}
    assert router.order == ["gemini", "groq", "ollama_cloud"]
    assert router.providers["gemini"].api_key == "from-file-gemini"
    assert router.providers["ollama_cloud"].base_url == "https://ollama.example.com"


def test_env_var_overrides_settings_toml(settings_file: Path) -> None:
    router = LitellmRouter(
        settings_path=settings_file,
        env={"GEMINI_API_KEY": "from-env", "GROQ_API_KEY": "groq-env"},
    )
    assert router.providers["gemini"].api_key == "from-env"
    assert router.providers["groq"].api_key == "groq-env"
    # ollama_cloud was not overridden.
    assert router.providers["ollama_cloud"].api_key == "from-file-ollama"


def test_missing_settings_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ProviderConfigError):
        LitellmRouter(settings_path=tmp_path / "nope.toml", env={})


def test_missing_providers_section_raises(tmp_path: Path) -> None:
    p = tmp_path / "s.toml"
    p.write_text("[other]\nx=1\n", encoding="utf-8")
    with pytest.raises(ProviderConfigError):
        LitellmRouter(settings_path=p, env={})


def test_empty_providers_section_raises(tmp_path: Path) -> None:
    p = tmp_path / "s.toml"
    p.write_text("[providers]\norder = []\n", encoding="utf-8")
    with pytest.raises(ProviderConfigError):
        LitellmRouter(settings_path=p, env={})


def test_provider_without_default_model_raises(tmp_path: Path) -> None:
    p = tmp_path / "s.toml"
    p.write_text(
        "[providers]\norder=['custom']\n[providers.custom]\napi_key='x'\n",
        encoding="utf-8",
    )
    with pytest.raises(ProviderConfigError):
        LitellmRouter(settings_path=p, env={})


def test_resolve_settings_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "x.toml"
    monkeypatch.setenv("ORCHESTRATOR_SETTINGS", str(target))
    assert _resolve_settings_path(None) == target
    monkeypatch.delenv("ORCHESTRATOR_SETTINGS", raising=False)
    # no env, default path resolves under the package
    p = _resolve_settings_path(None)
    assert p.name == "settings.toml"


def test_resolve_settings_path_explicit(tmp_path: Path) -> None:
    target = tmp_path / "y.toml"
    assert _resolve_settings_path(target) == target


def test_default_settings_file_loads() -> None:
    router = LitellmRouter(env={})
    assert "gemini" in router.providers


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_name", ["gemini", "groq", "ollama_cloud"])
def test_happy_path_per_provider(
    settings_file: Path,
    provider_name: str,
) -> None:
    router = LitellmRouter(settings_path=settings_file, env={})
    resp = router.complete(
        messages=[{"role": "user", "content": "hi"}],
        prefer=[provider_name],
        mock_response="hello-world",
    )
    assert resp.choices[0].message.content == "hello-world"


def test_complete_uses_configured_order_when_prefer_omitted(
    settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    original = litellm.completion

    def fake_completion(*, model: str, messages: list[dict[str, Any]], **kw: Any) -> Any:
        captured["model"] = model
        captured["api_key"] = kw.get("api_key")
        kw.pop("mock_response", None)
        return original(model=model, messages=messages, mock_response="ok", **kw)

    monkeypatch.setattr(litellm, "completion", fake_completion)
    router = LitellmRouter(settings_path=settings_file, env={})
    router.complete(messages=[{"role": "user", "content": "x"}])
    # Default order starts with gemini.
    assert captured["model"] == "gemini/gemini-1.5-pro"
    assert captured["api_key"] == "from-file-gemini"


def test_complete_unknown_provider_raises(settings_file: Path) -> None:
    router = LitellmRouter(settings_path=settings_file, env={})
    with pytest.raises(UnknownProvider):
        router.complete(messages=[{"role": "user", "content": "x"}], prefer=["bogus"])


def test_complete_empty_prefer_falls_back_to_order(settings_file: Path) -> None:
    router = LitellmRouter(settings_path=settings_file, env={})
    resp = router.complete(
        messages=[{"role": "user", "content": "x"}],
        prefer=[],
        mock_response="default",
    )
    assert resp.choices[0].message.content == "default"


def test_complete_quota_tracker_blocks_further_calls(
    settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = LitellmRouter(settings_path=settings_file, env={})
    # groq has daily_quota=3 in fixture.
    for _ in range(3):
        router.complete(
            messages=[{"role": "user", "content": "hi"}],
            prefer=["groq"],
            mock_response="ok",
        )
    with pytest.raises(ProviderQuotaExceeded) as ei:
        router.complete(
            messages=[{"role": "user", "content": "hi"}],
            prefer=["groq"],
            mock_response="ok",
        )
    assert ei.value.provider == "groq"
    assert isinstance(ei.value, BigAIError)


def test_complete_translates_rate_limit_error(
    settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kw: Any) -> Any:
        raise RateLimitError(message="429", llm_provider="gemini", model="x")

    monkeypatch.setattr(litellm, "completion", boom)
    router = LitellmRouter(settings_path=settings_file, env={})
    with pytest.raises(ProviderQuotaExceeded):
        router.complete(messages=[{"role": "user", "content": "x"}], prefer=["gemini"])


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: APIConnectionError(message="boom", llm_provider="gemini", model="x"),
        lambda: Timeout(message="slow", llm_provider="gemini", model="x"),
    ],
)
def test_complete_translates_connection_errors(
    settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_factory: Any,
) -> None:
    def boom(**kw: Any) -> Any:
        raise exc_factory()

    monkeypatch.setattr(litellm, "completion", boom)
    router = LitellmRouter(settings_path=settings_file, env={})
    with pytest.raises(ProviderUnavailable):
        router.complete(messages=[{"role": "user", "content": "x"}], prefer=["gemini"])


def test_complete_translates_5xx_apierror(
    settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kw: Any) -> Any:
        raise APIError(
            status_code=503,
            message="upstream",
            llm_provider="gemini",
            model="x",
        )

    monkeypatch.setattr(litellm, "completion", boom)
    router = LitellmRouter(settings_path=settings_file, env={})
    with pytest.raises(ProviderUnavailable):
        router.complete(messages=[{"role": "user", "content": "x"}], prefer=["gemini"])


def test_complete_passes_through_4xx_apierror(
    settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kw: Any) -> Any:
        raise APIError(
            status_code=400,
            message="bad",
            llm_provider="gemini",
            model="x",
        )

    monkeypatch.setattr(litellm, "completion", boom)
    router = LitellmRouter(settings_path=settings_file, env={})
    with pytest.raises(APIError):
        router.complete(messages=[{"role": "user", "content": "x"}], prefer=["gemini"])


def test_complete_validates_prefer_entries(settings_file: Path) -> None:
    router = LitellmRouter(settings_path=settings_file, env={})
    with pytest.raises(UnknownProvider):
        router.complete(
            messages=[{"role": "user", "content": "x"}],
            prefer=["gemini", "nope"],
        )


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


def test_stream_yields_chunks(
    settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = ["a", "b", "c"]

    def fake_completion(**kw: Any) -> Any:
        return iter(chunks)

    monkeypatch.setattr(litellm, "completion", fake_completion)
    router = LitellmRouter(settings_path=settings_file, env={})
    out = list(router.stream(messages=[{"role": "user", "content": "x"}], prefer=["gemini"]))
    assert out == chunks


# ---------------------------------------------------------------------------
# Direct construction (no settings file)
# ---------------------------------------------------------------------------


def test_direct_provider_dict_construction() -> None:
    providers = {
        "groq": Provider(
            name="groq",
            litellm_model="groq/llama-3.3-70b-versatile",
            api_key="k",
            daily_quota=10,
        )
    }
    router = LitellmRouter(providers=providers, order=["groq"])
    resp = router.complete(
        messages=[{"role": "user", "content": "x"}],
        mock_response="hi",
    )
    assert resp.choices[0].message.content == "hi"


def test_direct_construction_invalid_order_raises() -> None:
    providers = {
        "groq": Provider(name="groq", litellm_model="groq/llama-3.3-70b-versatile"),
    }
    with pytest.raises(UnknownProvider):
        LitellmRouter(providers=providers, order=["nope"])
