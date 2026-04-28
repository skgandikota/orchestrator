# Claude Code ↔ coracle integration

[Claude Code](https://github.com/anthropics/claude-code) is Anthropic's
official terminal-native coding agent. `coracle` plugs into it two
different ways, and you should pick based on **what you want Claude Code to
do with the coracle**:

| You want… | Use | Why |
|---|---|---|
| Claude Code to drive coracle **jobs** as first-class tools (submit, watch, cancel) — the agent stays "Claude" but it can hand long-running work to your local scheduler. | **Path A — MCP** | Tool-call ergonomics, streaming, cancellation, status all built in. |
| Claude Code to **treat coracle itself as the model** — every chat turn flows through the coracle's classifier → router → local/big-AI pipeline. | **Path B — OpenAI-compat shim** | Zero-API-key local routing, falls back to free-tier big AI for deep reasoning. |

The two paths are not mutually exclusive — you can register the MCP server
and point Claude Code at the shim base URL at the same time. The "When to
switch paths" section at the bottom covers mixed setups.

> **Pre-alpha note.** Both paths depend on issues that are still in flight:
> Path A on the MCP stdio entrypoint (#17), Path B on the OpenAI-compatible
> server (#11). Commands and env-var names below match the design in
> [`docs/PLAN.md`](../PLAN.md); update this doc as those land.

---

## Prerequisites

- Claude Code installed and authenticated:
  ```bash
  npm install -g @anthropic-ai/claude-code
  claude --version
  claude login
  ```
- `coracle` cloned, installed, and runnable from your shell (`coracle --help` works).
- macOS Apple Silicon, 16 GB RAM target — same envelope the coracle itself targets ([`README.md` § Hardware target](../../README.md#hardware-target)).

---

## Path A — MCP (preferred for job-control tools)

Claude Code speaks the [Model Context Protocol](https://modelcontextprotocol.io)
natively. The coracle ships an MCP stdio server (`coracle mcp`)
that exposes job-control as tools the agent can call mid-conversation.

### A.1 Register the server

Run, from any directory:

```bash
claude mcp add coracle -- coracle mcp
```

Claude Code writes this entry into its MCP registry
(`~/.config/claude-code/mcp.json` on macOS / Linux):

```json
{
  "mcpServers": {
    "coracle": {
      "command": "coracle",
      "args": ["mcp"],
      "env": {}
    }
  }
}
```

> Pin the binary path explicitly (e.g. `/Users/you/.venvs/coracle/bin/coracle`)
> if you run multiple coracle checkouts side-by-side — Claude Code
> launches the server with whatever `PATH` it inherits, which can surprise you.

### A.2 Verify the handshake

```bash
claude mcp list
```

Expected output (truncated):

```
coracle    stdio    ● connected    4 tools
  ├─ submit_job
  ├─ get_status
  ├─ stream_job
  └─ cancel_job
```

If it shows `✗ failed` or `0 tools`, jump to [Troubleshooting](#troubleshooting).

### A.3 The four MCP tools

| Tool | What it does |
|---|---|
| `submit_job` | Queue a new job (prompt + optional pipeline hint `fast` / `deep` / `research`). Returns a `job_id` immediately — never blocks on an LLM. |
| `get_status` | Cheap point-in-time read of a job's state from SQLite. Safe to poll. |
| `stream_job` | Server-sent stream of tokens + tool events for a running job. |
| `cancel_job` | Cooperative cancel — interrupts the current step at the next safe checkpoint. |

### A.4 Example session

Inside Claude Code:

```
> Use the coracle to refactor src/auth/*.py into a single module,
  and stream the work back to me.
```

Claude Code's tool trace will look like:

```
● coracle.submit_job(prompt="refactor src/auth/*.py …", pipeline="deep")
  → { "job_id": "j_01HZ…" }
● coracle.stream_job(job_id="j_01HZ…")
  ⟶ [classifier] deep
  ⟶ [big-ai]    plan: 1) inventory imports …
  ⟶ [coder]     editing src/auth/__init__.py …
  ⟶ [verify]    pytest -q  → 24 passed
  ✓ done
```

### A.5 Verification recipe

1. `claude mcp list` shows `coracle` connected with 4 tools.
2. In Claude Code, ask: *"Submit a fast job that prints the current time and stream the result."*
3. You should see a `submit_job` tool call, then `stream_job` events, then a final assistant message containing the time.

---

## Path B — OpenAI-compatible shim (preferred for "treat it as the model")

Claude Code is, by default, hard-wired to Anthropic's API. To make it talk
to a local OpenAI-compatible endpoint you have two options, in order of
preference:

### B.1 Native base-URL override (when available)

If your Claude Code build supports it, set:

```bash
export ANTHROPIC_API_BASE_URL=http://localhost:8000/v1
export ANTHROPIC_API_KEY=local-no-auth
claude
```

`coracle` ignores the API key — local server, no auth — but Claude
Code refuses to start without one, so any non-empty string works.

> **Reality check.** As of this writing Claude Code's first-class support
> for non-Anthropic endpoints is limited. Confirm with `claude --help` /
> the upstream changelog before relying on this; if it is not honoured,
> use the LiteLLM workaround below.

### B.2 LiteLLM proxy workaround

When Claude Code refuses to be redirected, run a one-line LiteLLM proxy
that masquerades as the Anthropic API and forwards to coracle:

```bash
pip install 'litellm[proxy]'
litellm --model openai/coracle \
        --api_base http://localhost:8000/v1 \
        --api_key local-no-auth \
        --port 4000
```

Then point Claude Code at the proxy:

```bash
export ANTHROPIC_API_BASE_URL=http://localhost:4000
export ANTHROPIC_API_KEY=local-no-auth
claude
```

**Trade-offs:**

- Adds a hop (Claude Code → LiteLLM → coracle → local/big-AI).
- LiteLLM does its own translation between Anthropic's `messages` schema
  and OpenAI's `chat/completions` — edge cases (tool calls, vision) may
  be lossy. File upstream issues against LiteLLM, not coracle.
- The proxy is a separate process you must keep running.

### B.3 Verification recipe

1. Start coracle: `coracle serve --port 8000`.
2. (If using B.2) start the LiteLLM proxy on port 4000.
3. Launch `claude`. Ask: *"What model are you?"*
4. Expected: a streaming reply that comes from the coracle's local
   `qwen2.5:7b` (fast pipeline) or, for a more demanding question like
   *"Design a SQLite schema for a job queue with retries"*, you should
   see a noticeably longer first-token latency as the request fans out
   to a free-tier big-AI provider before streaming back.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `claude mcp list` shows `✗ failed` | `coracle` binary not on `PATH` for the shell Claude Code spawns. | Re-register with absolute path: `claude mcp add coracle -- /abs/path/coracle mcp`. |
| MCP server connects but `0 tools` | Stdio handshake mismatch — server crashed before `tools/list`. | Run `coracle mcp` manually in a terminal; you should see a JSON-RPC banner. Check for an import error on startup. |
| Server not appearing in Claude Code at all | Edited `mcp.json` by hand and broke JSON. | `claude mcp list` will print a parse error; fix the file or re-run `claude mcp add`. |
| OpenAI shim returns 404 on `/v1/models` | coracle's models endpoint not yet enabled (#11). | Either upgrade coracle, or set Claude Code's model name explicitly so it never lists. |
| Claude Code ignores `ANTHROPIC_API_BASE_URL` | Build doesn't support the override. | Switch to the [LiteLLM workaround](#b2-litellm-proxy-workaround). |
| `RAM near limit, evicting model` warnings on M1 16 GB | Multiple 7B models loaded; only one slot is allowed. | Confirm `OLLAMA_MAX_LOADED_MODELS=1` and that no other Ollama clients are pinning a model. See [`docs/PLAN.md` § Single-LLM-slot scheduler](../PLAN.md). |
| `model not found: qwen2.5:7b` | Ollama models not pulled. | `ollama pull qwen2.5:7b && ollama pull qwen2.5-coder:7b`. |
| Quota exhausted on big-AI provider | Free-tier limit hit (Gemini / Groq / Ollama Cloud). | Coracle should auto-fall-through to the next provider, then to the headless-browser fallback. If it doesn't, check provider keys in your env and the `[providers]` block of your config. |

---

## When to switch paths

- Start with **Path A (MCP)** if you mostly want Claude Code's reasoning
  but want to offload long / repetitive / RAM-heavy work to coracle
  as discrete jobs.
- Switch to **Path B (shim)** when you want every turn — including the
  cheap ones — to go through the coracle's classifier so you can
  cap cost and RAM globally rather than per-tool-call.
- Run **both** when you want coracle to be the model *and* expose
  job-control to the agent (e.g. so the agent can fire-and-forget a long
  research job while continuing to chat).

---

## Screenshots

> _Image to be added._ Placeholder paths under
> [`docs/integrations/img/`](./img/):
>
> - `claude-mcp-list.png` — terminal output of `claude mcp list` with coracle connected.
> - `claude-tool-call.png` — Claude Code mid-conversation invoking `submit_job`.
> - `claude-shim-stream.png` — streaming chat completion via the shim path.

---

## See also

- [`README.md`](../../README.md) — project overview and integrations index.
- [`docs/PLAN.md`](../PLAN.md) — full architecture, including the MCP
  entrypoint (#17) and OpenAI-compat server (#11) this guide depends on.
- [`docs/VS_LITELLM.md`](../VS_LITELLM.md) — why coracle and LiteLLM
  are different products even though Path B uses LiteLLM as a stopgap.
