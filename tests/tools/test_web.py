"""Tests for orchestrator.tools.web."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from orchestrator.config.settings import Settings, ToolsSettings, WebToolSettings
from orchestrator.tools import web
from orchestrator.tools.web import (
    FetchError,
    FetchResult,
    SearchError,
    SearchResult,
    fetch,
    search,
)


def _settings(**web_overrides: Any) -> Settings:
    return Settings(tools=ToolsSettings(web=WebToolSettings(**web_overrides)))


@pytest.fixture(autouse=True)
def _allow_public(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """All fetch tests run against example.com which we treat as public."""

    def fake_getaddrinfo(host: str, *_a: Any, **_kw: Any) -> list[Any]:
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(web.socket, "getaddrinfo", fake_getaddrinfo)
    yield


# --------------------------------------------------------------------------- fetch


@respx.mock
def test_fetch_happy_path_strips_html() -> None:
    body = (
        b"<html><head><style>.x{}</style></head>"
        b"<body><!-- secret --><script>alert(1)</script>"
        b"<p>Hello <b>world</b></p></body></html>"
    )
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "text/html"})
    )
    result = fetch("https://example.com/", settings=_settings())
    assert isinstance(result, FetchResult)
    assert result.status == 200
    assert result.truncated is False
    assert "Hello world" in result.text
    assert "alert" not in result.text
    assert "secret" not in result.text
    assert ".x{}" not in result.text
    assert result.elapsed_ms >= 0


@respx.mock
def test_fetch_404_returns_status() -> None:
    respx.get("https://example.com/missing").mock(
        return_value=httpx.Response(404, content=b"nope", headers={"content-type": "text/plain"})
    )
    result = fetch("https://example.com/missing", settings=_settings())
    assert result.status == 404
    assert result.text == "nope"
    assert result.truncated is False


@respx.mock
def test_fetch_timeout_raises() -> None:
    respx.get("https://example.com/slow").mock(side_effect=httpx.ReadTimeout("boom"))
    with pytest.raises(FetchError, match="timeout"):
        fetch("https://example.com/slow", settings=_settings())


@respx.mock
def test_fetch_truncates_oversize_body() -> None:
    big = b"a" * 5000
    respx.get("https://example.com/big").mock(
        return_value=httpx.Response(200, content=big, headers={"content-type": "text/plain"})
    )
    result = fetch("https://example.com/big", max_bytes=1024, settings=_settings())
    assert result.truncated is True
    assert len(result.text) <= 1024


def test_fetch_rejects_non_http_scheme() -> None:
    with pytest.raises(FetchError, match="scheme"):
        fetch("file:///etc/passwd", settings=_settings())


def test_fetch_rejects_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *_a: Any, **_kw: Any) -> list[Any]:
        return [(0, 0, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(web.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchError, match="private"):
        fetch("http://localhost/", settings=_settings(allow_private=False))


def test_fetch_allows_private_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *_a: Any, **_kw: Any) -> list[Any]:
        return [(0, 0, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(web.socket, "getaddrinfo", fake_getaddrinfo)
    with respx.mock(assert_all_called=False) as router:
        router.get("http://localhost/").mock(
            return_value=httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
        )
        result = fetch("http://localhost/", settings=_settings(allow_private=True))
    assert result.status == 200


def test_fetch_invalid_max_bytes() -> None:
    with pytest.raises(FetchError):
        fetch("https://example.com/", max_bytes=0, settings=_settings())


@respx.mock
def test_fetch_sends_user_agent() -> None:
    route = respx.get("https://example.com/").mock(
        return_value=httpx.Response(200, content=b"x", headers={"content-type": "text/plain"})
    )
    fetch("https://example.com/", settings=_settings(user_agent="ua-test/9.9"))
    assert route.calls.last.request.headers["user-agent"] == "ua-test/9.9"


# --------------------------------------------------------------------------- search


def test_search_ddg_parses_results(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"title": "First", "href": "https://a.example/", "body": "snip-a"},
        {"title": "Second", "href": "https://b.example/", "body": "snip-b"},
        {"title": "Skip-no-url", "href": "", "body": "x"},
    ]

    class FakeDDGS:
        def __init__(self, *_a: Any, **_kw: Any) -> None: ...
        def __enter__(self) -> FakeDDGS:
            return self

        def __exit__(self, *_a: Any) -> None: ...
        def text(self, query: str, max_results: int) -> list[dict[str, str]]:
            assert query == "python"
            assert max_results == 5
            return rows

    import sys
    import types

    fake_module = types.ModuleType("duckduckgo_search")
    fake_module.DDGS = FakeDDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "duckduckgo_search", fake_module)

    out = search("python", provider="duckduckgo", limit=5, settings=_settings())
    assert [r.url for r in out] == ["https://a.example/", "https://b.example/"]
    assert out[0].title == "First"
    assert out[0].snippet == "snip-a"


def test_search_ddg_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class BoomDDGS:
        def __init__(self, *_a: Any, **_kw: Any) -> None: ...
        def __enter__(self) -> BoomDDGS:
            return self

        def __exit__(self, *_a: Any) -> None: ...
        def text(self, *_a: Any, **_kw: Any) -> list[dict[str, str]]:
            raise RuntimeError("rate limited")

    import sys
    import types

    fake_module = types.ModuleType("duckduckgo_search")
    fake_module.DDGS = BoomDDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "duckduckgo_search", fake_module)

    with pytest.raises(SearchError, match="DuckDuckGo"):
        search("anything", provider="duckduckgo", settings=_settings())


def test_search_brave_without_key_raises() -> None:
    with pytest.raises(SearchError, match="brave_api_key"):
        search("x", provider="brave", settings=_settings(brave_api_key=None))


@respx.mock
def test_search_brave_happy_path() -> None:
    payload = {
        "web": {
            "results": [
                {"title": "T1", "url": "https://r1/", "description": "d1"},
                {"title": "T2", "url": "https://r2/", "description": "d2"},
            ]
        }
    }
    route = respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    out = search(
        "kittens",
        provider="brave",
        limit=2,
        settings=_settings(brave_api_key="secret"),
    )
    assert isinstance(out[0], SearchResult)
    assert [r.url for r in out] == ["https://r1/", "https://r2/"]
    assert route.calls.last.request.headers["x-subscription-token"] == "secret"


@respx.mock
def test_search_brave_http_error() -> None:
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(429, json={"error": "rate"})
    )
    with pytest.raises(SearchError, match="Brave"):
        search("x", provider="brave", settings=_settings(brave_api_key="secret"))


def test_search_rejects_empty_query() -> None:
    with pytest.raises(SearchError):
        search("   ", settings=_settings())


def test_search_rejects_bad_limit() -> None:
    with pytest.raises(SearchError):
        search("x", limit=0, settings=_settings())


def test_fetch_rejects_url_without_host() -> None:
    with pytest.raises(FetchError, match="hostname"):
        fetch("http:///just-a-path", settings=_settings(allow_private=True))


def test_resolves_to_private_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_kw: Any) -> list[Any]:
        raise web.socket.gaierror("dns down")

    monkeypatch.setattr(web.socket, "getaddrinfo", boom)
    with pytest.raises(FetchError, match="DNS lookup failed"):
        fetch("https://example.com/", settings=_settings())


def test_resolves_to_private_skips_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage sockaddr entries are ignored, allowing an otherwise-public host."""

    def weird(*_a: Any, **_kw: Any) -> list[Any]:
        return [(0, 0, 0, "", ("not-an-ip", 0)), (0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(web.socket, "getaddrinfo", weird)
    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/").mock(
            return_value=httpx.Response(200, content=b"x", headers={"content-type": "text/plain"})
        )
        result = fetch("https://example.com/", settings=_settings())
    assert result.status == 200


def test_strip_html_falls_back_on_bad_encoding() -> None:
    """A bogus encoding name still produces text via the utf-8 fallback (lines 116-117)."""
    out = web._strip_html(b"<p>hi</p>", encoding="not-a-real-encoding")
    assert "hi" in out


def test_fetch_skips_empty_chunks_and_handles_exact_max_bytes() -> None:
    """Cover empty-chunk skip (178) and ``remaining <= 0`` early break (181-182)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        def stream():
            yield b""
            yield b"hello"
            yield b"more"

        return httpx.Response(
            200,
            content=stream(),
            headers={"content-type": "text/plain"},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    try:
        result = fetch(
            "https://example.com/chunky",
            max_bytes=5,
            settings=_settings(),
            client=client,
        )
    finally:
        client.close()
    assert result.text == "hello"
    assert result.truncated is True


@respx.mock
def test_fetch_http_error_non_timeout() -> None:
    respx.get("https://example.com/connreset").mock(side_effect=httpx.ConnectError("reset"))
    with pytest.raises(FetchError, match="HTTP error"):
        fetch("https://example.com/connreset", settings=_settings())


@respx.mock
def test_fetch_with_supplied_client_does_not_close_it() -> None:
    """When the caller supplies a client, ``fetch`` must not close it (199->202 branch)."""
    respx.get("https://example.com/with-client").mock(
        return_value=httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    )
    client = httpx.Client()
    try:
        result = fetch("https://example.com/with-client", settings=_settings(), client=client)
        assert result.status == 200
        assert client.is_closed is False
    finally:
        client.close()


def test_normalise_ddg_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """DDG provider stops yielding once *limit* is reached (line 252)."""
    rows = [{"title": f"T{i}", "href": f"https://x{i}.example/", "body": ""} for i in range(5)]

    class FakeDDGS:
        def __init__(self, *_a: Any, **_kw: Any) -> None: ...
        def __enter__(self) -> FakeDDGS:
            return self

        def __exit__(self, *_a: Any) -> None: ...
        def text(self, *_a: Any, **_kw: Any) -> list[dict[str, str]]:
            return rows

    import sys
    import types

    fake_module = types.ModuleType("duckduckgo_search")
    fake_module.DDGS = FakeDDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "duckduckgo_search", fake_module)

    out = search("anything", provider="duckduckgo", limit=2, settings=_settings())
    assert len(out) == 2


@respx.mock
def test_search_brave_skips_rows_missing_url() -> None:
    payload = {
        "web": {
            "results": [
                {"title": "no-url", "url": "", "description": "x"},
                {"title": "ok", "url": "https://r/", "description": "d"},
            ]
        }
    }
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    out = search(
        "kittens",
        provider="brave",
        limit=5,
        settings=_settings(brave_api_key="k"),
    )
    assert [r.url for r in out] == ["https://r/"]


@respx.mock
def test_search_brave_with_supplied_client_does_not_close_it() -> None:
    """Caller-supplied client must survive (279->282 branch)."""
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )
    client = httpx.Client()
    try:
        out = search(
            "x",
            provider="brave",
            settings=_settings(brave_api_key="k"),
            client=client,
        )
        assert out == []
        assert client.is_closed is False
    finally:
        client.close()
