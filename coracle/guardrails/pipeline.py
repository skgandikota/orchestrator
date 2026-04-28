"""Composable guardrail pipeline.

Chains the individual rule modules into one ``check_input`` /
``check_output`` surface. Each call returns a :class:`GuardrailDecision`
that callers can route on (allow / modify / block).

The pipeline is purely synchronous and side-effect-free: callers are
responsible for emitting audit-log events on the returned decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from . import injection, pii, policy, secrets
from .budget import BudgetReport
from .budget import check as budget_check

__all__ = [
    "GuardrailDecision",
    "GuardrailPipeline",
    "GuardrailResult",
    "Severity",
    "build_default_pipeline",
]


Severity = Literal["info", "warn", "block"]


@dataclass(frozen=True)
class GuardrailResult:
    """Output of a single rule."""

    rule: str
    severity: Severity
    reason: str | None = None
    modified_content: str | None = None


@dataclass
class GuardrailDecision:
    """Aggregated outcome of a full pipeline pass."""

    allowed: bool
    content: str
    results: list[GuardrailResult] = field(default_factory=list)
    pii_mapping: dict[str, str] = field(default_factory=dict)

    @property
    def severity(self) -> Severity:
        order: dict[Severity, int] = {"info": 0, "warn": 1, "block": 2}
        worst: Severity = "info"
        for r in self.results:
            if order[r.severity] > order[worst]:
                worst = r.severity
        return worst


@dataclass
class GuardrailPipeline:
    """Chain of guardrail checks for input + output sides."""

    enabled: bool = True
    redact_pii: bool = True
    redact_secrets: bool = True
    detect_injection: bool = True
    enforce_policy: bool = True
    daily_token_quota: int | None = None
    max_token_fraction: float = 0.8
    used_tokens_provider: Callable[[], int] | None = None

    def check_input(self, content: str) -> GuardrailDecision:
        """Run input-side checks (injection + budget + PII redaction)."""
        decision = GuardrailDecision(allowed=True, content=content)
        if not self.enabled:
            return decision

        if self.detect_injection:
            hits = injection.detect(content)
            if hits:
                decision.results.append(
                    GuardrailResult(
                        rule="prompt_injection",
                        severity="warn",
                        reason=f"matched patterns: {', '.join(hits)}",
                    )
                )

        if self.redact_pii:
            redacted, mapping = pii.redact(decision.content)
            if mapping:
                decision.content = redacted
                decision.pii_mapping = mapping
                decision.results.append(
                    GuardrailResult(
                        rule="pii",
                        severity="info",
                        reason=f"redacted {len(mapping)} item(s)",
                        modified_content=redacted,
                    )
                )

        if self.daily_token_quota is not None:
            used = self.used_tokens_provider() if self.used_tokens_provider else 0
            report: BudgetReport = budget_check(
                decision.content,
                daily_quota=self.daily_token_quota,
                used_today=used,
                max_fraction=self.max_token_fraction,
            )
            if not report.allowed:
                decision.allowed = False
                decision.results.append(
                    GuardrailResult(
                        rule="token_budget",
                        severity="block",
                        reason=(
                            f"would consume {report.fraction_after:.0%} of "
                            f"daily quota (max {self.max_token_fraction:.0%})"
                        ),
                    )
                )

        return decision

    def check_output(self, content: str) -> GuardrailDecision:
        """Run output-side checks (secret redaction + output policy)."""
        decision = GuardrailDecision(allowed=True, content=content)
        if not self.enabled:
            return decision

        if self.redact_secrets:
            findings = secrets.scan(decision.content)
            if findings:
                decision.content = secrets.redact(decision.content, findings)
                decision.results.append(
                    GuardrailResult(
                        rule="secrets",
                        severity="warn",
                        reason=f"redacted {len(findings)} secret-shaped value(s)",
                        modified_content=decision.content,
                    )
                )

        if self.enforce_policy:
            hits = policy.evaluate(decision.content)
            if hits:
                masked = policy.mask(decision.content, hits)
                decision.content = masked
                decision.allowed = False
                decision.results.append(
                    GuardrailResult(
                        rule="output_policy",
                        severity="block",
                        reason=f"matched rules: {', '.join(h.rule for h in hits)}",
                        modified_content=masked,
                    )
                )

        return decision


def build_default_pipeline(
    *,
    enabled: bool = True,
    daily_token_quota: int | None = None,
    used_tokens_provider: Callable[[], int] | None = None,
) -> GuardrailPipeline:
    """Construct a :class:`GuardrailPipeline` with all rules turned on."""
    return GuardrailPipeline(
        enabled=enabled,
        daily_token_quota=daily_token_quota,
        used_tokens_provider=used_tokens_provider,
    )
