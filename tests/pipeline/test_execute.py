"""Tests for the pipeline ``execute`` step."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from orchestrator.pipeline import (
    CoderClient,
    ExecutableStep,
    ExecuteError,
    IterationCapError,
    Scheduler,
    StateWriter,
    ToolRegistry,
    execute,
)
from orchestrator.pipeline.execute import (
    CODER_MODEL_ID,
    DEFAULT_MAX_ITERATIONS,
    _coerce_args,
    _extract_content,
    _extract_tool_calls,
)

# --- Fakes -----------------------------------------------------------------


class FakeRegistry:
    def __init__(self, *, tool_results: dict[str, Any] | None = None) -> None:
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo back the input.",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "explode",
                    "description": "Always raises.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        self._tool_results = tool_results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def openai_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    def invoke(self, name: str, args: Mapping[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        if name == "explode":
            raise RuntimeError("boom")
        if name in self._tool_results:
            return self._tool_results[name]
        return {"echoed": args}


class FakeScheduler:
    def __init__(self, *, raise_on_acquire: BaseException | None = None) -> None:
        self.acquired: list[str] = []
        self.released: list[str] = []
        self._raise_on_acquire = raise_on_acquire

    @contextmanager
    def acquire(self, model_id: str) -> Iterator[None]:
        if self._raise_on_acquire is not None:
            raise self._raise_on_acquire
        self.acquired.append(model_id)
        try:
            yield
        finally:
            self.released.append(model_id)


class FakeState:
    def __init__(self) -> None:
        self.writes: list[tuple[str, Any]] = []

    def update_step(self, step: ExecutableStep) -> None:
        self.writes.append((step.status, step.output))


class FakeCoder:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Mapping[str, Any]:
        self.calls.append(
            {"model": model, "messages": list(messages), "tools": list(tools)}
        )
        if not self._responses:
            raise AssertionError("FakeCoder ran out of canned responses")
        return self._responses.pop(0)


# --- Helpers ---------------------------------------------------------------


def _make_step(step_id: str = "s1") -> ExecutableStep:
    return ExecutableStep(
        id=step_id,
        description="Do the thing",
        expected_outcome="The thing is done",
    )


def _tool_call(name: str, args: dict[str, Any], *, call_id: str | None = None) -> dict[str, Any]:
    return {
        "id": call_id or f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# --- Protocol smoke --------------------------------------------------------


def test_protocols_are_runtime_checkable() -> None:
    assert isinstance(FakeRegistry(), ToolRegistry)
    assert isinstance(FakeScheduler(), Scheduler)
    assert isinstance(FakeState(), StateWriter)
    assert isinstance(FakeCoder([]), CoderClient)


# --- Happy paths -----------------------------------------------------------


def test_happy_path_no_tool_calls() -> None:
    step = _make_step()
    coder = FakeCoder([{"content": "all done", "tool_calls": []}])
    scheduler = FakeScheduler()
    registry = FakeRegistry()
    state = FakeState()

    result = execute(
        step,
        scheduler=scheduler,
        registry=registry,
        state=state,
        ollama=coder,
    )

    assert result.status == "done"
    assert result.output == "all done"
    assert scheduler.acquired == [CODER_MODEL_ID]
    assert scheduler.released == [CODER_MODEL_ID]
    assert [w[0] for w in state.writes] == ["running", "done"]
    assert coder.calls[0]["model"] == CODER_MODEL_ID
    assert coder.calls[0]["tools"] == registry.openai_tools()


def test_happy_path_one_tool_call() -> None:
    step = _make_step()
    coder = FakeCoder(
        [
            {
                "content": "thinking",
                "tool_calls": [_tool_call("echo", {"text": "hi"})],
            },
            {"content": "final answer", "tool_calls": []},
        ]
    )
    scheduler = FakeScheduler()
    registry = FakeRegistry()
    state = FakeState()

    result = execute(
        step,
        scheduler=scheduler,
        registry=registry,
        state=state,
        ollama=coder,
    )

    assert result.status == "done"
    assert result.output == "final answer"
    assert registry.calls == [("echo", {"text": "hi"})]
    # Second chat call should include the assistant + tool messages.
    second_messages = coder.calls[1]["messages"]
    roles = [m["role"] for m in second_messages]
    assert roles[-2:] == ["assistant", "tool"]
    assert second_messages[-1]["name"] == "echo"


def test_message_form_response_is_supported() -> None:
    step = _make_step()
    # Some Ollama clients return content/tool_calls under a "message" key.
    coder = FakeCoder(
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [_tool_call("echo", {"text": "hi"})],
                }
            },
            {"message": {"content": "ok", "tool_calls": []}},
        ]
    )
    state = FakeState()
    result = execute(
        step,
        scheduler=FakeScheduler(),
        registry=FakeRegistry(),
        state=state,
        ollama=coder,
    )
    assert result.status == "done"
    assert result.output == "ok"


# --- Multi-iteration -------------------------------------------------------


def test_multi_iteration_tool_loop() -> None:
    step = _make_step()
    coder = FakeCoder(
        [
            {"content": None, "tool_calls": [_tool_call("echo", {"text": "a"})]},
            {"content": None, "tool_calls": [_tool_call("echo", {"text": "b"})]},
            {"content": None, "tool_calls": [_tool_call("echo", {"text": "c"})]},
            {"content": "done", "tool_calls": []},
        ]
    )
    registry = FakeRegistry()
    state = FakeState()

    result = execute(
        step,
        scheduler=FakeScheduler(),
        registry=registry,
        state=state,
        ollama=coder,
    )

    assert result.status == "done"
    assert [c[0] for c in registry.calls] == ["echo", "echo", "echo"]
    assert len(coder.calls) == 4


# --- Iteration cap ---------------------------------------------------------


def test_iteration_cap_hit() -> None:
    step = _make_step()
    # Always return a tool call to force the cap.
    cap = 3
    responses = [
        {"content": None, "tool_calls": [_tool_call("echo", {"text": "x"})]}
        for _ in range(cap)
    ]
    coder = FakeCoder(responses)
    scheduler = FakeScheduler()
    registry = FakeRegistry()
    state = FakeState()

    result = execute(
        step,
        scheduler=scheduler,
        registry=registry,
        state=state,
        ollama=coder,
        max_iterations=cap,
    )

    assert result.status == "failed"
    assert isinstance(result.output, dict)
    assert result.output["error"] == "iteration cap reached"
    # Slot must still be released.
    assert scheduler.released == [CODER_MODEL_ID]
    assert state.writes[-1][0] == "failed"


def test_iteration_cap_default() -> None:
    assert DEFAULT_MAX_ITERATIONS == 8


def test_max_iterations_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_iterations"):
        execute(
            _make_step(),
            scheduler=FakeScheduler(),
            registry=FakeRegistry(),
            state=FakeState(),
            ollama=FakeCoder([]),
            max_iterations=0,
        )


# --- Tool failure surfaced -------------------------------------------------


def test_tool_failure_is_captured_as_structured_output() -> None:
    step = _make_step()
    coder = FakeCoder(
        [{"content": None, "tool_calls": [_tool_call("explode", {"x": 1})]}]
    )
    scheduler = FakeScheduler()
    state = FakeState()

    result = execute(
        step,
        scheduler=scheduler,
        registry=FakeRegistry(),
        state=state,
        ollama=coder,
    )

    assert result.status == "failed"
    assert isinstance(result.output, dict)
    assert result.output == {
        "tool": "explode",
        "args": {"x": 1},
        "error": "RuntimeError: boom",
    }
    # Slot still released, terminal checkpoint written.
    assert scheduler.released == [CODER_MODEL_ID]
    assert [w[0] for w in state.writes] == ["running", "failed"]


# --- Slot released on exception --------------------------------------------


class _BoomError(RuntimeError):
    pass


def test_slot_released_on_exception_inside_loop() -> None:
    step = _make_step()

    class ExplodingCoder:
        def chat(
            self,
            *,
            model: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
        ) -> Mapping[str, Any]:
            raise _BoomError("network down")

    scheduler = FakeScheduler()
    state = FakeState()

    with pytest.raises(_BoomError):
        execute(
            step,
            scheduler=scheduler,
            registry=FakeRegistry(),
            state=state,
            ollama=ExplodingCoder(),
        )

    # Even on an unhandled exception, the slot is released and the step is
    # checkpointed as failed.
    assert scheduler.acquired == [CODER_MODEL_ID]
    assert scheduler.released == [CODER_MODEL_ID]
    assert step.status == "failed"
    assert isinstance(step.output, dict)
    assert "_BoomError" in step.output["error"]
    assert state.writes[-1][0] == "failed"


def test_scheduler_acquire_failure_still_checkpoints() -> None:
    step = _make_step()
    scheduler = FakeScheduler(raise_on_acquire=RuntimeError("no slot"))
    state = FakeState()

    with pytest.raises(RuntimeError, match="no slot"):
        execute(
            step,
            scheduler=scheduler,
            registry=FakeRegistry(),
            state=state,
            ollama=FakeCoder([]),
        )

    # Two writes: initial 'running' and final 'failed'.
    assert [w[0] for w in state.writes] == ["running", "failed"]
    assert step.status == "failed"


# --- Edge-case helpers -----------------------------------------------------


def test_coerce_args_variants() -> None:
    assert _coerce_args('{"a": 1}') == {"a": 1}
    assert _coerce_args("") == {}
    assert _coerce_args("not-json") == {"_raw": "not-json"}
    assert _coerce_args("[1,2]") == {"_raw": [1, 2]}
    assert _coerce_args({"a": 1}) == {"a": 1}
    assert _coerce_args(None) == {}


def test_extract_helpers() -> None:
    assert _extract_tool_calls({"tool_calls": [{"id": "x"}]}) == [{"id": "x"}]
    assert _extract_tool_calls({"message": {"tool_calls": [{"id": "y"}]}}) == [
        {"id": "y"}
    ]
    assert _extract_tool_calls({}) == []
    assert _extract_content({"content": "hi"}) == "hi"
    assert _extract_content({"message": {"content": "hi"}}) == "hi"
    assert _extract_content({}) is None


def test_tool_call_without_function_block() -> None:
    step = _make_step()
    # Some clients put name/arguments at the top level of the call.
    coder = FakeCoder(
        [
            {
                "content": None,
                "tool_calls": [
                    {"name": "echo", "arguments": {"text": "flat"}}
                ],
            },
            {"content": "ok", "tool_calls": []},
        ]
    )
    registry = FakeRegistry()
    result = execute(
        step,
        scheduler=FakeScheduler(),
        registry=registry,
        state=FakeState(),
        ollama=coder,
    )
    assert result.status == "done"
    assert registry.calls == [("echo", {"text": "flat"})]


def test_tool_call_id_falls_back_to_index() -> None:
    step = _make_step()
    coder = FakeCoder(
        [
            {
                "content": None,
                "tool_calls": [
                    {"function": {"name": "echo", "arguments": '{"text": "no-id"}'}}
                ],
            },
            {"content": "fin", "tool_calls": []},
        ]
    )
    state = FakeState()
    execute(
        step,
        scheduler=FakeScheduler(),
        registry=FakeRegistry(),
        state=state,
        ollama=coder,
    )
    second_messages = coder.calls[1]["messages"]
    tool_msg = next(m for m in second_messages if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_0"


def test_iteration_cap_error_class() -> None:
    err = IterationCapError(5)
    assert err.cap == 5
    assert isinstance(err, ExecuteError)
    assert "5" in str(err)


def test_custom_model_id_threaded_through() -> None:
    step = _make_step()
    coder = FakeCoder([{"content": "ok", "tool_calls": []}])
    scheduler = FakeScheduler()
    execute(
        step,
        scheduler=scheduler,
        registry=FakeRegistry(),
        state=FakeState(),
        ollama=coder,
        model_id="custom-coder:1b",
    )
    assert scheduler.acquired == ["custom-coder:1b"]
    assert coder.calls[0]["model"] == "custom-coder:1b"


def test_base_exception_still_checkpoints_failed() -> None:
    """A BaseException (e.g. KeyboardInterrupt) bypasses the Exception
    handler but the ``finally`` block must still mark the step failed."""
    step = _make_step()

    class CancellingCoder:
        def chat(
            self,
            *,
            model: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
        ) -> Mapping[str, Any]:
            raise KeyboardInterrupt

    scheduler = FakeScheduler()
    state = FakeState()

    with pytest.raises(KeyboardInterrupt):
        execute(
            step,
            scheduler=scheduler,
            registry=FakeRegistry(),
            state=state,
            ollama=CancellingCoder(),
        )

    assert step.status == "failed"
    assert step.output == {
        "tool": None,
        "args": {},
        "error": "execution interrupted",
    }
    assert scheduler.released == [CODER_MODEL_ID]
    assert state.writes[-1][0] == "failed"
