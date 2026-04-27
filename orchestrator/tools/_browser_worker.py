"""Subprocess worker that owns the Playwright browser context.

Run as ``python -m orchestrator.tools._browser_worker``. Communicates with
the parent ``BrowserTool`` over stdio using line-delimited JSON-RPC.

Each request is a JSON object on a single line::

    {"id": 1, "method": "browse", "params": {"url": "https://example.com"}}

Each response is::

    {"id": 1, "result": {...}}
    {"id": 1, "error": {"type": "...", "message": "..."}}

Playwright is imported lazily on first use so that the parent process can
spawn this module without paying the import cost (and so tests can stub
the worker entirely without Playwright installed).
"""

from __future__ import annotations

import json
import sys
from typing import Any

DEFAULT_TEXT_CAP = 200 * 1024


class _Worker:
    def __init__(self, text_cap: int = DEFAULT_TEXT_CAP) -> None:
        self.text_cap = text_cap
        self._playwright = None
        self._browser = None
        self._context = None

    # -- lifecycle ----------------------------------------------------
    def _ensure_context(self):  # pragma: no cover - requires Playwright
        if self._context is not None:
            return self._context
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context()
        return self._context

    def _open(self, url: str):  # pragma: no cover - requires Playwright
        ctx = self._ensure_context()
        page = ctx.new_page()
        response = page.goto(url, wait_until="domcontentloaded")
        return page, response

    def _snapshot(self, page, response, url: str) -> dict[str, Any]:  # pragma: no cover
        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        if len(text) > self.text_cap:
            text = text[: self.text_cap]
        html = page.content()
        if len(html) > self.text_cap:
            html = html[: self.text_cap]
        return {
            "url": page.url or url,
            "title": page.title() or "",
            "text": text,
            "html_truncated": html,
            "status": response.status if response is not None else 0,
        }

    # -- handlers -----------------------------------------------------
    def browse(self, url: str) -> dict[str, Any]:  # pragma: no cover
        page, response = self._open(url)
        try:
            return self._snapshot(page, response, url)
        finally:
            page.close()

    def screenshot(self, url: str) -> dict[str, Any]:  # pragma: no cover
        import base64

        page, _ = self._open(url)
        try:
            png = page.screenshot(full_page=True)
        finally:
            page.close()
        return {"png_b64": base64.b64encode(png).decode("ascii")}

    def extract(self, url: str, selector: str) -> dict[str, Any]:  # pragma: no cover
        page, _ = self._open(url)
        try:
            handles = page.query_selector_all(selector)
            values = [(h.inner_text() or "") for h in handles]
        finally:
            page.close()
        return {"matches": values}

    def click(self, url: str, selector: str) -> dict[str, Any]:  # pragma: no cover
        page, response = self._open(url)
        try:
            page.click(selector)
            page.wait_for_load_state("domcontentloaded")
            return self._snapshot(page, response, url)
        finally:
            page.close()

    def fill(self, url: str, selector: str, value: str) -> dict[str, Any]:  # pragma: no cover
        page, response = self._open(url)
        try:
            page.fill(selector, value)
            return self._snapshot(page, response, url)
        finally:
            page.close()

    def shutdown(self) -> dict[str, Any]:  # pragma: no cover
        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
            if self._playwright is not None:
                self._playwright.stop()
        finally:
            self._context = None
            self._browser = None
            self._playwright = None
        return {"ok": True}

    # -- dispatch -----------------------------------------------------
    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        if method == "browse":
            return self.browse(params["url"])
        if method == "screenshot":
            return self.screenshot(params["url"])
        if method == "extract":
            return self.extract(params["url"], params["selector"])
        if method == "click":
            return self.click(params["url"], params["selector"])
        if method == "fill":
            return self.fill(params["url"], params["selector"], params["value"])
        if method == "shutdown":
            return self.shutdown()
        raise ValueError(f"unknown method: {method}")


def main() -> int:  # pragma: no cover - exercised only in real subprocess
    worker = _Worker()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(
                json.dumps({"id": None, "error": {"type": "ProtocolError", "message": str(exc)}})
                + "\n"
            )
            sys.stdout.flush()
            continue

        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        try:
            result = worker.dispatch(method, params)
            sys.stdout.write(json.dumps({"id": rid, "result": result}) + "\n")
        except Exception as exc:
            sys.stdout.write(
                json.dumps(
                    {
                        "id": rid,
                        "error": {"type": exc.__class__.__name__, "message": str(exc)},
                    }
                )
                + "\n"
            )
        sys.stdout.flush()

        if method == "shutdown":
            return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
