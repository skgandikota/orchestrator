# Using `orchestrator` with **opencode**

This guide wires the [opencode](https://github.com/opencode-ai/opencode) open-source
terminal coding assistant to a locally-running `orchestrator` instance via the
OpenAI-compatible HTTP endpoint shipped in Phase 5 (#11).

The whole walkthrough should take **under five minutes** on a fresh machine.

> **What is opencode?** A free, open-source CLI for AI-assisted coding. It
> speaks the OpenAI Chat Completions wire format, so any server that exposes
> `/v1/chat/completions` and `/v1/models` can be plugged in as a provider.
>
> **Why orchestrator?** opencode by itself just relays prompts to a single
> model. `orchestrator` runs the full **classify → refine → execute → verify**
> pipeline on top of local Qwen models, giving opencode users multi-step
> reasoning, model swapping, and offline operation — all behind the same
> OpenAI-compatible URL opencode already knows how to call.

> **Placeholder screenshot:** `docs/integrations/img/opencode-using-orchestrator.png`
> *(image to be added)*

---

## Prerequisites

- `orchestrator` installed and importable (`pip install -e .` from the repo root).
- Local Qwen models pulled and runnable (see `docs/PLAN.md` § Phase 2).
- Node.js ≥ 18 (opencode is distributed via npm).
- ~12 GB free RAM on Apple Silicon / 16 GB on x86 for the default profile.

Install opencode globally:

```bash
npm install -g opencode-ai
# verify
opencode --version
```

If `opencode-ai` is not the current published name on your system, see the
[opencode README](https://github.com/opencode-ai/opencode#install) for the
latest install command — the rest of this guide is install-method agnostic.

---

## Step 1 — Start `orchestrator serve`

```bash
orchestrator serve --port 8000
```

You should see a log line similar to:

```
INFO     orchestrator.server  Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO     orchestrator.server  models registered: orchestrator, orchestrator-fast, orchestrator-deep
```

Read the bound port either from this log line or from the `--port` flag you
passed. The rest of this guide assumes `8000`. If the port is already in use,
pick another with `--port 8123` and substitute it everywhere below.

Sanity-check the endpoint with `curl`:

```bash
curl -s http://localhost:8000/v1/models | jq
```

Expected shape:

```json
{
  "object": "list",
  "data": [
    { "id": "orchestrator", "object": "model", "owned_by": "local" },
    { "id": "orchestrator-fast", "object": "model", "owned_by": "local" },
    { "id": "orchestrator-deep", "object": "model", "owned_by": "local" }
  ]
}
```

If you do not see the `orchestrator` id, jump to **Troubleshooting → model not listed**.

---

## Step 2 — Configure opencode

opencode reads its config from:

- **Linux / WSL:** `~/.config/opencode/config.json`
- **macOS:** `~/.config/opencode/config.json` (XDG path; opencode also accepts
  `~/Library/Application Support/opencode/config.json` on some builds —
  prefer the XDG one for portability).
- **Windows:** `%APPDATA%\opencode\config.json`

Add (or merge) the following block:

```json
{
  "provider": "openai-compatible",
  "providers": {
    "orchestrator-local": {
      "type": "openai-compatible",
      "base_url": "http://localhost:8000/v1",
      "api_key": "local-no-auth",
      "models": ["orchestrator"]
    }
  },
  "model": "orchestrator-local/orchestrator"
}
```

### Why is `api_key` required if it is unused?

The OpenAI client libraries that opencode is built on top of refuse to send a
request without an `Authorization: Bearer …` header. `orchestrator` ignores the
value entirely (it is a local-only server with no auth), but opencode will hard-
error before the request is dispatched if the field is missing. Any non-empty
string works — `local-no-auth` is just a self-documenting placeholder.

> **Quirk:** opencode caches the model list at startup. **Restart opencode** any
> time you edit `config.json` or add a new model id, otherwise the new entry
> will not appear in the picker.

---

## Step 3 — Select the `orchestrator` model

### CLI flag (one-shot)

```bash
opencode --model orchestrator-local/orchestrator "summarize this repo"
```

### Interactive UI

Launch opencode without arguments and press the model-picker hotkey
(`Ctrl+M` in current builds), then choose **orchestrator-local/orchestrator**
from the list.

### Optional: forcing a pipeline

`orchestrator` exposes two power-user model ids alongside the default:

| Model id              | Behavior                                                |
|-----------------------|---------------------------------------------------------|
| `orchestrator`        | Auto-classify; pipeline depth chosen per request.       |
| `orchestrator-fast`   | Skip refine/verify — single-shot, lowest latency.       |
| `orchestrator-deep`   | Full pipeline + extra verification pass.                |

Swap `models: ["orchestrator"]` to `["orchestrator", "orchestrator-fast", "orchestrator-deep"]`
in `config.json` to expose all three. See `docs/model-profiles.md` for the
underlying Qwen mapping (`qwen2.5-coder` for code, `qwen2.5` for reasoning).

---

## Step 4 — Verify end-to-end

In one terminal, keep `orchestrator serve` running with `--log-level info`.
In another:

```bash
opencode --model orchestrator-local/orchestrator "hello, summarize this repo"
```

You should see opencode stream tokens back, and `orchestrator`'s log should
show the classify → refine → execute → verify trace:

```
INFO  orchestrator.pipeline  classify   route=code-summary  model=qwen2.5-coder
INFO  orchestrator.pipeline  refine     tokens_in=412  tokens_out=87
INFO  orchestrator.pipeline  execute    streaming=true
INFO  orchestrator.pipeline  verify     ok=true
INFO  orchestrator.server    POST /v1/chat/completions 200  duration_ms=… stream=true
```

If you see all four pipeline stages and a `200` on `/v1/chat/completions`,
the integration is working.

A direct `curl` reproduction (handy for scripting / CI smoke-tests):

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer local-no-auth" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "orchestrator",
    "messages": [{"role": "user", "content": "hello, summarize this repo"}],
    "stream": false
  }' | jq '.choices[0].message.content'
```

---

## Troubleshooting

### `connection refused` / `ECONNREFUSED`

The server is not running, or it bound to a different port.

```bash
# is anything listening on 8000?
curl -v http://localhost:8000/v1/models
# or
lsof -iTCP:8000 -sTCP:LISTEN
```

Restart with `orchestrator serve --port 8000` and confirm the log line shows
the same port you put in `config.json`.

### `401 Unauthorized`

opencode dropped the `Authorization` header — almost always because `api_key`
is missing or empty in `config.json`. Set it to any non-empty string
(`local-no-auth`) and restart opencode.

### Model not listed in the picker

1. Confirm the server actually advertises it:
   ```bash
   curl -s http://localhost:8000/v1/models | jq '.data[].id'
   ```
   You should see `orchestrator` in the output.
2. Confirm `config.json` parses (opencode silently ignores malformed configs):
   ```bash
   jq . ~/.config/opencode/config.json
   ```
3. **Restart opencode** — the model list is cached at process start.

### Slow first response on Apple Silicon (M1/M2 with 16 GB)

The first request loads the Qwen weights into RAM; subsequent calls are fast.
If you are hitting swap:

- Use `orchestrator-fast` to keep only one model resident.
- Lower context window via `orchestrator serve --max-context 4096`.
- Close other RAM-heavy apps (Chrome, Docker Desktop, simulators).

### Long pause when switching between code and reasoning prompts

`orchestrator` swaps between `qwen2.5-coder` and `qwen2.5` based on classify
output. On constrained hardware this swap can take several seconds. Pin a
single profile via `orchestrator-fast` (always coder) or set
`ORCHESTRATOR_PIN_MODEL=qwen2.5-coder` in the server's environment.

### Falling back to a hosted free-tier provider

If local inference is unavailable (no GPU, low RAM, travel), keep your
`orchestrator-local` provider block and add a second free-tier provider
alongside it in `config.json`. opencode lets you switch providers per-session
with `--model <provider>/<model>`, so your workflow stays identical — only the
backend changes.

---

## See also

- Project overview and quick-start: [`README.md`](../../README.md)
- Phase plan and roadmap: [`docs/PLAN.md`](../PLAN.md)
- Model profile reference: [`docs/model-profiles.md`](../model-profiles.md)
- Sibling integration guides: `docs/integrations/claude-code.md`,
  `docs/integrations/codex.md`
