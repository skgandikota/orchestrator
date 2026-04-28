"""Basic PII detection + redaction.

Regex-only detection of emails, phone numbers, US SSN-shaped strings,
and common credit-card numbers (Luhn-checked). Replacements use stable
placeholders so the pipeline can reverse-map on round-trip if the
upstream provider preserves them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern

__all__ = ["PII_PATTERNS", "Match", "luhn_valid", "redact", "scan"]


@dataclass(frozen=True)
class Match:
    """A single PII match."""

    kind: str
    span: tuple[int, int]
    value: str


PII_PATTERNS: dict[str, Pattern[str]] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    "phone": re.compile(
        r"(?<!\d)(?:\+?\d{1,2}[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\d)"
    ),
    "ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    "credit_card": re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)"),
    "ipv4": re.compile(
        r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\d)"
    ),
}


def luhn_valid(number: str) -> bool:
    """Return True if ``number`` (digits only after stripping) is Luhn-valid."""
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scan(text: str) -> list[Match]:
    """Return all PII matches in ``text`` (credit cards must pass Luhn)."""
    out: list[Match] = []
    for kind, pat in PII_PATTERNS.items():
        for m in pat.finditer(text):
            value = m.group(0)
            if kind == "credit_card" and not luhn_valid(value):
                continue
            out.append(Match(kind=kind, span=m.span(), value=value))
    return out


def redact(text: str, matches: list[Match] | None = None) -> tuple[str, dict[str, str]]:
    """Replace each match with ``[PII:<kind>:<n>]`` and return the mapping.

    The mapping is keyed by placeholder so callers can reverse-map upstream
    responses.
    """
    items = matches if matches is not None else scan(text)
    mapping: dict[str, str] = {}
    if not items:
        return text, mapping
    out = text
    counters: dict[str, int] = {}
    for m in sorted(items, key=lambda x: x.span[0], reverse=True):
        counters[m.kind] = counters.get(m.kind, 0) + 1
        token = f"[PII:{m.kind}:{counters[m.kind]}]"
        mapping[token] = m.value
        start, end = m.span
        out = out[:start] + token + out[end:]
    return out, mapping
