"""Tests for orchestrator.tools.browser.

The subprocess worker (and Playwright underneath) is replaced by an
in-process fake transport so no real browser ever launches.
"""

from __future__ import annotations

import io
import json
import sys as _sys
import types
from unittest import mock

import pytest

from orchestrator.tools import _browser_worker
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


# ---------------------------------------------------------------------------
# Subprocess worker (Playwright fully mocked)
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class _FakePage:
    def __init__(
        self,
        *,
        html: str = "<html>hi</html>",
        title: str = "T",
        text: str = "body",
        matches: list[str] | None = None,
        url: str = "https://example.com/",
        screenshot: bytes = b"PNG",
    ):
        self.url = url
        self._html = html
        self._title = title
        self._text = text
        self._matches = matches if matches is not None else []
        self._screenshot = screenshot
        self.closed = False
        self.clicked: str | None = None
        self.filled: tuple[str, str] | None = None
        self.waited_for: str | None = None

    def goto(self, url, wait_until=None):
        self.url = url
        return _FakeResponse(200)

    def evaluate(self, _expr):
        return self._text

    def content(self):
        return self._html

    def title(self):
        return self._title

    def screenshot(self, full_page=False):
        return self._screenshot

    def query_selector_all(self, _selector):
        return [types.SimpleNamespace(inner_text=lambda v=v: v) for v in self._matches]

    def click(self, selector):
        self.clicked = selector

    def fill(self, selector, value):
        self.filled = (selector, value)

    def wait_for_load_state(self, state):
        self.waited_for = state

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, ctx: _FakeContext) -> None:
        self._ctx = ctx
        self.closed = False

    def new_context(self):
        return self._ctx

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser

    def launch(self, headless=True):
        return self._browser


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakePWHandle:
    def __init__(self, pw: _FakePlaywright) -> None:
        self._pw = pw

    def start(self):
        return self._pw


def _install_fake_playwright(page: _FakePage) -> _FakePlaywright:
    ctx = _FakeContext(page)
    browser = _FakeBrowser(ctx)
    chromium = _FakeChromium(browser)
    pw = _FakePlaywright(chromium)
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePWHandle(pw)
    fake_pkg = types.ModuleType("playwright")
    _sys.modules["playwright"] = fake_pkg
    _sys.modules["playwright.sync_api"] = fake_module
    return pw


def test_worker_browse_uses_playwright(monkeypatch):
    page = _FakePage(text="hello", title="ExampleTitle", html="<html/>")
    pw = _install_fake_playwright(page)
    w = _browser_worker._Worker(text_cap=64)
    out = w.browse("https://example.com/")
    assert out["title"] == "ExampleTitle"
    assert out["text"] == "hello"
    assert out["status"] == 200
    assert out["html_truncated"] == "<html/>"
    assert page.closed
    # ensure context cached
    page2 = _FakePage()
    pw.chromium._browser._ctx._page = page2
    w.browse("https://example.com/two")
    assert page2.closed


def test_worker_screenshot_returns_b64():
    page = _FakePage(screenshot=b"\x89PNG-data")
    _install_fake_playwright(page)
    w = _browser_worker._Worker()
    out = w.screenshot("https://example.com/")
    import base64 as _b64

    assert _b64.b64decode(out["png_b64"]) == b"\x89PNG-data"


def test_worker_extract_returns_matches():
    page = _FakePage(matches=["one", "two", ""])
    _install_fake_playwright(page)
    w = _browser_worker._Worker()
    out = w.extract("https://example.com/", "li")
    assert out["matches"] == ["one", "two", ""]


def test_worker_click_and_fill():
    page = _FakePage()
    _install_fake_playwright(page)
    w = _browser_worker._Worker()
    w.click("https://example.com/", "button")
    assert page.clicked == "button"
    assert page.waited_for == "domcontentloaded"

    page2 = _FakePage()
    _install_fake_playwright(page2)
    w2 = _browser_worker._Worker()
    w2.fill("https://example.com/", "input", "value")
    assert page2.filled == ("input", "value")


def test_worker_caps_text_and_html():
    big = "x" * 1000
    page = _FakePage(text=big, html=big)
    _install_fake_playwright(page)
    w = _browser_worker._Worker(text_cap=10)
    out = w.browse("https://example.com/")
    assert len(out["text"]) == 10
    assert len(out["html_truncated"]) == 10


def test_worker_snapshot_handles_empty_text():
    page = _FakePage()
    page._text = ""
    _install_fake_playwright(page)
    w = _browser_worker._Worker()
    out = w.browse("https://example.com/")
    assert out["text"] == ""


def test_worker_snapshot_no_response():
    page = _FakePage()
    _install_fake_playwright(page)
    w = _browser_worker._Worker()
    snap = w._snapshot(page, None, "https://example.com/")
    assert snap["status"] == 0


def test_worker_snapshot_url_fallback():
    page = _FakePage()
    page.url = ""
    _install_fake_playwright(page)
    w = _browser_worker._Worker()
    snap = w._snapshot(page, _FakeResponse(200), "https://fallback/")
    assert snap["url"] == "https://fallback/"


def test_worker_shutdown_releases_resources():
    page = _FakePage()
    pw = _install_fake_playwright(page)
    w = _browser_worker._Worker()
    w.browse("https://example.com/")
    out = w.shutdown()
    assert out == {"ok": True}
    assert pw.stopped
    assert pw.chromium._browser.closed
    assert pw.chromium._browser._ctx.closed
    # Idempotent: nothing to release.
    assert w.shutdown() == {"ok": True}


def test_worker_dispatch_routes_all_methods():
    page = _FakePage(matches=["a"])
    _install_fake_playwright(page)
    w = _browser_worker._Worker()
    assert w.dispatch("browse", {"url": "https://example.com/"})["title"] == "T"
    assert "png_b64" in w.dispatch("screenshot", {"url": "https://example.com/"})
    assert w.dispatch("extract", {"url": "https://example.com/", "selector": "li"})["matches"] == [
        "a"
    ]
    assert w.dispatch("click", {"url": "https://example.com/", "selector": "b"})["status"] == 200
    assert (
        w.dispatch("fill", {"url": "https://example.com/", "selector": "i", "value": "v"})["status"]
        == 200
    )
    assert w.dispatch("shutdown", {}) == {"ok": True}
    with pytest.raises(ValueError):
        w.dispatch("nope", {})


def test_worker_main_loop_exits_on_eof():
    page = _FakePage()
    _install_fake_playwright(page)
    rc = _browser_worker.main(
        stdin=io.StringIO(""), stdout=io.StringIO(), worker=_browser_worker._Worker()
    )
    assert rc == 0


def test_worker_main_loop_handles_protocol_and_errors():
    page = _FakePage()
    _install_fake_playwright(page)
    worker = _browser_worker._Worker()
    stdin = io.StringIO(
        "\n"  # blank line skipped
        "not-json\n"
        + json.dumps({"id": 1, "method": "browse", "params": {"url": "https://example.com/"}})
        + "\n"
        + json.dumps({"id": 2, "method": "boom", "params": {}})
        + "\n"
        + json.dumps({"id": 3, "method": "shutdown", "params": {}})
        + "\n"
        + json.dumps({"id": 4, "method": "browse", "params": {"url": "https://example.com/"}})
        + "\n"  # never reached
    )
    stdout = io.StringIO()
    rc = _browser_worker.main(stdin=stdin, stdout=stdout, worker=worker)
    assert rc == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
    # 1: ProtocolError; 2: result; 3: error; 4: shutdown ok
    assert lines[0]["error"]["type"] == "ProtocolError"
    assert lines[1]["result"]["title"] == "T"
    assert lines[2]["error"]["type"] == "ValueError"
    assert lines[3]["result"] == {"ok": True}
    assert len(lines) == 4  # loop exited after shutdown


def test_worker_main_uses_default_worker_and_streams(monkeypatch):
    # Drive main() with default streams replaced via monkeypatch and ensure
    # it constructs its own _Worker when none is supplied.
    page = _FakePage()
    _install_fake_playwright(page)
    fake_stdin = io.StringIO(json.dumps({"id": 9, "method": "shutdown", "params": {}}) + "\n")
    fake_stdout = io.StringIO()
    monkeypatch.setattr(_browser_worker.sys, "stdin", fake_stdin)
    monkeypatch.setattr(_browser_worker.sys, "stdout", fake_stdout)
    assert _browser_worker.main() == 0
    assert json.loads(fake_stdout.getvalue().strip())["result"] == {"ok": True}


# ---------------------------------------------------------------------------
# Subprocess transport (real subprocess, fake worker payload via -c)
# ---------------------------------------------------------------------------
def test_subprocess_transport_round_trip(tmp_path):
    from orchestrator.tools.browser import _SubprocessTransport

    # Spawn a tiny Python program that echoes a canned reply.
    script = (
        "import sys\n"
        "for line in sys.stdin:\n"
        '    sys.stdout.write(\'{"id":1,"result":{"ok":true}}\\n\')\n'
        "    sys.stdout.flush()\n"
        "    break\n"
    )
    tx = _SubprocessTransport(argv=[_sys.executable, "-c", script])
    try:
        tx.send(json.dumps({"id": 1, "method": "ping", "params": {}}))
        raw = tx.recv(5.0)
        assert raw is not None
        assert json.loads(raw)["result"] == {"ok": True}
    finally:
        tx.close()


def test_subprocess_transport_send_after_close_raises():
    from orchestrator.tools.browser import _SubprocessTransport

    tx = _SubprocessTransport(argv=[_sys.executable, "-c", "import sys; sys.exit(0)"])
    tx.close()
    with pytest.raises(BrowserToolError):
        tx.send("hello")


def test_subprocess_transport_recv_returns_none_after_exit():
    from orchestrator.tools.browser import _SubprocessTransport

    tx = _SubprocessTransport(argv=[_sys.executable, "-c", "import sys; sys.exit(0)"])
    try:
        # Process exits immediately; recv should observe the dead process.
        result = tx.recv(2.0)
        assert result is None
        assert not tx.is_alive()
    finally:
        tx.close()


def test_subprocess_transport_close_kills_hung_process(monkeypatch):
    from orchestrator.tools.browser import _SubprocessTransport

    # Long-sleeping worker; close() must terminate it.
    busy_script = "import time\nwhile True: time.sleep(0.05)"
    tx = _SubprocessTransport(argv=[_sys.executable, "-c", busy_script])
    try:
        assert tx.is_alive()
    finally:
        tx.close()
    assert not tx.is_alive()


def test_browser_tool_default_factory_creates_subprocess_transport():
    from orchestrator.tools.browser import BrowserTool, _SubprocessTransport

    captured: dict[str, object] = {}

    class _NoopTransport:
        def __init__(self):
            captured["created"] = True
            self.alive = True

        def send(self, line):
            captured["sent"] = line

        def recv(self, timeout):
            return json.dumps({"id": 1, "result": {"ok": True}})

        def is_alive(self):
            return self.alive

        def close(self):
            self.alive = False

    tool = BrowserTool(transport_factory=_NoopTransport)
    assert tool._transport_factory is _NoopTransport
    tool.close()  # nothing to close, no-op

    # Ensure the *real* default is _SubprocessTransport even when not used.
    default_tool = BrowserTool.__init__
    assert default_tool is BrowserTool.__init__
    # Construct without any factory and assert the resolved default class.
    t2 = BrowserTool()
    try:
        assert t2._transport_factory is _SubprocessTransport
    finally:
        t2._teardown()


def test_browser_tool_idle_timeout_respawns_transport():
    tx1 = FakeTransport(responses=[_snap()])
    tx2 = FakeTransport(responses=[_snap(title="reborn")])
    tool = BrowserTool(
        transport_factory=_factory(tx1, tx2),
        url_resolver=_public_resolver,
        idle_timeout=0.0001,
    )
    tool.browse("https://example.com/")
    # Force idle expiry by backdating last-used to a truthy past timestamp.
    tool._last_used = 1.0
    snap = tool.browse("https://example.com/")
    assert snap.title == "reborn"
    assert not tx1.is_alive()


def test_call_handles_invalid_json_response():
    class BadTransport(FakeTransport):
        def recv(self, timeout):
            return "not-json"

    tool = BrowserTool(
        transport_factory=_factory(BadTransport()),
        url_resolver=_public_resolver,
    )
    with pytest.raises(BrowserToolError, match="invalid worker response"):
        tool.browse("https://example.com/")


def test_extract_rejects_non_list_matches():
    tx = FakeTransport(responses=[{"result": {"matches": "oops"}}])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    with pytest.raises(BrowserToolError, match="non-list"):
        tool.extract("https://example.com/", "h1")


def test_screenshot_invalid_b64_raises():
    tx = FakeTransport(responses=[{"result": {"png_b64": "validlooking"}}])
    tool = BrowserTool(
        transport_factory=_factory(tx),
        url_resolver=_public_resolver,
    )
    with (
        mock.patch("base64.b64decode", side_effect=ValueError("bad")),
        pytest.raises(BrowserToolError, match="invalid screenshot"),
    ):
        tool.screenshot("https://example.com/")


def test_send_failure_tears_down_transport():
    class ExplodingTransport(FakeTransport):
        def send(self, line):
            raise BrowserToolError("pipe dead")

    tool = BrowserTool(
        transport_factory=_factory(ExplodingTransport()),
        url_resolver=_public_resolver,
    )
    with pytest.raises(BrowserToolError, match="pipe dead"):
        tool.browse("https://example.com/")
    assert tool._transport is None


def test_url_resolver_threaded_through():
    captured: dict[str, object] = {}

    def resolver(host, port):
        captured["host"] = host
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    tx = FakeTransport(responses=[_snap()])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=resolver)
    tool.browse("https://example.com/")
    assert captured["host"] == "example.com"


def test_close_when_transport_already_dead():
    tx = FakeTransport(responses=[_snap()])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    tool.browse("https://example.com/")
    tx._alive = False
    tool.close()
    assert tool._transport is None


def test_close_swallows_send_error_during_shutdown():
    class FlakyOnShutdown(FakeTransport):
        def send(self, line):
            payload = json.loads(line)
            if payload.get("method") == "shutdown":
                raise BrowserToolError("broken pipe")
            return super().send(line)

    tx = FlakyOnShutdown(responses=[_snap()])
    tool = BrowserTool(transport_factory=_factory(tx), url_resolver=_public_resolver)
    tool.browse("https://example.com/")
    tool.close()  # must not raise
    assert tool._transport is None


def test_to_snapshot_passthrough_when_within_cap():
    # Triggers _cap()'s "return text" branch (line 240) by having one field
    # over the cap and the other under it.
    big = "x" * 200
    tx = FakeTransport(responses=[_snap(text=big, html_truncated="short")])
    tool = BrowserTool(
        transport_factory=_factory(tx),
        url_resolver=_public_resolver,
        text_cap=50,
    )
    snap = tool.browse("https://example.com/")
    assert len(snap.text) == 50
    assert snap.html_truncated == "short"


def test_subprocess_transport_send_handles_broken_pipe():
    from orchestrator.tools.browser import _SubprocessTransport

    tx = _SubprocessTransport(argv=[_sys.executable, "-c", "import sys; sys.exit(0)"])
    # Wait for the subprocess to fully exit so writes break.
    if tx._proc is not None:
        tx._proc.wait(timeout=5.0)
    with pytest.raises(BrowserToolError, match="stdin write failed"):
        tx.send("hello")
    tx.close()


def test_subprocess_transport_recv_drains_buffered_after_exit():
    from orchestrator.tools.browser import _SubprocessTransport

    script = (
        'import sys\nsys.stdout.write(\'{"id":1,"result":{"ok":true}}\\n\')\nsys.stdout.flush()\n'
    )
    tx = _SubprocessTransport(argv=[_sys.executable, "-c", script])
    try:
        raw = tx.recv(5.0)
        assert raw is not None
        assert json.loads(raw)["result"] == {"ok": True}
        # Process is now exited; second recv must return None.
        assert tx.recv(0.5) is None
    finally:
        tx.close()


def test_subprocess_transport_close_kill_path_on_terminate_timeout():
    from orchestrator.tools.browser import _SubprocessTransport

    busy = "import time\nwhile True: time.sleep(0.05)"
    tx = _SubprocessTransport(argv=[_sys.executable, "-c", busy])
    proc = tx._proc
    assert proc is not None
    # Force terminate() to appear to time out so close() escalates to kill().
    real_wait = proc.wait

    call_count = {"n": 0}

    def fake_wait(timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            import subprocess as _sub

            raise _sub.TimeoutExpired(cmd="x", timeout=timeout)
        return real_wait(timeout=timeout)

    proc.wait = fake_wait  # type: ignore[assignment]
    tx.close()
    assert call_count["n"] >= 2
    assert not tx.is_alive()


def test_subprocess_transport_close_is_idempotent():
    from orchestrator.tools.browser import _SubprocessTransport

    tx = _SubprocessTransport(argv=[_sys.executable, "-c", "import sys; sys.exit(0)"])
    tx.close()
    tx.close()  # _proc is None; second call must be a no-op
    assert tx._proc is None
