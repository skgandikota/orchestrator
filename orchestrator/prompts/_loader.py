"""Loader helpers for versioned prompt templates.

Every file inside :mod:`orchestrator.prompts` must declare a header of
the form ``# version: N`` (where ``N`` is a positive integer). The
loader exposes the parsed version alongside the prompt body so that
eval reports can attribute regressions to a specific template revision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"^\s*#\s*version\s*:\s*(\d+)\s*$", re.IGNORECASE)

PROMPTS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Prompt:
    """A loaded prompt template with its declared version."""

    name: str
    version: int
    body: str
    path: Path


def parse_version(text: str) -> int:
    """Return the integer declared by the ``# version: N`` header.

    Raises ``ValueError`` if the header is missing or malformed.
    """

    for line in text.splitlines():
        if not line.strip():
            continue
        match = _VERSION_RE.match(line)
        if match:
            return int(match.group(1))
        # The first non-empty line must be the version header.
        break
    raise ValueError("prompt is missing a '# version: N' header on the first line")


def load_prompt(path: str | Path) -> Prompt:
    """Load a single prompt file and return a :class:`Prompt`."""

    p = Path(path)
    text = p.read_text(encoding="utf-8")
    version = parse_version(text)
    body_lines = text.splitlines()
    # Strip the version header (and any blank line directly after it).
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    if body_lines:
        body_lines.pop(0)  # the header itself
    if body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    return Prompt(name=p.stem, version=version, body="\n".join(body_lines), path=p)


def load_all(directory: str | Path | None = None) -> dict[str, Prompt]:
    """Load every ``*.txt`` / ``*.md`` prompt under ``directory``."""

    base = Path(directory) if directory else PROMPTS_DIR
    out: dict[str, Prompt] = {}
    for path in sorted(base.iterdir()):
        if path.suffix.lower() not in {".txt", ".md"}:
            continue
        prompt = load_prompt(path)
        out[prompt.name] = prompt
    return out
