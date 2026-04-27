"""Settings loader: TOML defaults + env-var overrides via pydantic v2."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

__all__ = [
    "DEFAULT_SETTINGS_PATH",
    "LoggingSettings",
    "OllamaSettings",
    "RamSettings",
    "SchedulerSettings",
    "Settings",
    "SettingsError",
    "load_settings",
]

ENV_PREFIX = "ORCHESTRATOR"
ENV_SEP = "__"

DEFAULT_SETTINGS_PATH = Path(__file__).with_name("settings.toml")


class SettingsError(RuntimeError):
    """Raised when settings cannot be loaded or validated."""


class RamSettings(BaseModel):
    soft_cap_mb: int = Field(8000, ge=1)
    hard_cap_mb: int = Field(11000, ge=1)
    poll_interval_seconds: float = Field(1.0, gt=0)

    @field_validator("hard_cap_mb")
    @classmethod
    def _hard_ge_soft(cls, v: int, info: Any) -> int:
        soft = info.data.get("soft_cap_mb")
        if soft is not None and v < soft:
            raise ValueError("hard_cap_mb must be >= soft_cap_mb")
        return v


class SchedulerSettings(BaseModel):
    max_concurrent_steps: int = Field(2, ge=1)
    checkpoint_every_step: bool = True


class OllamaSettings(BaseModel):
    base_url: str = "http://127.0.0.1:11434"
    request_timeout_seconds: int = Field(120, ge=1)


class LoggingSettings(BaseModel):
    model_config = {"protected_namespaces": ()}

    level: str = "INFO"
    json: bool = False

    @field_validator("level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        up = v.upper()
        if up not in allowed:
            raise ValueError(f"level must be one of {sorted(allowed)}")
        return up


class Settings(BaseModel):
    ram: RamSettings = Field(default_factory=RamSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SettingsError(f"Settings file not found: {path}")
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"Invalid TOML in {path}: {exc}") from exc


def _apply_env_overrides(
    data: dict[str, Any],
    env: dict[str, str],
    prefix: str = ENV_PREFIX,
) -> dict[str, Any]:
    """Override nested keys from env vars shaped ``ORCHESTRATOR__SECTION__KEY``."""
    head = f"{prefix}{ENV_SEP}"
    for raw_key, raw_val in env.items():
        if not raw_key.startswith(head):
            continue
        path = raw_key[len(head) :].split(ENV_SEP)
        if not path or any(not part for part in path):
            continue
        cursor = data
        for part in path[:-1]:
            key = part.lower()
            existing = cursor.get(key)
            if not isinstance(existing, dict):
                existing = {}
                cursor[key] = existing
            cursor = existing
        cursor[path[-1].lower()] = raw_val
    return data


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from TOML, applying env-var overrides, then validate.

    Args:
        path: Optional path to a TOML file. Defaults to the bundled
            ``settings.toml``.

    Raises:
        SettingsError: If the file is missing, malformed, or fails validation.
    """
    src = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
    raw = _read_toml(src)
    merged = _apply_env_overrides(raw, dict(os.environ))
    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
        raise SettingsError(f"Invalid settings: {exc}") from exc
