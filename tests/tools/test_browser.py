"""Tests for orchestrator.tools.browser.

The subprocess worker (and Playwright underneath) is replaced by an
in-process fake transport so no real browser ever launches.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.tools._url_guard import UrlGuardError, check_url
from orchestrator.tools.browser import (
    BrowserTool,
    BrowserToolError,
    PageSnapshot,
)


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------
class FakeTransport:
    """In-process stand-in for the real subprocess transport."""

    def __init__(self, *, responses=None, timeout_first=False, crash_after=None):
        self.responses = list(responses or [])
        self.sent: list[dict] = []
        self._alive = True
        self._timeout_first = timeout_first
        self._crash_after = crash_after
        self._calls = 0

    def send(self, line: str) -> None:
        self._calls += 1
        self.sent.append(json.loads(line))
        if self._crash_after is not None and self._calls > self._crash_after:
            self._alive = False

    def recv(self, timeout: float) -> str | None:
        if self._timeout_first:
            self._timeout_first = False
            return None
        if not self.responses:
            return None
        rid = self.sent[-1]["id"]
        payload = self.responses.pop(0)
        if "error" in payload:
            return json.dumps({"id": rid, "error": payload["error"]})
        return json.dumps({"id": rid, "result": payload.get("result", {})})

    def is_alive(self) -> bool:
        return self._alive

    def close(self) -> None:
        self._alive = False


def _factory(*transports):
    """Build a transport_factory that yields the given transports in order."""
    bucket = list(transports)

    def make() -> FakeTransport:
        return bucket.pop(0)

    return make


def _snap(**overrides):
    base = {
        "url": "https://example.com/",
        "title": "Example",
        "text": "hello",
        "html_truncated": "<html>hi</html>",
        "status": 200,
    }
    base.update(overrides)
    return {"result": base}


# ---------------------------------------------------------------------------
# URL guard
# ---------------------------------------------------------------------------
def test_url_guard_accepts_public_literal():
    assert check_url("https://1.1.1.1/path") == "https://1.1.1.1/path"


def test_url_guard_rejects_private_literal():
    with pytest.raises(UrlGuardError):
        check_url("http://10.0.0.1/")


def test_url_guard_rejects_loopback_literal():
    with pytest.raises(UrlGuardError):
        check_url("http://127.0.0.1/")


def test_url_guard_rejects_bad_scheme():
    with pytest.raises(UrlGuardError):
        check_url("file:///etc/passwd")


def test_url_guard_rejects_private_dns():
    def fake_resolver(host, port):
        return [(0, 0, 0, "", ("192.168.1.5", 0))]

    with pytest.raises(UrlGuardError):
        check_url("http://intranet.example/", resolver=fake_resolver)


def test_url_guard_accepts_public_dns():
    def fake_resolver(host, port):
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    assert check_url("https://example.com/", resolver=fake_resolver) == "https://example.com/"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
def _public_resolver(host, port):
    return [(0, 0, 0, "", ("93.184.216.34", 0))]


def test_browse_returns_snapshot():
    tx = FakeTransport(responses=[_snap()])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    snap = tool.browse("https://example.com/")
    assert isinstance(snap, PageSnapshot)
    assert snap.url == "https://example.com/"
    assert snap.status == 200
    assert tx.sent[0]["method"] == "browse"


def test_screenshot_decodes_b64():
    import base64

    raw = b"\x89PNG\r\n\x1a\nfake"
    tx = FakeTransport(responses=[{"result": {"png_b64": base64.b64encode(raw).decode()}}])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    assert tool.screenshot("https://example.com/") == raw


def test_extract_returns_matches():
    tx = FakeTransport(responses=[{"result": {"matches": ["a", "b"]}}])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    assert tool.extract("https://example.com/", "h1") == ["a", "b"]


def test_extract_selector_miss_returns_empty():
    tx = FakeTransport(responses=[{"result": {"matches": []}}])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    assert tool.extract("https://example.com/", ".missing") == []


def test_click_and_fill_round_trip():
    tx = FakeTransport(
        responses=[
            _snap(title="After click"),
            _snap(title="After fill"),
        ]
    )
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    s1 = tool.click("https://example.com/", "button#go")
    s2 = tool.fill("https://example.com/", "input[name=q]", "hello")
    assert s1.title == "After click"
    assert s2.title == "After fill"
    assert tx.sent[0]["method"] == "click"
    assert tx.sent[1]["method"] == "fill"
    assert tx.sent[1]["params"]["value"] == "hello"


def test_text_is_capped():
    big = "x" * 1000
    tx = FakeTransport(responses=[_snap(text=big, html_truncated=big)])
    tool = BrowserTool(
        transport_factory=_factory(tx),
        url_resolver=_public_resolver,
        text_cap=100,
    )
    snap = tool.browse("https://example.com/")
    assert len(snap.text) == 100
    assert len(snap.html_truncated) == 100


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_blocked_url_raises_before_call():
    calls = {"made": 0}

    def factory():
        calls["made"] += 1
        return FakeTransport()

    tool = BrowserTool(transport_factory=factory)
    with pytest.raises(BrowserToolError):
        tool.browse("http://127.0.0.1/")
    assert calls["made"] == 0


def test_rpc_timeout_raises_and_respawns():
    tx1 = FakeTransport(timeout_first=True)
    tx2 = FakeTransport(responses=[_snap()])
    tool = BrowserTool(
        transport_factory=_factory(tx1, tx2),
        url_resolver=_public_resolver,
        call_timeout=0.05,
    )
    with pytest.raises(BrowserToolError, match="timed out"):
        tool.browse("https://example.com/")
    snap = tool.browse("https://example.com/")
    assert snap.title == "Example"


def test_worker_crash_auto_restarts():
    tx_crashed = FakeTransport(responses=[_snap()], crash_after=0)
    tx_fresh = FakeTransport(responses=[_snap(title="recovered")])
    tool = BrowserTool(
        transport_factory=_factory(tx_crashed, tx_fresh),
        url_resolver=_public_resolver,
    )
    tool.browse("https://example.com/")
    snap = tool.browse("https://example.com/")
    assert snap.title == "recovered"


def test_worker_crash_with_no_response_raises():
    tx = FakeTransport(responses=[], crash_after=0)
    tx2 = FakeTransport()
    tool = BrowserTool(
        transport_factory=_factory(tx, tx2),
        url_resolver=_public_resolver,
        call_timeout=0.05,
    )
    with pytest.raises(BrowserToolError, match="crashed"):
        tool.browse("https://example.com/")


def test_worker_returns_error_payload():
    tx = FakeTransport(responses=[{"error": {"type": "TimeoutError", "message": "nav failed"}}])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    with pytest.raises(BrowserToolError, match="TimeoutError"):
        tool.browse("https://example.com/")


def test_close_is_idempotent():
    tx = FakeTransport(responses=[_snap(), {"result": {"ok": True}}])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    tool.browse("https://example.com/")
    tool.close()
    tool.close()
    assert not tx.is_alive()


def test_context_manager_closes():
    tx = FakeTransport(responses=[_snap(), {"result": {"ok": True}}])
    with BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver) as tool:
        tool.browse("https://example.com/")
    assert not tx.is_alive()
