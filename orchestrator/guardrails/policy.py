"""Output-policy guard.

Block-list of phrases the assistant must never emit (destructive shell
commands, inline credentials, etc). Matched content is masked and the
severity is escalated to ``block``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern

__all__ = ["DEFAULT_POLICY", "PolicyHit", "evaluate", "mask"]


@dataclass(frozen=True)
class PolicyHit:
    """A policy violation."""

    rule: str
    span: tuple[int, int]
    snippet: str


DEFAULT_POLICY: dict[str, Pattern[str]] = {
    # destructive shell commands
    "rm_rf_root": re.compile(r"\brm\s+-rf?\s+(--no-preserve-root\s+)?/(?:\s|$|\*)"),
    "shutdown_now": re.compile(r"(?i)\b(shutdown|halt|poweroff)\s+(-h\s+)?now\b"),
    "fork_bomb": re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:"),
    "dd_to_disk": re.compile(r"(?i)\bdd\s+if=.+\s+of=/dev/(sd[a-z]|nvme\d+n\d+|disk\d+)"),
    "mkfs_disk": re.compile(r"(?i)\bmkfs(\.\w+)?\s+/dev/"),
    "chmod_world_writable": re.compile(r"\bchmod\s+(-R\s+)?0?777\b"),
    "curl_pipe_shell": re.compile(r"(?i)curl\s+[^\n|]*\|\s*(sudo\s+)?(bash|sh|zsh|fish)\b"),
    "wget_pipe_shell": re.compile(r"(?i)wget\s+[^\n|]*\|\s*(sudo\s+)?(bash|sh|zsh)\b"),
    # hard-coded credential markers
    "password_assignment": re.compile(r"(?i)\bpassword\s*=\s*[\"'][^\"']{3,}[\"']"),
    "api_key_assignment": re.compile(r"(?i)\bapi[_-]?key\s*=\s*[\"'][^\"']{8,}[\"']"),
}


def evaluate(text: str, policy: dict[str, Pattern[str]] | None = None) -> list[PolicyHit]:
    """Return all policy violations found in ``text``."""
    rules = policy if policy is not None else DEFAULT_POLICY
    hits: list[PolicyHit] = []
    for name, pat in rules.items():
        for m in pat.finditer(text):
            hits.append(PolicyHit(rule=name, span=m.span(), snippet=m.group(0)))
    return hits


def mask(text: str, hits: list[PolicyHit] | None = None) -> str:
    """Replace each hit with ``[BLOCKED:<rule>]``."""
    items = hits if hits is not None else evaluate(text)
    if not items:
        return text
    out = text
    for h in sorted(items, key=lambda x: x.span[0], reverse=True):
        s, e = h.span
        out = out[:s] + f"[BLOCKED:{h.rule}]" + out[e:]
    return out
