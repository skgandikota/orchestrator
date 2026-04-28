"""Secret-leak detector.

Regex pack inspired by ``detect-secrets`` / ``gitleaks``. Operates on
arbitrary text. Matches are redacted in place and reported as findings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern

__all__ = ["SECRET_PATTERNS", "Finding", "redact", "scan"]


@dataclass(frozen=True)
class Finding:
    """A single match produced by :func:`scan`."""

    rule: str
    span: tuple[int, int]
    snippet: str


_AK = "AK" + "IA"
_AS = "AS" + "IA"
_GHP = "ghp" + "_"
_GHO = "gho" + "_"
_GHPAT = "github_pat" + "_"
_XOX = "xox"
_AIZA = "AI" + "za"
_SK_ANT = "sk-" + "ant" + "-"
_PK_BEGIN = "-----" + "BEGIN" + " "
_PK_TAIL = "PRIVATE" + " " + "KEY" + "-----"
_JWT_HEAD = "ey" + "J"


SECRET_PATTERNS: dict[str, Pattern[str]] = {
    "aws_access_key": re.compile(r"\b(?:" + _AK + "|" + _AS + r")[0-9A-Z]{16}\b"),
    "aws_secret_key": re.compile(
        r"(?i)aws(.{0,20})?(secret|access).{0,20}?['\"]?[A-Za-z0-9/+=]{40}['\"]?"
    ),
    "github_token": re.compile(r"\b" + _GHP + r"[A-Za-z0-9]{36}\b"),
    "github_oauth": re.compile(r"\b" + _GHO + r"[A-Za-z0-9]{36}\b"),
    "github_fine_grained": re.compile(r"\b" + _GHPAT + r"[A-Za-z0-9_]{82}\b"),
    "slack_token": re.compile(r"\b" + _XOX + r"[abprs]-[A-Za-z0-9-]{10,}\b"),
    "google_api_key": re.compile(r"\b" + _AIZA + r"[0-9A-Za-z\-_]{35}\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "anthropic_key": re.compile(r"\b" + _SK_ANT + r"[A-Za-z0-9_\-]{20,}\b"),
    "private_key_block": re.compile(_PK_BEGIN + r"(?:RSA |EC |DSA |OPENSSH |PGP )?" + _PK_TAIL),
    "jwt": re.compile(
        r"\b" + _JWT_HEAD + r"[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
    ),
}


def scan(text: str) -> list[Finding]:
    """Return all secret-shaped matches in ``text``."""
    findings: list[Finding] = []
    for rule, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append(Finding(rule=rule, span=match.span(), snippet=_mask(match.group(0))))
    return findings


def redact(text: str, findings: list[Finding] | None = None) -> str:
    """Replace each finding with a stable ``[REDACTED:<rule>]`` placeholder."""
    items = findings if findings is not None else scan(text)
    if not items:
        return text
    out = text
    # apply right-to-left so spans don't shift
    for f in sorted(items, key=lambda x: x.span[0], reverse=True):
        start, end = f.span
        out = out[:start] + f"[REDACTED:{f.rule}]" + out[end:]
    return out


def _mask(raw: str) -> str:
    if len(raw) <= 8:
        return "***"
    return f"{raw[:4]}…{raw[-2:]}"
