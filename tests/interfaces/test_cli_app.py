"""Tests for the typer-based ``orchestrator`` CLI app."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from orchestrator.interfaces import cli as cli_mod
from orchestrator.interfaces.cli import (
    DEFAULT_BASE_URL,
    ENV_BASE_URL,
    _resolve_base_url,
    app,
)

runner = CliRunner(mix_stderr=False)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    """Replace ``cli._client`` with a client that uses a MockTransport."""
    captured: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_wrapped)

    def _factory(base_url: str, *, timeout: float | None = 30.0) -> httpx.Client:
        return httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    monkeypatch.setattr(cli_mod, "_client", _factory)
    return captured


# --------------------------------------------------------------------------- #
# Base-URL resolution                                                         #
# --------------------------------------------------------------------------- #
def test_resolve_base_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    assert _resolve_base_url(None) == DEFAULT_BASE_URL


def test_resolve_base_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "http://api.example.test:9000")
    assert _resolve_base_url(None) == "http://api.example.test:9000"


def test_resolve_base_url_flag_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "http://api.example.test:9000")
    assert _resolve_base_url("http://flag.example.test") == "http://flag.example.test"


# --------------------------------------------------------------------------- #
# Root callback / help                                                        #
# --------------------------------------------------------------------------- #
def test_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("serve", "mcp", "submit", "status", "stream", "cancel", "models"):
        assert sub in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    # typer/click exits with code 2 when no_args_is_help triggers
    assert result.exit_code in (0, 2)
    assert "Usage" in (result.stdout + result.stderr)


# --------------------------------------------------------------------------- #
# serve / mcp lazy entrypoints                                                #
# --------------------------------------------------------------------------- #
def test_serve_invokes_server_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake_module = MagicMock(run=fake)
    monkeypatch.setattr(
        cli_mod, "import_module", lambda name: fake_module if "server" in name else None
    )
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "9001"])
    assert result.exit_code == 0, result.stdout + result.stderr
    fake.assert_called_once_with(host="0.0.0.0", port=9001)


def test_serve_missing_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "import_module", lambda name: object())
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "run is not defined" in result.stderr


def test_mcp_invokes_mcp_server_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake_module = MagicMock(run=fake)
    monkeypatch.setattr(cli_mod, "import_module", lambda name: fake_module)
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 0, result.stdout + result.stderr
    fake.assert_called_once_with()


def test_mcp_missing_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "import_module", lambda name: object())
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 1
    assert "run is not defined" in result.stderr


# --------------------------------------------------------------------------- #
# submit                                                                      #
# --------------------------------------------------------------------------- #
def test_submit_prints_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/jobs"
        body = json.loads(request.content.decode())
        assert body == {"message": "do a thing"}
        return httpx.Response(200, json={"job_id": "j-123"})

    captured = _install_transport(monkeypatch, handler)
    result = runner.invoke(app, ["submit", "do a thing"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "j-123"
    assert len(captured) == 1


def test_submit_with_model_and_base_url_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.example.test"
        body = json.loads(request.content.decode())
        assert body == {"message": "go", "model": "llama3"}
        return httpx.Response(200, json={"job_id": "j-9"})

    _install_transport(monkeypatch, handler)
    result = runner.invoke(
        app,
        [
            "--base-url",
            "http://api.example.test:7000",
            "submit",
            "go",
            "--model",
            "llama3",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "j-9" in result.stdout


def test_submit_honors_env_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "http://from-env.test:8123")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "from-env.test"
        return httpx.Response(200, json={"job_id": "env-job"})

    _install_transport(monkeypatch, handler)
    result = runner.invoke(app, ["submit", "hi"])
    assert result.exit_code == 0
    assert "env-job" in result.stdout


def test_submit_missing_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda r: httpx.Response(200, json={"unexpected": True}))
    result = runner.invoke(app, ["submit", "x"])
    assert result.exit_code == 1
    assert "job_id" in result.stderr


def test_submit_with_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    sse_body = b"data: hello\n\ndata:  world\n\n: comment\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "s-1"})
        assert request.url.path == "/v1/jobs/s-1/stream"
        return httpx.Response(200, content=sse_body)

    _install_transport(monkeypatch, handler)
    result = runner.invoke(app, ["submit", "go", "--stream"])
    assert result.exit_code == 0, result.stdout + result.stderr
    out_lines = [ln for ln in result.stdout.splitlines() if ln]
    assert "s-1" in out_lines
    assert "hello" in out_lines
    assert "world" in out_lines


def test_submit_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    result = runner.invoke(app, ["submit", "x"])
    assert result.exit_code == 2
    assert "HTTP 500" in result.stderr


def test_submit_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    _install_transport(monkeypatch, handler)
    result = runner.invoke(app, ["submit", "x"])
    assert result.exit_code == 2
    assert "request failed" in result.stderr


# --------------------------------------------------------------------------- #
# status                                                                      #
# --------------------------------------------------------------------------- #
def test_status_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"job_id": "j-1", "status": "running"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/jobs/j-1"
        assert request.url.params.get("mode") is None
        return httpx.Response(200, json=payload)

    _install_transport(monkeypatch, handler)
    result = runner.invoke(app, ["status", "j-1"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == payload


def test_status_with_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("mode") == "b"
        return httpx.Response(200, json={"status": "ok"})

    _install_transport(monkeypatch, handler)
    result = runner.invoke(app, ["status", "j-1", "--mode", "b"])
    assert result.exit_code == 0


# --------------------------------------------------------------------------- #
# stream                                                                      #
# --------------------------------------------------------------------------- #
def test_stream_prints_data_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"data: one\n\ndata:two\n\n\ndata: three\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/j-2/stream"
        return httpx.Response(200, content=body)

    _install_transport(monkeypatch, handler)
    result = runner.invoke(app, ["stream", "j-2"])
    assert result.exit_code == 0, result.stdout + result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln]
    assert lines == ["one", "two", "three"]


def test_stream_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda r: httpx.Response(404, text="missing"))
    result = runner.invoke(app, ["stream", "nope"])
    assert result.exit_code == 2
    assert "HTTP 404" in result.stderr


def test_stream_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    _install_transport(monkeypatch, handler)
    result = runner.invoke(app, ["stream", "j"])
    assert result.exit_code == 2
    assert "stream failed" in result.stderr


# --------------------------------------------------------------------------- #
# cancel                                                                      #
# --------------------------------------------------------------------------- #
def test_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/jobs/abc/cancel"
        return httpx.Response(200, json={"ok": True})

    _install_transport(monkeypatch, handler)
    result = runner.invoke(app, ["cancel", "abc"])
    assert result.exit_code == 0
    assert "cancelled abc" in result.stdout


# --------------------------------------------------------------------------- #
# models                                                                      #
# --------------------------------------------------------------------------- #
def test_models_lists_data_field(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"data": [{"id": "m1"}, {"id": "m2"}]}
    _install_transport(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["m1", "m2"]


def test_models_lists_models_field_with_names(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"models": [{"name": "alpha"}, {"name": "beta"}]}
    _install_transport(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["alpha", "beta"]


def test_models_accepts_plain_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda r: httpx.Response(200, json=["x", "y"]))
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["x", "y"]


def test_models_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert "(no models)" in result.stdout
