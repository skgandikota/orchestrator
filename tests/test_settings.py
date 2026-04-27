"""Tests for orchestrator.config.settings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchestrator.config.settings import (
    DEFAULT_SETTINGS_PATH,
    Settings,
    SettingsError,
    load_settings,
)


def _strip_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("ORCHESTRATOR__"):
            monkeypatch.delenv(k, raising=False)


def test_defaults_load(monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_env(monkeypatch)
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.ram.soft_cap_mb == 8000
    assert s.ram.hard_cap_mb == 11000
    assert s.scheduler.max_concurrent_steps == 2
    assert s.ollama.base_url.startswith("http://")
    assert s.logging.level == "INFO"
    assert s.logging.json is False
    assert DEFAULT_SETTINGS_PATH.exists()


def test_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRATOR__RAM__SOFT_CAP_MB", "4096")
    monkeypatch.setenv("ORCHESTRATOR__RAM__HARD_CAP_MB", "9000")
    monkeypatch.setenv("ORCHESTRATOR__LOGGING__JSON", "true")
    monkeypatch.setenv("ORCHESTRATOR__OLLAMA__BASE_URL", "http://example.invalid:9999")

    s = load_settings()
    assert s.ram.soft_cap_mb == 4096
    assert s.ram.hard_cap_mb == 9000
    assert s.logging.json is True
    assert s.ollama.base_url == "http://example.invalid:9999"


def test_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.toml"
    with pytest.raises(SettingsError, match="not found"):
        load_settings(missing)


def test_invalid_type_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_env(monkeypatch)
    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[ram]\nsoft_cap_mb = "not-an-int"\n',
        encoding="utf-8",
    )
    with pytest.raises(SettingsError):
        load_settings(bad)


def test_hard_cap_must_be_ge_soft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_env(monkeypatch)
    bad = tmp_path / "bad.toml"
    bad.write_text(
        "[ram]\nsoft_cap_mb = 9000\nhard_cap_mb = 1000\n",
        encoding="utf-8",
    )
    with pytest.raises(SettingsError):
        load_settings(bad)


def test_invalid_log_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_env(monkeypatch)
    bad = tmp_path / "bad.toml"
    bad.write_text('[logging]\nlevel = "VERBOSE"\n', encoding="utf-8")
    with pytest.raises(SettingsError):
        load_settings(bad)


def test_malformed_toml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(SettingsError, match="Invalid TOML"):
        load_settings(bad)


def test_env_override_skips_empty_segment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars with empty path components are silently ignored (line 146)."""
    _strip_env(monkeypatch)
    cfg = tmp_path / "ok.toml"
    cfg.write_text("[ram]\nsoft_cap_mb = 4096\n", encoding="utf-8")
    # ORCHESTRATOR____KEY -> path == ["", "KEY"] -> any empty -> continue
    monkeypatch.setenv("ORCHESTRATOR____STRAY", "ignored")
    s = load_settings(cfg)
    assert s.ram.soft_cap_mb == 4096


def test_env_override_replaces_non_dict_intermediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When an intermediate TOML key is a scalar, env override replaces it with a dict (152-153)."""
    _strip_env(monkeypatch)
    cfg = tmp_path / "ok.toml"
    # tools.web is set as a *string* here so the env override has to replace it with a dict.
    cfg.write_text(
        '[tools]\nweb = "scalar-not-a-dict"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHESTRATOR__TOOLS__WEB__USER_AGENT", "ua-from-env/1.0")
    s = load_settings(cfg)
    assert s.tools.web.user_agent == "ua-from-env/1.0"
