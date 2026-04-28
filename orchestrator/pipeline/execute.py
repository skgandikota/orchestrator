"""Pipeline ``execute`` step.

Runs exactly one :class:`ExecutableStep` against the local *coder* model
(``qwen2.5-coder:7b`` by default) using OpenAI-style function calling. Tool
schemas are sourced from a :class:`ToolRegistry` and dispatched through the
same registry; this module never imports concrete tool implementations.

Resource management contract
----------------------------
- A scheduler slot for the coder model is acquired **before** any LLM call
  and released **before** the function returns or raises (via a context
  manager + ``try/finally``).
- The step's status transitions ``pending -> running -> done|failed`` are
  each persisted to SQLite by the supplied :class:`StateWriter`. Both the
  ``running`` and the terminal write are first-class checkpoints.

Looping
-------
The execute loop iterates up to ``max_iterations`` times (default 8). Each
iteration sends the current message stack to the coder; if the model returns
``tool_calls`` they are dispatched through ``registry.invoke`` and the
results appended as ``role='tool'`` messages. The first response without
``tool_calls`` is treated as the final answer. Exceeding the cap is a
terminal failure (``IterationCapError``).

This module is intentionally unaware of the rest of the pipeline -- it sees
one step, a registry, a scheduler, a state writer and a model client.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger("orchestrator.pipeline.execute")


__all__ = [
    "CoderClient",
    "ExecutableStep",
    "ExecuteError",
    "IterationCapError",
    "Scheduler",
    "StateWriter",
    "StepStatus",
    "ToolRegistry",
    "execute",
]


CODER_MODEL_ID = "qwen2.5-coder:7b"
DEFAULT_MAX_ITERATIONS = 8

StepStatus = Literal["pending", "running", "done", "failed"]


class ExecutableStep(BaseModel):
    """A single step the executor runs.

    Defined inline so this module is decoupled from upstream planning code.
    Callers from the plan step can construct an :class:`ExecutableStep`
    structurally from a ``PlanStep``.
    """

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    expected_outcome: str = Field(..., min_length=1)
    status: StepStatus = "pending"
    output: Any | None = None


class ExecuteError(RuntimeError):
    """Base error class for the execute step."""


class IterationCapError(ExecuteError):
    """Raised when the per-step iteration cap is hit before a final answer."""

    def __init__(self, cap: int) -> None:
        super().__init__(f"iteration cap reached ({cap})")
        self.cap = cap


@runtime_checkable
class ToolRegistry(Protocol):
    """Minimal surface required from the tool registry."""

    def openai_tools(self) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        ...

    def invoke(self, name: str, args: Mapping[str, Any]) -> Any:  # pragma: no cover
        ...


@runtime_checkable
class Scheduler(Protocol):
    """The slice of the LLM-slot scheduler the executor depends on."""

    def acquire(self, model_id: str) -> AbstractContextManager[Any]:  # pragma: no cover - protocol
        ...


@runtime_checkable
class StateWriter(Protocol):
    """Persists step status checkpoints to SQLite (or a fake in tests)."""

    def update_step(self, step: ExecutableStep) -> None:  # pragma: no cover - protocol
        ...


@runtime_checkable
class CoderClient(Protocol):
    """Subset of the Ollama coder client used by the executor.

    ``chat`` must return a mapping with at least ``content`` (``str | None``)
    and ``tool_calls`` (a list of OpenAI-style tool call dicts, possibly
    empty). Implementations are tested via fakes; we never hit the network
    here.
    """

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Mapping[str, Any]:  # pragma: no cover - protocol
        ...


_SYSTEM_PROMPT = (
    "You are an execution agent. You will be given a single step with a goal "
    "and an expected outcome. Use the provided tools to accomplish the goal. "
    "When you are done, reply with a final answer in plain text and stop "
    "calling tools."
)


def _build_initial_messages(step: ExecutableStep) -> list[dict[str, Any]]:
    user_payload = {
        "step_id": step.id,
        "description": step.description,
        "expected_outcome": step.expected_outcome,
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload)},
    ]


def _coerce_args(raw: Any) -> dict[str, Any]:
    """Normalize OpenAI-style ``arguments`` (str JSON or dict) to a dict."""
    if isinstance(raw, str):
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        if isinstance(decoded, dict):
            return decoded
        return {"_raw": decoded}
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _extract_tool_calls(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract OpenAI-style tool calls from a chat response.

    Accepts both the top-level form (``response['tool_calls']``) and the
    ``response['message']['tool_calls']`` form used by some clients.
    """
    if response.get("tool_calls"):
        return list(response["tool_calls"])
    message = response.get("message")
    if isinstance(message, Mapping) and message.get("tool_calls"):
        return list(message["tool_calls"])
    return []


def _extract_content(response: Mapping[str, Any]) -> str | None:
    if response.get("content") is not None:
        return str(response["content"])
    message = response.get("message")
    if isinstance(message, Mapping) and message.get("content") is not None:
        return str(message["content"])
    return None


def _tool_call_id(call: Mapping[str, Any], fallback_index: int) -> str:
    tcid = call.get("id")
    if isinstance(tcid, str) and tcid:
        return tcid
    return f"call_{fallback_index}"


def _tool_name_and_args(call: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    function = call.get("function")
    if isinstance(function, Mapping):
        name = str(function.get("name") or "")
        args = _coerce_args(function.get("arguments"))
    else:
        name = str(call.get("name") or "")
        args = _coerce_args(call.get("arguments"))
    return name, args


def execute(
    step: ExecutableStep,
    *,
    scheduler: Scheduler,
    registry: ToolRegistry,
    state: StateWriter,
    ollama: CoderClient,
    model_id: str = CODER_MODEL_ID,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> ExecutableStep:
    """Run ``step`` to completion and return the updated step.

    The returned step is the same instance with ``status`` set to either
    ``"done"`` (with ``output`` populated) or ``"failed"`` (with ``output``
    holding a structured error payload).

    Failure modes are *captured*, not raised, with one exception: programmer
    errors from outside the LLM/tool flow (e.g. the scheduler raising before
    a slot is held) propagate after the step has been checkpointed as
    failed and the slot has been released.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    step.status = "running"
    step.output = None
    state.update_step(step)
    logger.info("execute.start", step_id=step.id, model=model_id)

    try:
        with scheduler.acquire(model_id):
            messages = _build_initial_messages(step)
            tools = registry.openai_tools()

            for iteration in range(1, max_iterations + 1):
                response = ollama.chat(
                    model=model_id,
                    messages=messages,
                    tools=tools,
                )
                tool_calls = _extract_tool_calls(response)
                content = _extract_content(response)

                if not tool_calls:
                    step.status = "done"
                    step.output = content if content is not None else ""
                    logger.info(
                        "execute.done",
                        step_id=step.id,
                        iterations=iteration,
                    )
                    return step

                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": list(tool_calls),
                }
                messages.append(assistant_msg)

                for index, call in enumerate(tool_calls):
                    name, args = _tool_name_and_args(call)
                    call_id = _tool_call_id(call, index)
                    try:
                        result = registry.invoke(name, args)
                    except Exception as exc:
                        error_payload = {
                            "tool": name,
                            "args": args,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        step.status = "failed"
                        step.output = error_payload
                        logger.warning(
                            "execute.tool_failed",
                            step_id=step.id,
                            tool=name,
                            error=str(exc),
                        )
                        return step

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": name,
                            "content": json.dumps(result, default=str),
                        }
                    )

            step.status = "failed"
            step.output = {
                "tool": None,
                "args": {},
                "error": "iteration cap reached",
            }
            logger.warning(
                "execute.iteration_cap",
                step_id=step.id,
                cap=max_iterations,
            )
            return step

    except Exception as exc:
        step.status = "failed"
        step.output = {
            "tool": None,
            "args": {},
            "error": f"{type(exc).__name__}: {exc}",
        }
        logger.exception("execute.exception", step_id=step.id)
        raise
    finally:
        # The scheduler context manager releases the slot on every exit
        # path; this ``finally`` exists to guarantee the *checkpoint* is
        # written before the function unwinds, even on an exception that
        # short-circuited the ``return`` above.
        if step.status == "running":
            step.status = "failed"
            step.output = step.output or {
                "tool": None,
                "args": {},
                "error": "execution interrupted",
            }
        state.update_step(step)
