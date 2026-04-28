"""Web fetch + search tool.

Provides:

* :func:`fetch` - stream a URL via ``httpx`` with size cap, scheme/IP
  guards, and HTML sanitisation (``<script>``/``<style>``/comments stripped).
* :func:`search` - run a web search via DuckDuckGo (default) or Brave.

All HTTP calls flow through a shared :class:`httpx.Client` configured with the
``tools.web.user_agent`` from settings. No live network calls are made unless
the caller actually invokes one of these functions.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Iterable
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field
from selectolax.parser import HTMLParser

from coracle.config.settings import Settings, WebToolSettings, load_settings

__all__ = [
    "FetchError",
    "FetchResult",
    "SearchError",
    "SearchResult",
    "fetch",
    "search",
]

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class FetchError(RuntimeError):
    """Raised when :func:`fetch` cannot complete a request safely."""


class SearchError(RuntimeError):
    """Raised when :func:`search` cannot complete a query."""


class FetchResult(BaseModel):
    url: str
    status: int
    content_type: str = ""
    text: str = ""
    truncated: bool = False
    elapsed_ms: int = Field(ge=0)


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


class _SearchProvider(Protocol):
    def search(self, query: str, limit: int) -> list[SearchResult]: ...


def _web_settings(settings: Settings | None) -> WebToolSettings:
    return (settings or load_settings()).tools.web


def _validate_url(url: str, *, allow_private: bool) -> str:
    """Validate scheme + host. Returns the parsed hostname."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise FetchError(f"unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise FetchError(f"URL is missing a hostname: {url!r}")
    if not allow_private and _resolves_to_private(host):
        raise FetchError(f"refusing to fetch private/loopback host: {host!r}")
    return host


def _resolves_to_private(host: str) -> bool:
    """True if any A/AAAA record for ``host`` is private/loopback/link-local."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"DNS lookup failed for {host!r}: {exc}") from exc
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


def _strip_html(body: bytes, encoding: str | None) -> str:
    """Return human-readable text with <script>/<style>/comments stripped."""
    enc = encoding or "utf-8"
    try:
        markup = body.decode(enc, errors="replace")
    except (LookupError, TypeError):
        markup = body.decode("utf-8", errors="replace")
    tree = HTMLParser(markup)
    for sel in ("script", "style"):
        for node in tree.css(sel):
            node.decompose()
    # Strip HTML comments. selectolax exposes them as nodes whose tag is
    # "_comment"; iterate defensively in case the backend changes.
    body_node = tree.body or tree.root
    if body_node is not None:  # pragma: no branch - selectolax always yields body or root
        for node in list(body_node.iter(include_text=False)):
            if getattr(node, "tag", None) == "_comment":
                node.decompose()
    text = tree.text(separator=" ", strip=True) if tree.body else ""
    return " ".join(text.split())


def _build_client(ua: str, timeout: float) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": ua},
    )


def fetch(
    url: str,
    max_bytes: int = 1_000_000,
    timeout: float = 15,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Fetch ``url`` and return a sanitised :class:`FetchResult`.

    Args:
        url: Absolute http(s) URL.
        max_bytes: Maximum body size (bytes). Streaming aborts once exceeded
            and ``truncated=True`` is set on the result.
        timeout: Per-request timeout in seconds.
        settings: Optional pre-loaded :class:`Settings`.
        client: Optional pre-built :class:`httpx.Client` (used by tests).

    Raises:
        FetchError: For unsupported schemes, blocked private hosts, DNS
            failures, network errors, or timeouts.
    """
    if max_bytes <= 0:
        raise FetchError("max_bytes must be positive")
    web = _web_settings(settings)
    _validate_url(url, allow_private=web.allow_private)

    owns_client = client is None
    http = client or _build_client(web.user_agent, timeout)
    start = time.perf_counter()
    try:
        with http.stream("GET", url, timeout=timeout) as response:
            chunks: list[bytes] = []
            total = 0
            truncated = False
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                remaining = max_bytes - total
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    chunks.append(chunk[:remaining])
                    total += remaining
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)
            body = b"".join(chunks)
            content_type = response.headers.get("content-type", "")
            encoding = response.encoding
            status = response.status_code
    except httpx.TimeoutException as exc:
        raise FetchError(f"timeout fetching {url!r}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"HTTP error fetching {url!r}: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    text = (
        _strip_html(body, encoding)
        if "html" in content_type.lower()
        else body.decode(encoding or "utf-8", errors="replace")
    )
    return FetchResult(
        url=url,
        status=status,
        content_type=content_type,
        text=text,
        truncated=truncated,
        elapsed_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class _DuckDuckGoProvider:
    def __init__(self, user_agent: str) -> None:
        self._user_agent = user_agent

    def search(self, query: str, limit: int) -> list[SearchResult]:
        try:
            from duckduckgo_search import DDGS  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dep guard
            raise SearchError("duckduckgo-search is not installed") from exc
        try:
            with DDGS(headers={"User-Agent": self._user_agent}) as ddgs:
                raw = list(ddgs.text(query, max_results=limit))
        except Exception as exc:
            raise SearchError(f"DuckDuckGo search failed: {exc}") from exc
        return _normalise_ddg(raw, limit)


def _normalise_ddg(rows: Iterable[dict[str, str]], limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    for row in rows:
        url = row.get("href") or row.get("url") or ""
        if not url:
            continue
        results.append(
            SearchResult(
                title=row.get("title", ""),
                url=url,
                snippet=row.get("body") or row.get("snippet") or "",
            )
        )
        if len(results) >= limit:
            break
    return results


class _BraveProvider:
    def __init__(self, api_key: str, user_agent: str, client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._user_agent = user_agent
        self._client = client

    def search(self, query: str, limit: int) -> list[SearchResult]:
        owns_client = self._client is None
        http = self._client or _build_client(self._user_agent, timeout=15)
        try:
            response = http.get(
                _BRAVE_ENDPOINT,
                params={"q": query, "count": limit},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._api_key,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise SearchError(f"Brave search failed: {exc}") from exc
        finally:
            if owns_client:
                http.close()

        web_block = payload.get("web") or {}
        rows = web_block.get("results") or []
        results: list[SearchResult] = []
        for row in rows[:limit]:
            url = row.get("url") or ""
            if not url:
                continue
            results.append(
                SearchResult(
                    title=row.get("title", ""),
                    url=url,
                    snippet=row.get("description") or row.get("snippet") or "",
                )
            )
        return results


def search(
    query: str,
    provider: Literal["duckduckgo", "brave"] = "duckduckgo",
    limit: int = 10,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> list[SearchResult]:
    """Run a web search and return up to ``limit`` :class:`SearchResult`.

    Args:
        query: Free-text search query.
        provider: ``"duckduckgo"`` (default, no key) or ``"brave"`` (requires
            ``settings.tools.web.brave_api_key``).
        limit: Maximum number of results to return.
        settings: Optional pre-loaded :class:`Settings`.
        client: Optional pre-built :class:`httpx.Client` (Brave only; tests).

    Raises:
        SearchError: On unknown provider, missing Brave key, or upstream
            failures.
    """
    if not query or not query.strip():
        raise SearchError("query must be a non-empty string")
    if limit <= 0:
        raise SearchError("limit must be positive")
    web = _web_settings(settings)
    if provider == "duckduckgo":
        impl: _SearchProvider = _DuckDuckGoProvider(web.user_agent)
    elif provider == "brave":
        if not web.brave_api_key:
            raise SearchError(
                "Brave provider selected but settings.tools.web.brave_api_key is unset"
            )
        impl = _BraveProvider(web.brave_api_key, web.user_agent, client=client)
    else:  # pragma: no cover - guarded by Literal but defensive
        raise SearchError(f"unknown search provider: {provider!r}")
    return impl.search(query, limit)
