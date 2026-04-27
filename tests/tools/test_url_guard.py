"""Tests for ``orchestrator.tools._url_guard``."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.tools._url_guard import UrlGuardError, check_url


def _resolver_returning(addr: str):
    def _r(host: str, *_a: Any, **_kw: Any) -> list[Any]:
        return [(0, 0, 0, "", (addr, 0))]

    return _r


def test_check_url_rejects_empty() -> None:
    with pytest.raises(UrlGuardError, match="non-empty"):
        check_url("")


def test_check_url_rejects_non_string() -> None:
    with pytest.raises(UrlGuardError, match="non-empty"):
        check_url(None)  # type: ignore[arg-type]


def test_check_url_rejects_unknown_scheme() -> None:
    with pytest.raises(UrlGuardError, match="scheme"):
        check_url("ftp://example.com/")


def test_check_url_rejects_missing_host() -> None:
    with pytest.raises(UrlGuardError, match="no host"):
        check_url("http:///path")


def test_check_url_allows_public_literal_ip() -> None:
    assert check_url("http://93.184.216.34/") == "http://93.184.216.34/"


def test_check_url_blocks_loopback_literal_ip() -> None:
    with pytest.raises(UrlGuardError, match="blocked"):
        check_url("http://127.0.0.1/")


def test_check_url_resolver_failure_raises() -> None:
    def boom(*_a: Any, **_kw: Any) -> list[Any]:
        raise OSError("dns down")

    with pytest.raises(UrlGuardError, match="could not resolve"):
        check_url("http://example.com/", resolver=boom)


def test_check_url_blocks_private_resolution() -> None:
    with pytest.raises(UrlGuardError, match="blocked address"):
        check_url("http://intranet.example/", resolver=_resolver_returning("10.0.0.1"))


def test_check_url_skips_unparseable_resolved_addr() -> None:
    """Resolver returning a non-IP sockaddr -> skipped, URL accepted."""

    def weird(*_a: Any, **_kw: Any) -> list[Any]:
        return [(0, 0, 0, "", ("not-an-ip", 0))]

    assert check_url("http://example.com/", resolver=weird) == "http://example.com/"


def test_check_url_passes_when_public_resolution() -> None:
    assert (
        check_url("http://example.com/", resolver=_resolver_returning("93.184.216.34"))
        == "http://example.com/"
    )
