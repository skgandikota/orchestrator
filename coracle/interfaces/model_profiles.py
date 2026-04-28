"""Public model profiles surfaced by ``GET /v1/models``.

The user-facing contract for the coracle is a **single auto-routing
model** named ``coracle``. Internally the coracle routes between
``fast``/``deep``/``research``/``status`` pipelines based on a classifier;
that routing is invisible to callers by default.

Named profiles (``coracle-fast``, ``coracle-deep``,
``coracle-research``, ``coracle-status``) exist purely as
**escape-hatch overrides**: when the caller wants to *force* a specific
pipeline class and bypass the classifier entirely, they pick one of those
names. They are not separate "routing flavors" -- they are just the four
pin-this-class shortcuts.

This module is intentionally dependency-free (no FastAPI, no DB) so it can
be imported and exercised in isolation by unit tests. The HTTP layer in
``openai_compat.py`` consumes :func:`resolve_profile` and
:func:`list_profiles` to build OpenAI-compatible responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DEFAULT_PROFILE_NAME",
    "ForceClass",
    "ModelProfile",
    "ProfileNotFoundError",
    "list_profiles",
    "resolve_profile",
]

ForceClass = Literal["fast", "deep", "research", "status"]

DEFAULT_PROFILE_NAME = "coracle"


@dataclass(frozen=True)
class ModelProfile:
    """A user-selectable model profile.

    ``force_class`` is ``None`` for the default auto-routing profile, and
    one of the four pipeline classes for the override profiles.
    """

    name: str
    force_class: ForceClass | None
    description: str


class ProfileNotFoundError(KeyError):
    """Raised when an unknown profile name is resolved.

    The HTTP layer maps this to a 404. We subclass :class:`KeyError` so
    that ``dict``-style ``except KeyError`` paths also catch it.
    """

    status_code: int = 404

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"unknown model profile: {self.name!r}"


# The registry is a module-level ordered dict so iteration order is the
# canonical "list" order we want to expose via ``GET /v1/models``: the
# default ``coracle`` first, then the four overrides.
_REGISTRY: dict[str, ModelProfile] = {
    "coracle": ModelProfile(
        name="coracle",
        force_class=None,
        description=(
            "Default auto-routing model. The coracle picks a pipeline "
            "(fast/deep/research/status) based on the request's classifier."
        ),
    ),
    "coracle-fast": ModelProfile(
        name="coracle-fast",
        force_class="fast",
        description="Override: skip the classifier and run the fast pipeline.",
    ),
    "coracle-deep": ModelProfile(
        name="coracle-deep",
        force_class="deep",
        description="Override: skip the classifier and run the deep pipeline.",
    ),
    "coracle-research": ModelProfile(
        name="coracle-research",
        force_class="research",
        description="Override: skip the classifier and run the research pipeline.",
    ),
    "coracle-status": ModelProfile(
        name="coracle-status",
        force_class="status",
        description="Override: skip the classifier and run the status pipeline.",
    ),
}


def resolve_profile(name: str) -> ModelProfile:
    """Look up a profile by name.

    Raises:
        ProfileNotFoundError: ``name`` is not a registered profile.
    """

    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ProfileNotFoundError(name) from exc


def list_profiles() -> list[ModelProfile]:
    """Return all registered profiles, default ``coracle`` first."""

    return list(_REGISTRY.values())
