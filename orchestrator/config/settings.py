"""Settings loader: TOML defaults + env-var overrides via pydantic v2."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

__all__ = [
    "DEFAULT_SETTINGS_PATH",
    "FsToolSettings",
    "GuardrailsSettings",
    "LoggingSettings",
    "OllamaSettings",
    "RamSettings",
    "SchedulerSettings",
    "Settings",
    "SettingsError",
    "ShellToolSettings",
    "StateSettings",
    "StatusSettings",
    "ToolsSettings",
    "WebToolSettings",
    "load_settings",
]

ENV_PREFIX = "ORCHESTRATOR"
ENV_SEP = "__"

DEFAULT_SETTINGS_PATH = Path(__file__).with_name("settings.toml")


class SettingsError(RuntimeError):
    """Raised when settings cannot be loaded or validated."""


class RamSettings(BaseModel):
    soft_cap_mb: int = Field(7000, ge=1)
    hard_cap_mb: int = Field(5000, ge=1)
    poll_interval_s: float = Field(1.0, gt=0)

    @field_validator("hard_cap_mb")
    @classmethod
    def _hard_lt_soft(cls, v: int, info: Any) -> int:
        soft = info.data.get("soft_cap_mb")
        if soft is not None and v >= soft:
            raise ValueError("hard_cap_mb must be < soft_cap_mb (caps are minimum free RAM)")
        return v


class SchedulerSettings(BaseModel):
    max_concurrent_steps: int = Field(2, ge=1)
    checkpoint_every_step: bool = True
    acquire_timeout_s: float = Field(120.0, gt=0)
    min_free_mb_for_load: int = Field(5500, ge=1)


class OllamaSettings(BaseModel):
    base_url: str = "http://127.0.0.1:11434"
    request_timeout_seconds: int = Field(120, ge=1)
    request_timeout_s: float = Field(120.0, gt=0)
    keep_alive: str = "24h"
    reasoning_model: str = "qwen2.5:7b"
    coder_model: str = "qwen2.5-coder:7b"


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


class FsToolSettings(BaseModel):
    workspace_root: str = str(Path.home() / "orchestrator-workspace")


class ShellToolSettings(BaseModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(
        default_factory=lambda: [
            "rm",
            "rmdir",
            "sudo",
            "su",
            "mv",
            "dd",
            "mkfs",
            "shutdown",
            "reboot",
            "kill",
            "killall",
            "chown",
            "chmod",
        ]
    )


class WebToolSettings(BaseModel):
    """Settings for the web fetch + search tool (`orchestrator.tools.web`)."""

    user_agent: str = "orchestrator-bot/0.1 (+https://github.com/skgandikota/orchestrator)"
    allow_private: bool = False
    brave_api_key: str | None = None


class StateSettings(BaseModel):
    """Settings for the durable state store (``orchestrator.core.state``)."""

    db_path: Path = Field(default=Path("./orchestrator.db"))


class GuardrailsSettings(BaseModel):
    """Settings for local input/output guardrails."""

    enabled: bool = True
    redact_pii: bool = True
    redact_secrets: bool = True
    detect_injection: bool = True
    enforce_policy: bool = True
    daily_token_quota: int | None = None
    max_token_fraction: float = Field(0.8, gt=0.0, le=1.0)


class StatusSettings(BaseModel):
    """Settings for the status subsystem (mode B narrator, issue #14)."""

    narrator_enabled: bool = False
    narrator_model: str = "qwen2.5:1.5b"
    narrator_max_tokens: int = Field(80, ge=1)


class ToolsSettings(BaseModel):
    fs: FsToolSettings = Field(default_factory=FsToolSettings)
    shell: ShellToolSettings = Field(default_factory=ShellToolSettings)
    web: WebToolSettings = Field(default_factory=WebToolSettings)


class Settings(BaseModel):
    ram: RamSettings = Field(default_factory=RamSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    state: StateSettings = Field(default_factory=StateSettings)
    status: StatusSettings = Field(default_factory=StatusSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    guardrails: GuardrailsSettings = Field(default_factory=GuardrailsSettings)


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
