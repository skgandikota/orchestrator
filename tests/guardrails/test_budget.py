import pytest

from coracle.guardrails import budget


def test_estimate_tokens_basic():
    assert budget.estimate_tokens("") == 0
    assert budget.estimate_tokens("a") == 1
    # 8 chars / 4 = 2 tokens
    assert budget.estimate_tokens("abcdefgh") == 2


def test_check_under_budget_allowed():
    report = budget.check("hello", daily_quota=1000)
    assert report.allowed is True
    assert report.projected_tokens >= 1


def test_check_over_budget_disallowed_no_raise():
    text = "x" * 10_000
    report = budget.check(text, daily_quota=100, max_fraction=0.5)
    assert report.allowed is False
    assert report.fraction_after > 0.5


def test_check_over_budget_raises_when_requested():
    with pytest.raises(budget.BudgetExceeded) as excinfo:
        budget.check("x" * 10_000, daily_quota=100, raise_on_exceed=True)
    assert excinfo.value.report.allowed is False


def test_check_validates_inputs():
    with pytest.raises(ValueError):
        budget.check("hi", daily_quota=0)
    with pytest.raises(ValueError):
        budget.check("hi", daily_quota=10, max_fraction=0)
    with pytest.raises(ValueError):
        budget.check("hi", daily_quota=10, max_fraction=1.5)


def test_check_uses_custom_estimator():
    report = budget.check("ignored", daily_quota=100, estimator=lambda _t: 50)
    assert report.projected_tokens == 50


def test_check_accounts_for_used_tokens():
    report = budget.check("hi", daily_quota=100, used_today=95, max_fraction=0.9)
    assert report.allowed is False
