"""Top-level command-line interface for the coracle.

This is a thin client of the local HTTP API exposed by
:mod:`coracle.interfaces.server`.  It is intentionally small: each
subcommand either delegates to a long-running server entry point
(``serve``/``mcp``) or talks to the HTTP API over :mod:`httpx`.

The module also re-exports a few operator-facing helpers
(:func:`list_recoverable`, :func:`resume_job`, :func:`cancel_job`) used by
the recovery surface in :mod:`coracle.core.recovery`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from importlib import import_module
from typing import Annotated, Any

import httpx
import typer

from coracle.core.recovery import Job, StateStore, cancel, resume

__all__ = [
    "DEFAULT_BASE_URL",
    "ENV_BASE_URL",
    "app",
    "cancel_job",
    "list_recoverable",
    "resume_job",
]

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
ENV_BASE_URL = "CORACLE_URL"

app = typer.Typer(
    name="coracle",
    help="Thin CLI client of the local coracle HTTP API.",
    no_args_is_help=True,
    add_completion=False,
)


# --------------------------------------------------------------------------- #
# Operator helpers (kept for backwards-compatibility with recovery surface).  #
# --------------------------------------------------------------------------- #
def list_recoverable(state: StateStore) -> list[Job]:
    """Return every job currently in the ``recoverable`` state."""
    jobs: Iterable[Job] = state.list_recoverable()
    return list(jobs)


def resume_job(state: StateStore, job_id: str) -> None:
    resume(state, job_id)


def cancel_job(state: StateStore, job_id: str) -> None:
    cancel(state, job_id)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _resolve_base_url(override: str | None) -> str:
    if override:
        return override
    return os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)


def _get_base_url(ctx: typer.Context) -> str:
    obj = ctx.obj or {}
    return str(obj.get("base_url", DEFAULT_BASE_URL))


def _client(base_url: str, *, timeout: float | None = 30.0) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=timeout)


def _abort(message: str, code: int = 1) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=code)


def _request_json(
    method: str,
    base_url: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    try:
        with _client(base_url) as client:
            resp = client.request(method, path, json=json_body, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        _abort(f"HTTP {exc.response.status_code}: {exc.response.text.strip()}", code=2)
    except httpx.HTTPError as exc:
        _abort(f"request failed: {exc}", code=2)


# --------------------------------------------------------------------------- #
# Root callback (global options)                                              #
# --------------------------------------------------------------------------- #
@app.callback()
def _root(
    ctx: typer.Context,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            help=(
                "Base URL of the coracle HTTP API. "
                f"Falls back to ${ENV_BASE_URL} or {DEFAULT_BASE_URL}."
            ),
        ),
    ] = None,
) -> None:
    ctx.obj = {"base_url": _resolve_base_url(base_url)}


# --------------------------------------------------------------------------- #
# Server / MCP entrypoints (lazy imports)                                     #
# --------------------------------------------------------------------------- #
@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Bind port.")] = 8000,
) -> None:
    """Run the FastAPI/uvicorn HTTP server."""
    try:
        server = import_module("coracle.interfaces.server")
    except ImportError as exc:  # pragma: no cover - import error is environment-specific
        _abort(f"server module unavailable: {exc}")
    runner = getattr(server, "run", None)
    if runner is None:
        _abort("coracle.interfaces.server.run is not defined")
    runner(host=host, port=port)


@app.command()
def mcp() -> None:
    """Run the MCP stdio server."""
    try:
        mcp_server = import_module("coracle.interfaces.mcp_server")
    except ImportError as exc:  # pragma: no cover
        _abort(f"mcp module unavailable: {exc}")
    runner = getattr(mcp_server, "run", None)
    if runner is None:
        _abort("coracle.interfaces.mcp_server.run is not defined")
    runner()


# --------------------------------------------------------------------------- #
# HTTP-talking subcommands                                                    #
# --------------------------------------------------------------------------- #
@app.command()
def submit(
    ctx: typer.Context,
    message: Annotated[str, typer.Argument(help="Goal/message to submit.")],
    model: Annotated[
        str | None, typer.Option("--model", help="Override the default model id.")
    ] = None,
    stream: Annotated[
        bool, typer.Option("--stream", help="Stream the SSE output after submission.")
    ] = False,
) -> None:
    """POST a new job and print its ``job_id`` (optionally tailing output)."""
    base = _get_base_url(ctx)
    payload: dict[str, Any] = {"message": message}
    if model is not None:
        payload["model"] = model
    data = _request_json("POST", base, "/v1/jobs", json_body=payload)
    job_id = data.get("job_id") if isinstance(data, dict) else None
    if not job_id:
        _abort("response did not contain a job_id")
    typer.echo(job_id)
    if stream:
        _stream_job(base, str(job_id))


@app.command()
def status(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Job id returned by ``submit``.")],
    mode: Annotated[str | None, typer.Option("--mode", help="Detail mode: a, b, or c.")] = None,
) -> None:
    """Print the status payload for ``job_id``."""
    params = {"mode": mode} if mode else None
    data = _request_json("GET", _get_base_url(ctx), f"/v1/jobs/{job_id}", params=params)
    typer.echo(json.dumps(data, indent=2, sort_keys=True))


@app.command()
def stream(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Job id to tail.")],
) -> None:
    """Tail the SSE stream of ``job_id`` to stdout."""
    _stream_job(_get_base_url(ctx), job_id)


@app.command(name="cancel")
def cancel_cmd(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Job id to cancel.")],
) -> None:
    """Cancel a running job."""
    _request_json("POST", _get_base_url(ctx), f"/v1/jobs/{job_id}/cancel")
    typer.echo(f"cancelled {job_id}")


@app.command()
def models(ctx: typer.Context) -> None:
    """List the models exposed by the local API (``GET /v1/models``)."""
    data = _request_json("GET", _get_base_url(ctx), "/v1/models")
    items: list[Any] = []
    if isinstance(data, dict):
        items = list(data.get("data") or data.get("models") or [])
    elif isinstance(data, list):
        items = data
    if not items:
        typer.echo("(no models)")
        return
    for item in items:
        if isinstance(item, dict):
            typer.echo(str(item.get("id") or item.get("name") or item))
        else:
            typer.echo(str(item))


def _stream_job(base_url: str, job_id: str) -> None:
    """Tail the SSE endpoint and print every ``data:`` payload."""
    try:
        with (
            _client(base_url, timeout=None) as client,
            client.stream("GET", f"/v1/jobs/{job_id}/stream") as resp,
        ):
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    typer.echo(line[len("data:") :].lstrip())
    except httpx.HTTPStatusError as exc:
        _abort(f"HTTP {exc.response.status_code}: {exc.response.text.strip()}", code=2)
    except httpx.HTTPError as exc:
        _abort(f"stream failed: {exc}", code=2)


if __name__ == "__main__":  # pragma: no cover
    app()
