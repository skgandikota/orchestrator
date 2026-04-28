# Codex integration

This guide walks you through pointing [OpenAI Codex CLI](https://github.com/openai/codex)
at a locally running `orchestrator` so that every Codex request is routed,
classified, and dispatched to whichever backend model the orchestrator's
profile system selects.

> **Audience:** developers who already have `orchestrator` installed (see
> [`README.md`](../../README.md)) and want Codex to use the local
> OpenAI-compatible endpoint instead of `api.openai.com`.

![Codex using orchestrator (image to be added)](img/codex-using-orchestrator.png)

---

## What is Codex and why integrate it?

Codex is OpenAI's command-line coding agent. It speaks the standard OpenAI
HTTP API (`/v1/chat/completions`, `/v1/models`), so any server that emulates
that surface can drive it. `orchestrator` exposes exactly such a surface
(see [#11 — OpenAI-compatible gateway](../PLAN.md#phase-3--openai-compatible-gateway)),
which means Codex becomes the third first-class consumer alongside
`claude-code` and `opencode`.

Routing Codex through `orchestrator` gives you:

- **Profile-based routing** — `orchestrator-fast` for quick edits,
  `orchestrator-deep` for hard refactors, plus the default classifier route.
- **Local inference fallback** — keep sensitive code off third-party APIs.
- **Unified observability** — every Codex turn shows up in the orchestrator
  logs alongside requests from other consumers.

---

## Prerequisites

| Tool | Tested version | Install |
|------|----------------|---------|
| `orchestrator` | `main` (Phase 7) | `pip install -e .` from this repo |
| `codex` | `0.10+` | `npm i -g @openai/codex` (or see Codex docs) |
| Python | `3.11+` | required by `orchestrator` |

Confirm Codex is on your `PATH`:

```pwsh
codex --version
```

---

## Step 1 — Start the orchestrator

From the repo root:

```pwsh
orchestrator serve --host 127.0.0.1 --port 8000
```

You should see a startup banner that ends with:

```
Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Note the port — the rest of this guide assumes `8000`. If you change it,
substitute that port everywhere `OPENAI_BASE_URL` appears below.

Sanity-check the gateway:

```pwsh
curl http://127.0.0.1:8000/v1/models
```

The response must list `orchestrator`, `orchestrator-fast`, and
`orchestrator-deep`. Codex requires the model id you pass on the command
line to appear here exactly — case and punctuation matter.

---

## Step 2 — Configure Codex

Codex reads, in order: environment variables, then its config file. Either
of the two options below is sufficient; pick one.

### Option A — environment variables (recommended for first run)

```pwsh
$env:OPENAI_BASE_URL = "http://localhost:8000/v1"
$env:OPENAI_API_KEY  = "local-no-auth"
```

The orchestrator does not validate the API key when bound to `localhost`,
but Codex refuses to start without one — `local-no-auth` is the canonical
placeholder we use across all integration docs.

### Option B — Codex config file

Codex stores its provider config at `~/.codex/config.toml` on macOS/Linux
and `%USERPROFILE%\.codex\config.toml` on Windows. Add an
`orchestrator` provider and make it the default:

```toml
# ~/.codex/config.toml
model         = "orchestrator"
model_provider = "orchestrator"

[model_providers.orchestrator]
name     = "Local orchestrator"
base_url = "http://localhost:8000/v1"
env_key  = "OPENAI_API_KEY"
wire_api = "chat"
```

Then export a placeholder key once per shell:

```pwsh
$env:OPENAI_API_KEY = "local-no-auth"
```

> **Other formats.** Older Codex builds also support a JSON config at the
> same path; the keys are identical. If you maintain both, env-vars win.

---

## Step 3 — Run a real task

Open a fresh terminal (so the env vars are picked up) and run a small
end-to-end exercise:

```pwsh
codex "Refactor src/utils.py: extract the retry loop into a helper, add type hints, and write a pytest case."
```

Codex will stream its plan, then its diff, then its tests. Because
`OPENAI_BASE_URL` points at `orchestrator`, every chunk you see was
produced by whichever backend the classifier selected.

To force a specific pipeline:

```pwsh
codex --model orchestrator-fast "Rename the variable foo to user_id across the repo."
codex --model orchestrator-deep "Design a plugin system for the CLI; output a PLAN.md section."
```

See [`docs/VS_LITELLM.md`](../VS_LITELLM.md) and the model-profiles section
of [`docs/PLAN.md`](../PLAN.md) for what each profile does.

---

## Step 4 — Verify routing

While Codex is running, the orchestrator log should show, for each turn:

```
INFO  POST /v1/chat/completions  model=orchestrator stream=true
INFO  classifier=heuristic decision=fast tokens_in=842
INFO  dispatch backend=<resolved-backend> profile=orchestrator-fast
INFO  stream first_token_ms=312 total_ms=4418
```

Three things to confirm:

1. **The request hit `/v1/chat/completions`** — if you only see
   `/v1/models`, Codex never sent the prompt; check `OPENAI_BASE_URL`.
2. **A classifier decision was logged** — confirms the profile pipeline
   ran rather than a passthrough.
3. **Streaming SSE was used** (`stream=true`) — Codex's UI depends on it.

You can replay the same call with `curl` to bypass Codex entirely:

```pwsh
curl -N http://127.0.0.1:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer local-no-auth" `
  -d '{"model":"orchestrator","stream":true,"messages":[{"role":"user","content":"hello"}]}'
```

You should see a stream of `data: {...}` SSE frames terminated by
`data: [DONE]`.

### Expected output (trimmed)

```
data: {"id":"chatcmpl-...","choices":[{"delta":{"role":"assistant"}}]}
data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"Hello"}}]}
data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"!"}}]}
data: {"id":"chatcmpl-...","choices":[{"finish_reason":"stop","delta":{}}]}
data: [DONE]
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `model 'orchestrator' not found` | Model id typo, or you typed `Orchestrator` | The id must match `/v1/models` byte-for-byte. |
| Codex hangs after the prompt, no tokens | SSE not streaming through a proxy | Disable corporate proxy for `localhost`, or set `NO_PROXY=localhost,127.0.0.1`. |
| Codex still calls `api.openai.com` | Cached provider from a previous session | Delete `~/.codex/auth.json` and re-export `OPENAI_BASE_URL` in a new shell. |
| `context length exceeded` | Backend model has a smaller window than the request | Switch profile (`--model orchestrator-deep`) or trim files Codex includes. |
| Server OOM / swap thrash | Local backend can't fit the chosen model | Pick a smaller model in the orchestrator profile, or raise host RAM. |
| 401 from orchestrator | You bound the server to a non-loopback host | Either rebind to `127.0.0.1` or set `OPENAI_API_KEY` to a real configured key. |

---

## Forcing a pipeline

Power users can pin a pipeline per invocation:

- `--model orchestrator-fast` — short context, latency-optimised.
- `--model orchestrator-deep` — long context, quality-optimised.
- `--model orchestrator` — default, classifier picks.

See [`docs/PLAN.md`](../PLAN.md) for the full profile matrix and
[`README.md`](../../README.md) for how Codex fits next to the other
integrations.
