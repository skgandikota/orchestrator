"""Tests for :mod:`orchestrator.pipeline.parse`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from orchestrator.pipeline import (
    ActionItem,
    ActionType,
    ParseModelClient,
    ParsedActions,
    load_repair_prompt,
    parse_model_output,
)
from orchestrator.pipeline import parse as parse_module


class FakeModel:
    """Deterministic stand-in implementing the :class:`ParseModelClient` Protocol."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class ExplodingModel:
    def complete(self, prompt: str) -> str:
        del prompt
        raise RuntimeError("boom")


def test_ParseModelClient_protocol_is_runtime_checkable() -> None:
    assert isinstance(FakeModel("x"), ParseModelClient)
    assert not isinstance(object(), ParseModelClient)


def test_load_repair_prompt_contains_placeholder() -> None:
    text = load_repair_prompt()
    assert "{{RAW}}" in text


def test_strict_json_object_with_actions_key() -> None:
    raw = json.dumps(
        {
            "actions": [
                {
                    "type": "tool_call",
                    "payload": {"name": "shell", "arguments": {"cmd": "ls"}},
                    "order": 1,
                    "dependencies": [0],
                },
                {
                    "type": "message_to_user",
                    "payload": {"text": "done"},
                    "order": 0,
                },
            ]
        }
    )
    result = parse_model_output(raw)
    assert isinstance(result, ParsedActions)
    assert [a.type for a in result.actions] == [
        ActionType.MESSAGE_TO_USER,
        ActionType.TOOL_CALL,
    ]
    # order is normalised to a 0..n-1 sequence preserving sort key
    assert [a.order for a in result.actions] == [0, 1]
    assert result.actions[1].dependencies == [0]


def test_strict_json_top_level_list() -> None:
    raw = json.dumps([{"type": "message_to_user", "payload": {"text": "hi"}}])
    result = parse_model_output(raw)
    assert len(result.actions) == 1
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER


def test_strict_json_single_action_object() -> None:
    raw = json.dumps({"type": "message_to_user", "payload": {"text": "hi"}})
    result = parse_model_output(raw)
    assert result.actions[0].payload == {"text": "hi"}


def test_markdown_fenced_json_block() -> None:
    raw = """Here is the plan:

```json
{"actions": [{"type": "file_write", "payload": {"path": "a.txt", "content": "x"}}]}
```

That's it.
"""
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.FILE_WRITE


def test_markdown_fenced_code_block_becomes_code_action() -> None:
    raw = """```python
print('hi')
```"""
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.CODE_BLOCK
    assert result.actions[0].payload == {"language": "python", "code": "print('hi')"}


def test_markdown_invalid_json_block_falls_through() -> None:
    raw = "```json\n{not valid json}\n```"
    # No valid JSON, no other strategy hits, no model -> fallback to message_to_user.
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER
    assert result.actions[0].payload["text"] == raw


def test_tool_call_regex_strategy() -> None:
    raw = (
        "preamble\n"
        '<tool_call>{"name": "shell", "arguments": {"cmd": "ls"}}</tool_call>\n'
        '<tool_call>{"name": "fs.read", "arguments": "{\\"path\\": \\"x\\"}"}</tool_call>\n'
        "tail"
    )
    result = parse_model_output(raw)
    assert [a.type for a in result.actions] == [ActionType.TOOL_CALL, ActionType.TOOL_CALL]
    assert result.actions[1].payload == {"name": "fs.read", "arguments": {"path": "x"}}


def test_tool_call_regex_skips_invalid_json() -> None:
    raw = (
        "<tool_call>{not json}</tool_call>"
        '<tool_call>{"name": "shell", "arguments": {"cmd": "ls"}}</tool_call>'
    )
    result = parse_model_output(raw)
    assert len(result.actions) == 1
    assert result.actions[0].payload["name"] == "shell"


def test_tool_call_regex_skips_non_dict_decoded() -> None:
    raw = "<tool_call>[1, 2, 3]</tool_call>"
    result = parse_model_output(raw)
    # No tool_call hits; falls back to message_to_user.
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER


def test_tool_call_regex_skips_bad_arg_string() -> None:
    raw = '<tool_call>{"name": "shell", "arguments": "not json"}</tool_call>'
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER


def test_tool_call_regex_requires_string_name_and_dict_args() -> None:
    raw = (
        '<tool_call>{"name": 5, "arguments": {}}</tool_call>'
        '<tool_call>{"name": "x", "arguments": 5}</tool_call>'
    )
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER


def test_fallback_when_empty_input() -> None:
    result = parse_model_output("")
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER
    assert result.actions[0].payload == {"text": ""}


def test_fallback_for_unstructured_text() -> None:
    raw = "just some prose, nothing to parse"
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER
    assert result.actions[0].payload["text"] == raw


def test_repair_invoked_when_json_invalid_and_succeeds() -> None:
    bad = "{this is broken"
    repaired = json.dumps({"actions": [{"type": "message_to_user", "payload": {"text": "fixed"}}]})
    model = FakeModel(repaired)
    result = parse_model_output(bad, model_client=model)
    assert model.prompts, "model should have been called for repair"
    assert "{this is broken" in model.prompts[0]
    assert result.actions[0].payload == {"text": "fixed"}


def test_repair_failure_falls_back_to_message() -> None:
    result = parse_model_output("garbage", model_client=ExplodingModel())
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER


def test_repair_returns_unparseable_text() -> None:
    model = FakeModel("still not json")
    result = parse_model_output("garbage", model_client=model)
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER


def test_invalid_actions_are_dropped_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    raw = json.dumps(
        {
            "actions": [
                {"type": "tool_call", "payload": {"name": "shell"}},  # missing arguments
                {"type": "message_to_user", "payload": {"text": "ok"}},
                {"type": "bogus", "payload": {}},
                {"type": "message_to_user", "payload": "not an object"},
                "not even a dict",
            ]
        }
    )
    with caplog.at_level("WARNING", logger="orchestrator.pipeline.parse"):
        result = parse_model_output(raw)
    assert [a.type for a in result.actions] == [ActionType.MESSAGE_TO_USER]
    messages = " ".join(rec.message for rec in caplog.records)
    assert "parse.payload_invalid" in messages
    assert "parse.unknown_action_type" in messages
    assert "parse.payload_not_object" in messages


def test_all_actions_invalid_yields_fallback() -> None:
    raw = json.dumps({"actions": [{"type": "bogus", "payload": {}}]})
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER


def test_negative_or_non_int_order_resets_to_index() -> None:
    raw = json.dumps(
        {
            "actions": [
                {"type": "message_to_user", "payload": {"text": "a"}, "order": -3},
                {"type": "message_to_user", "payload": {"text": "b"}, "order": "bad"},
            ]
        }
    )
    result = parse_model_output(raw)
    assert [a.payload["text"] for a in result.actions] == ["a", "b"]
    assert [a.order for a in result.actions] == [0, 1]


def test_dependencies_filtered_and_non_list_ignored() -> None:
    raw = json.dumps(
        {
            "actions": [
                {
                    "type": "message_to_user",
                    "payload": {"text": "a"},
                    "dependencies": [0, -1, "bad", 2],
                },
                {
                    "type": "message_to_user",
                    "payload": {"text": "b"},
                    "dependencies": "nope",
                },
            ]
        }
    )
    result = parse_model_output(raw)
    assert result.actions[0].dependencies == [0, 2]
    assert result.actions[1].dependencies == []


def test_coerce_actions_rejects_non_list_under_actions_key() -> None:
    raw = json.dumps({"actions": "not a list"})
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER


def test_coerce_actions_rejects_unknown_top_level_shape() -> None:
    raw = json.dumps({"something": "else"})
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER


def test_actions_list_filters_non_dict_entries() -> None:
    raw = json.dumps(
        [
            "string-not-dict",
            42,
            {"type": "message_to_user", "payload": {"text": "kept"}},
        ]
    )
    result = parse_model_output(raw)
    assert len(result.actions) == 1
    assert result.actions[0].payload["text"] == "kept"


def test_action_item_immutable() -> None:
    item = ActionItem(type=ActionType.MESSAGE_TO_USER, payload={"text": "x"}, order=0)
    with pytest.raises(ValueError):
        item.order = 5  # type: ignore[misc]


def test_run_strategies_returns_none_for_empty_string() -> None:
    # Direct private hit covers the early exit when the strict-JSON strategy
    # cannot do anything with whitespace-only input.
    assert parse_module._run_strategies("   ") is None


def test_validate_skips_when_payload_missing() -> None:
    candidates: list[dict[str, Any]] = [{"type": "message_to_user"}]
    items = list(parse_module._validate(candidates))
    assert items == []


def test_strict_json_actions_list_with_only_invalid_returns_none() -> None:
    raw = json.dumps({"actions": [1, 2, 3]})
    # Coercion drops all entries; strategies fall through to fallback.
    result = parse_model_output(raw)
    assert result.actions[0].type is ActionType.MESSAGE_TO_USER
