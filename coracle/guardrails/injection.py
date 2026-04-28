"""Prompt-injection heuristics.

Pattern-based detection of common jailbreak / instruction-override
phrases. Inspired by Simon Willison's published lists and the OWASP LLM
Top-10. Designed for retrieved external content (web/MCP/tool output);
running it on user messages is acceptable but expected to false-positive
more often.
"""

from __future__ import annotations

import re
from re import Pattern

__all__ = ["INJECTION_PATTERNS", "detect", "score"]


INJECTION_PATTERNS: dict[str, Pattern[str]] = {
    "ignore_previous": re.compile(
        r"(?i)\bignore\s+(all\s+)?(the\s+)?(previous|prior|above)\s+"
        r"(instructions?|prompts?|messages?|rules?)\b"
    ),
    "disregard_previous": re.compile(
        r"(?i)\bdisregard\s+(all\s+)?(the\s+)?(previous|prior|above)\b"
    ),
    "system_prompt_leak": re.compile(r"(?i)\b(system\s*prompt|developer\s*message)\s*:"),
    "role_override": re.compile(r"(?i)\byou\s+are\s+now\s+(a|an|the)\s+\w+"),
    "jailbreak_dan": re.compile(r"(?i)\b(DAN|do\s+anything\s+now)\b"),
    "do_not_refuse": re.compile(r"(?i)\b(do\s+not|never)\s+refuse\b"),
    "reveal_prompt": re.compile(
        r"(?i)\b(reveal|print|repeat|show)\s+(your|the)\s+(system|hidden|initial)\s+prompt\b"
    ),
    "act_as": re.compile(r"(?i)\bact\s+as\s+(if\s+you\s+were\s+|a|an|the)\s*\w+"),
    "bypass_safety": re.compile(r"(?i)\bbypass\s+(all\s+)?(safety|guard|filter|restriction)s?\b"),
    "base64_payload": re.compile(r"(?i)\bbase64\s*(decoded?|encoded?)\b"),
    "hidden_instruction_html": re.compile(r"<!--\s*(SYSTEM|INSTRUCTION|PROMPT)[\s:].*?-->", re.S),
    "delim_injection": re.compile(r"(?im)^\s*###\s*(new\s+)?(instructions?|system)\s*:?\s*$"),
}


def detect(text: str) -> list[str]:
    """Return the list of pattern names that fired against ``text``."""
    return [name for name, pat in INJECTION_PATTERNS.items() if pat.search(text)]


def score(text: str) -> float:
    """Naive 0..1 risk score (number of patterns / total)."""
    if not INJECTION_PATTERNS:  # pragma: no cover - defensive
        return 0.0
    return len(detect(text)) / len(INJECTION_PATTERNS)
