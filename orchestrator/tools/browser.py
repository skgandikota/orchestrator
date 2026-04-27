"""Coder-facing browser tool.

Wraps a subprocess worker that owns a Playwright ``BrowserContext``.
The worker is started lazily, reused across calls, and torn down on
:meth:`BrowserTool.close`. Crashes or hangs are detected per-call and
surface as :class:`BrowserToolError` while the process is respawned for
the next call.

The public surface is intentionally synchronous: any async behaviour
lives inside the worker subprocess.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ._url_guard import UrlGuardError, check_url

DEFAULT_CALL_TIMEOUT = 30.0
DEFAULT_IDLE_TIMEOUT = 300.0
DEFAULT_TEXT_CAP = 200 * 1024


class BrowserToolError(RuntimeError):
    """Raised when the worker times out, crashes, or returns an error."""


@dataclass(frozen=True)
class PageSnapshot:
    """Snapshot returned by navigation-style operations."""

    url: str
    title: str
    text: str
    html_truncated: str
    status: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PageSnapshot:
        return cls(
            url=str(data.get("url", "")),
            title=str(data.get("title", "")),
            text=str(data.get("text", "")),
            html_truncated=str(data.get("html_truncated", "")),
            status=int(data.get("status", 0)),
        )


class _Transport(Protocol):
    """Bidirectional line-delimited JSON channel to the worker."""

    def send(self, line: str) -> None: ...
    def recv(self, timeout: float) -> str | None: ...
    def is_alive(self) -> bool: ...
    def close(self) -> None: ...


class _SubprocessTransport:
    """Default transport: a real ``python -m`` subprocess."""

    def __init__(self, argv: list[str] | None = None) -> None:
        self._argv = argv or [sys.executable, "-m", "orchestrator.tools._browser_worker"]
        self._proc: subprocess.Popen[str] | None = None
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._start()

    def _start(self) -> None:
        self._proc = subprocess.Popen(
            self._argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            with self._lock:
                self._lines.append(line)

    def send(self, line: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise BrowserToolError("worker stdin closed")
        try:
            self._proc.stdin.write(line if line.endswith("\n") else line + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise BrowserToolError(f"worker stdin write failed: {exc}") from exc

    def recv(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._lines:
                    return self._lines.pop(0)
            if self._proc is not None and self._proc.poll() is not None:
                with self._lock:
                    if self._lines:
                        return self._lines.pop(0)
                return None
            time.sleep(0.01)
        return None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2.0)
        finally:
            self._proc = None


class BrowserTool:
    """Synchronous browser tool callable from the coder dispatcher.

    Parameters
    ----------
    call_timeout:
        Per-call hard deadline (seconds). A timeout kills and respawns
        the worker.
    idle_timeout:
        Worker is shut down if no call happens within this many seconds.
    text_cap:
        Soft upper bound (in characters) on ``text`` and ``html_truncated``
        in returned snapshots. Enforced both in worker and parent.
    transport_factory:
        Test seam: a zero-arg callable returning a :class:`_Transport`.
        Defaults to spawning the real subprocess worker.
    """

    def __init__(
        self,
        *,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        text_cap: int = DEFAULT_TEXT_CAP,
        transport_factory=None,
        url_resolver=None,
    ) -> None:
        self.call_timeout = call_timeout
        self.idle_timeout = idle_timeout
        self.text_cap = text_cap
        self._transport_factory = transport_factory or _SubprocessTransport
        self._url_resolver = url_resolver
        self._transport: _Transport | None = None
        self._next_id = 0
        self._last_used = 0.0
        self._lock = threading.Lock()

    # -- transport plumbing ------------------------------------------
    def _ensure_transport(self) -> _Transport:
        if self._transport is not None and self._transport.is_alive():
            if (
                self.idle_timeout > 0
                and self._last_used
                and time.monotonic() - self._last_used > self.idle_timeout
            ):
                self._teardown()
            else:
                return self._transport
        self._teardown()
        self._transport = self._transport_factory()
        return self._transport

    def _teardown(self) -> None:
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.close()
            self._transport = None

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            transport = self._ensure_transport()
            self._next_id += 1
            rid = self._next_id
            payload = json.dumps({"id": rid, "method": method, "params": params})
            try:
                transport.send(payload)
                raw = transport.recv(self.call_timeout)
            except BrowserToolError:
                self._teardown()
                raise

            if raw is None:
                alive = transport.is_alive()
                self._teardown()
                if not alive:
                    raise BrowserToolError(f"worker crashed during {method!r}")
                raise BrowserToolError(f"worker timed out after {self.call_timeout}s on {method!r}")

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._teardown()
                raise BrowserToolError(f"invalid worker response: {exc}") from exc

            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                raise BrowserToolError(
                    f"{err.get('type', 'WorkerError')}: {err.get('message', '')}"
                )
            self._last_used = time.monotonic()
            return msg.get("result") or {}

    # -- URL pre-flight ----------------------------------------------
    def _guard(self, url: str) -> str:
        try:
            if self._url_resolver is not None:
                return check_url(url, resolver=self._url_resolver)
            return check_url(url)
        except UrlGuardError as exc:
            raise BrowserToolError(f"blocked url: {exc}") from exc

    # -- snapshot post-processing ------------------------------------
    def _cap(self, text: str) -> str:
        if len(text) > self.text_cap:
            return text[: self.text_cap]
        return text

    def _to_snapshot(self, raw: dict[str, Any]) -> PageSnapshot:
        snap = PageSnapshot.from_dict(raw)
        if len(snap.text) > self.text_cap or len(snap.html_truncated) > self.text_cap:
            snap = PageSnapshot(
                url=snap.url,
                title=snap.title,
                text=self._cap(snap.text),
                html_truncated=self._cap(snap.html_truncated),
                status=snap.status,
            )
        return snap

    # -- public API --------------------------------------------------
    def browse(self, url: str) -> PageSnapshot:
        url = self._guard(url)
        return self._to_snapshot(self._call("browse", {"url": url}))

    def screenshot(self, url: str) -> bytes:
        import base64

        url = self._guard(url)
        result = self._call("screenshot", {"url": url})
        b64 = result.get("png_b64", "")
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            raise BrowserToolError(f"invalid screenshot payload: {exc}") from exc

    def extract(self, url: str, selector: str) -> list[str]:
        url = self._guard(url)
        result = self._call("extract", {"url": url, "selector": selector})
        matches = result.get("matches", [])
        if not isinstance(matches, list):
            raise BrowserToolError("worker returned non-list matches")
        return [str(m) for m in matches]

    def click(self, url: str, selector: str) -> PageSnapshot:
        url = self._guard(url)
        return self._to_snapshot(self._call("click", {"url": url, "selector": selector}))

    def fill(self, url: str, selector: str, value: str) -> PageSnapshot:
        url = self._guard(url)
        return self._to_snapshot(
            self._call("fill", {"url": url, "selector": selector, "value": value})
        )

    def close(self) -> None:
        if self._transport is None:
            return
        with self._lock:
            try:
                if self._transport.is_alive():
                    self._next_id += 1
                    payload = json.dumps({"id": self._next_id, "method": "shutdown", "params": {}})
                    try:
                        self._transport.send(payload)
                        self._transport.recv(min(self.call_timeout, 5.0))
                    except BrowserToolError:
                        pass
            finally:
                self._teardown()

    def __enter__(self) -> BrowserTool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
