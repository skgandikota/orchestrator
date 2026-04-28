"""Pipeline parse step.

Normalises a raw big-model output (free-form text, JSON, or marked-up
plan/code) into a strongly-typed :class:`ParsedActions` document.

Multiple parsing strategies are attempted in order:

1. Strict ``json.loads`` of the entire payload.
2. Extraction of ```json``-fenced markdown blocks.
3. Regex extraction of OpenAI-style ``tool_call`` markers.
4. A repair pass via the supplied reasoning model (one re-roll only).
5. Fallback: treat the whole output as a ``message_to_user`` action.

Invalid action payloads are logged and dropped; they never crash the
pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft202012Validator, ValidationError
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ActionItem",
    "ActionType",
    "ParseModelClient",
    "ParsedActions",
    "load_repair_prompt",
    "parse_model_output",
]

_LOGGER = logging.getLogger(__name__)


class ActionType(StrEnum):
    """Canonical coracle action kinds."""

    TOOL_CALL = "tool_call"
    CODE_BLOCK = "code_block"
    FILE_WRITE = "file_write"
    MESSAGE_TO_USER = "message_to_user"
    PLAN_UPDATE = "plan_update"


_PAYLOAD_SCHEMAS: dict[ActionType, dict[str, Any]] = {
    ActionType.TOOL_CALL: {
        "type": "object",
        "required": ["name", "arguments"],
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "arguments": {"type": "object"},
        },
    },
    ActionType.CODE_BLOCK: {
        "type": "object",
        "required": ["language", "code"],
        "additionalProperties": False,
        "properties": {
            "language": {"type": "string"},
            "code": {"type": "string"},
        },
    },
    ActionType.FILE_WRITE: {
        "type": "object",
        "required": ["path", "content"],
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string"},
        },
    },
    ActionType.MESSAGE_TO_USER: {
        "type": "object",
        "required": ["text"],
        "additionalProperties": False,
        "properties": {"text": {"type": "string"}},
    },
    ActionType.PLAN_UPDATE: {
        "type": "object",
        "required": ["steps"],
        "additionalProperties": False,
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
        },
    },
}


_VALIDATORS: dict[ActionType, Draft202012Validator] = {
    kind: Draft202012Validator(schema) for kind, schema in _PAYLOAD_SCHEMAS.items()
}


class ActionItem(BaseModel):
    """A single, schema-validated action emitted by the parse step."""

    model_config = ConfigDict(frozen=True)

    type: ActionType
    payload: dict[str, Any]
    order: int = Field(ge=0)
    dependencies: list[int] = Field(default_factory=list)


class ParsedActions(BaseModel):
    """The final, validated bundle returned by :func:`parse_model_output`."""

    model_config = ConfigDict(frozen=True)

    actions: list[ActionItem] = Field(default_factory=list)


@runtime_checkable
class ParseModelClient(Protocol):
    """Minimal contract for the local reasoning model used for repair."""

    def complete(self, prompt: str) -> str:  # pragma: no cover - protocol
        ...


_FENCE_RE = re.compile(
    r"```(?P<lang>[A-Za-z0-9_+-]*)\s*\n(?P<body>.*?)```",
    re.DOTALL,
)
_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(?P<json>\{.*?\})\s*</tool_call>",
    re.DOTALL,
)


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_repair_prompt() -> str:
    """Return the bundled repair prompt template."""

    return (_PROMPTS_DIR / "parse_repair.md").read_text(encoding="utf-8")


def parse_model_output(
    raw: str,
    *,
    model_client: ParseModelClient | None = None,
) -> ParsedActions:
    """Parse ``raw`` big-model output into a :class:`ParsedActions` bundle.

    The function never raises: any structural failure is funnelled into
    a single ``message_to_user`` fallback so the pipeline can continue.
    """

    candidates = _run_strategies(raw)
    if candidates is None and model_client is not None:
        repaired = _repair(raw, model_client)
        if repaired is not None:
            candidates = _run_strategies(repaired)

    if not candidates:
        candidates = [_fallback_action(raw)]

    validated = list(_validate(candidates))
    if not validated:
        validated = [
            ActionItem(
                type=ActionType.MESSAGE_TO_USER,
                payload={"text": raw},
                order=0,
            )
        ]

    ordered = _normalise_order(validated)
    return ParsedActions(actions=ordered)


def _run_strategies(raw: str) -> list[dict[str, Any]] | None:
    for strategy in (_strategy_strict_json, _strategy_markdown_fences, _strategy_tool_call_regex):
        result = strategy(raw)
        if result:
            return result
    return None


def _strategy_strict_json(raw: str) -> list[dict[str, Any]] | None:
    text = raw.strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _coerce_actions(decoded)


def _strategy_markdown_fences(raw: str) -> list[dict[str, Any]] | None:
    collected: list[dict[str, Any]] = []
    for match in _FENCE_RE.finditer(raw):
        lang = match.group("lang").lower()
        body = match.group("body").strip()
        if lang in {"json", ""}:
            try:
                decoded = json.loads(body)
            except json.JSONDecodeError:
                continue
            actions = _coerce_actions(decoded)
            if actions:
                collected.extend(actions)
                continue
        if lang and lang != "json":
            collected.append(
                {
                    "type": ActionType.CODE_BLOCK.value,
                    "payload": {"language": lang, "code": body},
                }
            )
    return collected or None


def _strategy_tool_call_regex(raw: str) -> list[dict[str, Any]] | None:
    matches = list(_TOOL_CALL_RE.finditer(raw))
    if not matches:
        return None
    collected: list[dict[str, Any]] = []
    for match in matches:
        try:
            decoded = json.loads(match.group("json"))
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue
        name = decoded.get("name")
        arguments = decoded.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                continue
        if not isinstance(name, str) or not isinstance(arguments, dict):
            continue
        collected.append(
            {
                "type": ActionType.TOOL_CALL.value,
                "payload": {"name": name, "arguments": arguments},
            }
        )
    return collected or None


def _coerce_actions(decoded: Any) -> list[dict[str, Any]] | None:
    if isinstance(decoded, dict) and "actions" in decoded:
        actions = decoded["actions"]
    elif isinstance(decoded, list):
        actions = decoded
    elif isinstance(decoded, dict) and "type" in decoded and "payload" in decoded:
        actions = [decoded]
    else:
        return None
    if not isinstance(actions, list):
        return None
    return [a for a in actions if isinstance(a, dict)] or None


def _repair(raw: str, model_client: ParseModelClient) -> str | None:
    try:
        prompt = load_repair_prompt().replace("{{RAW}}", raw)
        return model_client.complete(prompt)
    except Exception as exc:
        _LOGGER.warning("parse.repair_failed", extra={"error": str(exc)})
        return None


def _validate(candidates: Iterable[dict[str, Any]]) -> Iterable[ActionItem]:
    for index, candidate in enumerate(candidates):
        try:
            kind = ActionType(candidate.get("type"))
        except ValueError:
            _LOGGER.warning(
                "parse.unknown_action_type",
                extra={"index": index, "value": candidate.get("type")},
            )
            continue
        payload = candidate.get("payload")
        if not isinstance(payload, dict):
            _LOGGER.warning("parse.payload_not_object", extra={"index": index})
            continue
        try:
            _VALIDATORS[kind].validate(payload)
        except ValidationError as exc:
            _LOGGER.warning(
                "parse.payload_invalid",
                extra={"index": index, "kind": kind.value, "error": exc.message},
            )
            continue
        order = candidate.get("order", index)
        if not isinstance(order, int) or order < 0:
            order = index
        deps_raw = candidate.get("dependencies", [])
        if not isinstance(deps_raw, list):
            deps_raw = []
        dependencies = [d for d in deps_raw if isinstance(d, int) and d >= 0]
        yield ActionItem(type=kind, payload=payload, order=order, dependencies=dependencies)


def _normalise_order(items: list[ActionItem]) -> list[ActionItem]:
    sorted_items = sorted(enumerate(items), key=lambda pair: (pair[1].order, pair[0]))
    return [
        ActionItem(
            type=item.type,
            payload=item.payload,
            order=new_order,
            dependencies=item.dependencies,
        )
        for new_order, (_, item) in enumerate(sorted_items)
    ]


def _fallback_action(raw: str) -> dict[str, Any]:
    return {
        "type": ActionType.MESSAGE_TO_USER.value,
        "payload": {"text": raw},
    }
