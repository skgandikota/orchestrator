"""Tests for the intent classifier (#37)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from coracle.core.classifier import (
    ClassifyResult,
    OllamaClient,
    StateRecorder,
    classify,
)

# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeOllama:
    """Returns canned payloads in order; raises if asked beyond what is queued."""

    def __init__(self, responses: Iterable[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def structured(self, *, model: str, schema: type[BaseModel], prompt: str) -> Any:
        self.calls.append({"model": model, "schema": schema, "prompt": prompt})
        if not self._responses:
            raise AssertionError("FakeOllama exhausted")
        return self._responses.pop(0)


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, ClassifyResult]] = []

    def record_classification(self, user_msg: str, result: ClassifyResult) -> None:
        self.events.append((user_msg, result))


def _payload(class_: str, confidence: float, reason: str = "ok") -> str:
    return json.dumps({"class": class_, "confidence": confidence, "reason": reason})


# --------------------------------------------------------------------------- #
# Pydantic model
# --------------------------------------------------------------------------- #


def test_classify_result_aliases_class() -> None:
    obj = ClassifyResult.model_validate({"class": "fast", "confidence": 0.9, "reason": "hi"})
    assert obj.class_ == "fast"
    dumped = obj.model_dump(by_alias=True)
    assert dumped["class"] == "fast"
    assert "class_" not in dumped


def test_classify_result_populate_by_name() -> None:
    obj = ClassifyResult(class_="deep", confidence=0.5, reason="why")
    assert obj.class_ == "deep"


def test_classify_result_rejects_bad_class() -> None:
    with pytest.raises(ValidationError):
        ClassifyResult.model_validate({"class": "weird", "confidence": 0.1, "reason": "x"})


def test_classify_result_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValidationError):
        ClassifyResult.model_validate({"class": "fast", "confidence": 1.5, "reason": "x"})


# --------------------------------------------------------------------------- #
# Regex pre-filter
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "msg",
    [
        "status",
        "  Status please",
        "What's happening?",
        "whats happening with my job",
        "Progress on the build?",
        "where are we on this",
    ],
)
@pytest.mark.asyncio
async def test_regex_prefilter_returns_status_without_calling_ollama(
    msg: str,
) -> None:
    ollama = FakeOllama([])  # would raise if called
    result = await classify(msg, ollama=ollama)  # type: ignore[arg-type]
    assert result.class_ == "status"
    assert result.confidence == 1.0
    assert result.reason == "regex pre-filter"
    assert ollama.calls == []


@pytest.mark.asyncio
async def test_regex_prefilter_records_to_state() -> None:
    rec = FakeRecorder()
    ollama = FakeOllama([])
    await classify(
        "status now",
        ollama=ollama,  # type: ignore[arg-type]
        recorder=rec,  # type: ignore[arg-type]
    )
    assert len(rec.events) == 1
    assert rec.events[0][1].class_ == "status"


# --------------------------------------------------------------------------- #
# Happy-path model invocation per class
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("class_name", ["fast", "deep", "research", "status"])
@pytest.mark.asyncio
async def test_model_path_returns_each_class(class_name: str) -> None:
    ollama = FakeOllama([_payload(class_name, 0.87, f"{class_name} reason")])
    result = await classify("please help me with X", ollama=ollama)  # type: ignore[arg-type]
    assert result.class_ == class_name
    assert result.confidence == pytest.approx(0.87)
    assert result.reason == f"{class_name} reason"
    assert len(ollama.calls) == 1
    call = ollama.calls[0]
    assert call["model"] == "qwen2.5:7b"
    assert call["schema"] is ClassifyResult
    assert "please help me with X" in call["prompt"]


@pytest.mark.asyncio
async def test_model_path_accepts_dict_payload() -> None:
    ollama = FakeOllama([{"class": "fast", "confidence": 0.5, "reason": "ok"}])
    result = await classify("anything", ollama=ollama)  # type: ignore[arg-type]
    assert result.class_ == "fast"


@pytest.mark.asyncio
async def test_model_path_accepts_bytes_payload() -> None:
    ollama = FakeOllama([_payload("research", 0.7).encode("utf-8")])
    result = await classify("dig deeper", ollama=ollama)  # type: ignore[arg-type]
    assert result.class_ == "research"


@pytest.mark.asyncio
async def test_model_path_accepts_already_validated_result() -> None:
    canned = ClassifyResult(class_="deep", confidence=0.42, reason="pre-built")
    ollama = FakeOllama([canned])
    result = await classify("anything", ollama=ollama)  # type: ignore[arg-type]
    assert result is canned


# --------------------------------------------------------------------------- #
# Retry / fallback
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_malformed_json_retries_then_succeeds() -> None:
    ollama = FakeOllama(["not json{{{", _payload("fast", 0.6)])
    result = await classify("ambiguous", ollama=ollama)  # type: ignore[arg-type]
    assert result.class_ == "fast"
    assert len(ollama.calls) == 2


@pytest.mark.asyncio
async def test_validation_error_retries_then_succeeds() -> None:
    bad = json.dumps({"class": "wat", "confidence": 0.5, "reason": "x"})
    ollama = FakeOllama([bad, _payload("research", 0.9)])
    result = await classify("ambiguous", ollama=ollama)  # type: ignore[arg-type]
    assert result.class_ == "research"
    assert len(ollama.calls) == 2


@pytest.mark.asyncio
async def test_two_failures_falls_back_to_deep() -> None:
    ollama = FakeOllama(["broken", "still broken"])
    result = await classify("ambiguous", ollama=ollama)  # type: ignore[arg-type]
    assert result.class_ == "deep"
    assert result.confidence == 0.0
    assert result.reason == "classifier fallback"
    assert len(ollama.calls) == 2


@pytest.mark.asyncio
async def test_unexpected_payload_type_falls_back() -> None:
    ollama = FakeOllama([12345, 67890])  # neither str/bytes/dict/result
    result = await classify("ambiguous", ollama=ollama)  # type: ignore[arg-type]
    assert result.class_ == "deep"
    assert result.reason == "classifier fallback"


# --------------------------------------------------------------------------- #
# State recorder always sees the final decision
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recorder_called_once_on_model_path() -> None:
    rec = FakeRecorder()
    ollama = FakeOllama([_payload("deep", 0.8)])
    await classify(
        "refactor this",
        ollama=ollama,
        recorder=rec,  # type: ignore[arg-type]
    )
    assert len(rec.events) == 1
    msg, result = rec.events[0]
    assert msg == "refactor this"
    assert result.class_ == "deep"


@pytest.mark.asyncio
async def test_recorder_called_once_on_fallback() -> None:
    rec = FakeRecorder()
    ollama = FakeOllama(["nope", "still nope"])
    await classify(
        "ambiguous",
        ollama=ollama,  # type: ignore[arg-type]
        recorder=rec,  # type: ignore[arg-type]
    )
    assert len(rec.events) == 1
    assert rec.events[0][1].reason == "classifier fallback"


# --------------------------------------------------------------------------- #
# Protocol exports remain importable (smoke test for re-export surface)
# --------------------------------------------------------------------------- #


def test_protocols_are_importable() -> None:
    assert OllamaClient is not None
    assert StateRecorder is not None


def test_classify_reexport_from_core() -> None:
    from coracle.core import ClassifyResult as Re_Result
    from coracle.core import classify as re_classify

    assert Re_Result is ClassifyResult
    assert re_classify is classify
