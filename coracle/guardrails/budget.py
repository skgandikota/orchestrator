"""Token-budget guard.

Refuses requests where the projected token count would consume more than
``max_fraction`` of a daily quota for the chosen provider. The estimator
is intentionally simple — ``len(text) / 4`` — so the module has no
dependency on a real tokenizer. Callers may pass a custom estimator.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["BudgetExceeded", "BudgetReport", "check", "estimate_tokens"]


def estimate_tokens(text: str) -> int:
    """Return a coarse token estimate (4 chars per token, min 1)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class BudgetReport:
    """Result of a budget check."""

    projected_tokens: int
    daily_quota: int
    used_today: int
    fraction_after: float
    allowed: bool


class BudgetExceeded(RuntimeError):
    """Raised when a budget check is enforced and would be exceeded."""

    def __init__(self, report: BudgetReport) -> None:
        super().__init__(
            f"projected token usage would consume "
            f"{report.fraction_after:.0%} of daily quota "
            f"(quota={report.daily_quota}, used={report.used_today}, "
            f"projected={report.projected_tokens})"
        )
        self.report = report


def check(
    text: str,
    *,
    daily_quota: int,
    used_today: int = 0,
    max_fraction: float = 0.8,
    estimator: Callable[[str], int] = estimate_tokens,
    raise_on_exceed: bool = False,
) -> BudgetReport:
    """Return a :class:`BudgetReport` for the given input.

    Args:
        text: The text whose token count is being projected.
        daily_quota: Total tokens allowed per day for the provider.
        used_today: Tokens already consumed today.
        max_fraction: Refuse when ``(used_today + projected) / quota`` exceeds
            this fraction (0..1).
        estimator: Custom token estimator.
        raise_on_exceed: If True and the budget is exceeded, raise
            :class:`BudgetExceeded`.
    """
    if daily_quota <= 0:
        raise ValueError("daily_quota must be > 0")
    if not 0 < max_fraction <= 1:
        raise ValueError("max_fraction must be in (0, 1]")
    projected = estimator(text)
    fraction_after = (used_today + projected) / daily_quota
    allowed = fraction_after <= max_fraction
    report = BudgetReport(
        projected_tokens=projected,
        daily_quota=daily_quota,
        used_today=used_today,
        fraction_after=fraction_after,
        allowed=allowed,
    )
    if not allowed and raise_on_exceed:
        raise BudgetExceeded(report)
    return report
