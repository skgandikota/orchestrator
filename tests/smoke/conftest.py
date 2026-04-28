"""Pytest configuration for smoke tests.

Adds a ``--live`` flag that gates ``@pytest.mark.live`` tests so the live
swap-cycle test never runs in CI. The mocked ``@pytest.mark.smoke`` test is
always collected and fast enough for the default suite.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run smoke tests marked @pytest.mark.live against a real Ollama daemon.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="live smoke test; pass --live to enable")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
