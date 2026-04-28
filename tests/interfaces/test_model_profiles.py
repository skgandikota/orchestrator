"""Tests for the model profile registry and OpenAI-compat resolver."""

from __future__ import annotations

import pytest

from coracle.interfaces.model_profiles import (
    DEFAULT_PROFILE_NAME,
    ModelProfile,
    ProfileNotFoundError,
    list_profiles,
    resolve_profile,
)
from coracle.interfaces.openai_compat import (
    JobRow,
    list_models_response,
    submit_job,
)


def test_default_profile_is_coracle_with_no_force_class() -> None:
    profile = resolve_profile("coracle")
    assert profile.name == "coracle"
    assert profile.force_class is None


@pytest.mark.parametrize(
    ("name", "force_class"),
    [
        ("coracle-fast", "fast"),
        ("coracle-deep", "deep"),
        ("coracle-research", "research"),
        ("coracle-status", "status"),
    ],
)
def test_override_profiles_force_their_class(name: str, force_class: str) -> None:
    profile = resolve_profile(name)
    assert profile.force_class == force_class
    assert profile.description


def test_resolve_profile_unknown_name_raises_404() -> None:
    with pytest.raises(ProfileNotFoundError) as excinfo:
        resolve_profile("gpt-4")
    err = excinfo.value
    assert err.status_code == 404
    assert err.name == "gpt-4"
    assert "gpt-4" in str(err)
    # Subclasses KeyError so dict-style handlers still match.
    assert isinstance(err, KeyError)


def test_list_profiles_contains_all_five_with_coracle_first() -> None:
    profiles = list_profiles()
    names = [p.name for p in profiles]
    assert names[0] == DEFAULT_PROFILE_NAME
    assert set(names) == {
        "coracle",
        "coracle-fast",
        "coracle-deep",
        "coracle-research",
        "coracle-status",
    }


def test_model_profile_is_immutable() -> None:
    profile = resolve_profile("coracle")
    with pytest.raises(AttributeError):
        profile.name = "tampered"  # type: ignore[misc]


def test_list_models_response_has_openai_list_shape() -> None:
    payload = list_models_response(now=lambda: 1700000000.5)
    assert payload["object"] == "list"
    data = payload["data"]
    assert isinstance(data, list)
    assert len(data) == 5
    first = data[0]
    assert first["id"] == "coracle"
    assert first["object"] == "model"
    assert first["created"] == 1700000000
    assert first["owned_by"] == "coracle"
    assert first["force_class"] is None
    # Every override profile carries its force_class in the payload.
    by_id = {entry["id"]: entry for entry in data}
    assert by_id["coracle-fast"]["force_class"] == "fast"
    assert by_id["coracle-deep"]["force_class"] == "deep"
    assert by_id["coracle-research"]["force_class"] == "research"
    assert by_id["coracle-status"]["force_class"] == "status"


def test_list_models_response_uses_default_clock_when_none_provided() -> None:
    payload = list_models_response()
    assert isinstance(payload["data"], list)
    assert payload["data"][0]["id"] == "coracle"


def test_list_models_response_puts_default_first_even_if_registry_reshuffled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coracle.interfaces import model_profiles as mp

    reordered = {
        "coracle-fast": mp._REGISTRY["coracle-fast"],
        "coracle": mp._REGISTRY["coracle"],
        "coracle-deep": mp._REGISTRY["coracle-deep"],
        "coracle-research": mp._REGISTRY["coracle-research"],
        "coracle-status": mp._REGISTRY["coracle-status"],
    }
    monkeypatch.setattr(mp, "_REGISTRY", reordered)
    payload = list_models_response(now=lambda: 0.0)
    assert payload["data"][0]["id"] == "coracle"


def test_submit_job_with_default_profile_invokes_classifier() -> None:
    classifier_calls: list[None] = []

    def classifier() -> str:
        classifier_calls.append(None)
        return "deep"

    submitted: list[tuple[str, str | None]] = []

    def submitter(model: str, force_class: str | None) -> str:
        submitted.append((model, force_class))
        return "job-1"

    row = submit_job("coracle", submitter=submitter, classifier=classifier)

    assert classifier_calls == [None]
    assert submitted == [("coracle", "deep")]
    assert row == JobRow(job_id="job-1", model="coracle", force_class="deep")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("coracle-fast", "fast"),
        ("coracle-deep", "deep"),
        ("coracle-research", "research"),
        ("coracle-status", "status"),
    ],
)
def test_submit_job_with_override_skips_classifier(name: str, expected: str) -> None:
    def classifier() -> str:  # pragma: no cover - must not run
        raise AssertionError("classifier must be skipped when force_class is set")

    submitted: list[tuple[str, str | None]] = []

    def submitter(model: str, force_class: str | None) -> str:
        submitted.append((model, force_class))
        return f"job-{expected}"

    row = submit_job(name, submitter=submitter, classifier=classifier)

    assert submitted == [(name, expected)]
    assert row.force_class == expected
    assert row.model == name


def test_submit_job_override_does_not_require_classifier() -> None:
    submitted: list[tuple[str, str | None]] = []

    def submitter(model: str, force_class: str | None) -> str:
        submitted.append((model, force_class))
        return "job-x"

    row = submit_job("coracle-fast", submitter=submitter)

    assert submitted == [("coracle-fast", "fast")]
    assert row.force_class == "fast"


def test_submit_job_default_profile_without_classifier_raises() -> None:
    def submitter(model: str, force_class: str | None) -> str:  # pragma: no cover
        raise AssertionError("submitter must not be called when validation fails")

    with pytest.raises(ValueError, match="classifier is required"):
        submit_job("coracle", submitter=submitter)


def test_submit_job_unknown_profile_raises_profile_not_found() -> None:
    def submitter(model: str, force_class: str | None) -> str:  # pragma: no cover
        raise AssertionError("submitter must not be called for unknown profiles")

    with pytest.raises(ProfileNotFoundError):
        submit_job("does-not-exist", submitter=submitter, classifier=lambda: "fast")


def test_model_profile_dataclass_shape() -> None:
    # Sanity check the public dataclass surface stays stable.
    profile = ModelProfile(name="x", force_class=None, description="d")
    assert profile.name == "x"
    assert profile.force_class is None
    assert profile.description == "d"
