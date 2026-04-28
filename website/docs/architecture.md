---
sidebar_position: 2
title: Architecture
---

> Mirrored from the canonical [docs/PLAN.md](https://github.com/skgandikota/coracle/blob/main/docs/PLAN.md) in the repo. Edit there.

# Coracle — Implementation Plan

## Problem Statement
Build a personal-machine AI coracle (Mac M1 Pro, 16GB RAM) that intelligently splits work between **free-tier "big" cloud AI** (planning) and **local Ollama models** (reasoning + execution), without ever spiking RAM enough to crash the machine. Consumed by coding agents like Claude Code, opencode, and codex.

## Core Concept
```
User query (from opencode / Claude Code / codex via OpenAI-compatible /v1/chat/completions)
   ↓
[Local Reasoning Model — qwen2.5:7b, RESIDENT]
   ① CLASSIFY intent → { fast | deep | research | status }
       (tiny structured-output prompt, ~200ms, no big-AI call)
   ② Based on class, dispatch to one of the pipelines below.
   ↓
┌──────────────────── if class = status ────────────────────┐
│  Read SQLite job state → template (or 1.5B narrator)      │
│  → return immediately, never load coder                    │
└────────────────────────────────────────────────────────────┘
┌──────────────────── if class = fast ──────────────────────┐
│  Reasoning plans locally → coder executes → reasoning     │
│  verifies. No big-AI hop. RAM-friendly, low latency.       │
└────────────────────────────────────────────────────────────┘
┌──────────────── if class = deep / research ───────────────┐
│  Reasoning consolidates context + refines prompt          │
│       ↓                                                    │
│  Big AI (Gemini / Groq / Ollama Cloud / browser fallback) │
│       ↓                                                    │
│  Reasoning parses into checkpointed steps                 │
│       ↓                                                    │
│  Coder executes step-by-step using tool belt              │
│       ↓                                                    │
│  Reasoning verifies → continue / replan / done            │
│  (research class = same flow but biased toward web tools) │
└────────────────────────────────────────────────────────────┘
```
The classifier is the always-on front door. The user just talks to one model called `coracle` — routing is invisible.

## Key Architectural Decisions (confirmed with user)

| Area | Decision |
|------|----------|
| Language | Python |
| External interface | **OpenAI-compatible HTTP API** (`/v1/chat/completions`, `/v1/models`) **+ MCP server + native HTTP/CLI**, all sharing one core. The OpenAI-compatible endpoint is the primary integration path — it lets opencode, Claude Code, codex, Cursor, Continue, etc. treat the coracle as a drop-in "model" via `base_url=http://localhost:PORT/v1`. |
| Big-AI providers | Multi-provider via `litellm` + custom adapters: Gemini API, Groq API, Ollama Cloud, headless-browser fallback (Playwright) for Claude/ChatGPT/Gemini web |
| Local reasoning model | `qwen2.5:7b` (Ollama) — resident by default |
| Local coder model | `qwen2.5-coder:7b` (Ollama) — swapped in on demand |
| RAM strategy | **Single-LLM-slot scheduler**: only one 7B model resident at a time; coder runs in discrete checkpointed steps so swaps are safe |
| Status/interrupt | 3 modes selectable per-query: (a) instant DB-templated, (b) tiny 1.5B narrator (~1GB always resident, optional), (c) full reasoning synthesis at next checkpoint |
| Tool belt for coder | Filesystem, shell, web fetch/search, headless browser, git |
| State store | SQLite (`jobs`, `steps`, `messages`, `artifacts`) — durable job state; status queries hit DB first, zero RAM |

## High-Level Architecture

```
coracle/
├── core/
│   ├── classifier.py       # intent router on resident reasoning model
│   │                       #   in: user msg + brief context; out: {class, confidence, reason}
│   │                       #   structured-output prompt, fast (~200ms), cached for repeat queries
│   ├── scheduler.py        # single-LLM-slot enforcement, RAM watchdog
│   ├── state.py            # SQLite job/step/message store
│   ├── pipeline.py         # consolidate → refine → big-AI → parse → execute → verify
│   └── ram_monitor.py      # psutil-based, soft/hard limits, kill-switch
├── models/
│   ├── ollama_local.py     # load/unload, keep-alive control, generate
│   ├── big_ai/
│   │   ├── litellm_router.py  # Gemini, Groq, Ollama Cloud
│   │   ├── browser_driver.py  # Playwright headless: Claude.ai, ChatGPT, Gemini web
│   │   └── fallback.py     # provider preference order + retry/backoff
│   └── narrator.py         # optional always-resident small model
├── tools/                  # coder's tool belt
│   ├── fs.py  shell.py  web.py  browser.py  git.py
│   └── registry.py         # tool schemas + dispatch
├── interfaces/
│   ├── openai_compat.py    # /v1/chat/completions, /v1/models, /v1/completions — OpenAI-spec
│   │                       #   PRIMARY integration: opencode/Claude Code/codex point base_url here
│   │                       #   Exposes named "model" profiles (e.g. coracle-fast, coracle-deep)
│   │                       #   Streams SSE in OpenAI delta format
│   ├── mcp_server.py       # stdio MCP — alternate path, exposes job control as tools
│   ├── http_api.py         # Native FastAPI — /jobs, /jobs/{id}, /jobs/{id}/stream, /status
│   └── cli.py              # convenience CLI wrapping native HTTP
├── prompts/                # versioned prompt templates per phase
├── config/
│   └── settings.toml       # provider keys, model names, RAM limits, mode defaults
├── tests/
└── pyproject.toml
```

## Critical Design Rules
1. **One model name to the outside world: `coracle`.** The resident reasoning model auto-classifies and routes — users never pick a mode. Named profiles exist only as optional overrides.
2. **Never two 7B models loaded simultaneously.** Scheduler holds a mutex; swap = unload current → load next.
3. **Every coder step is a checkpoint.** Step boundary = safe swap point + DB write.
4. **Status queries never block on the scheduler** — classifier short-circuits to DB read.
5. **All long-running work is a "job"** with an ID; HTTP/MCP return job_id immediately, client polls or streams.
6. **RAM watchdog**: if free RAM < threshold, refuse new model loads, queue the request, surface to user.
7. **Provider fallback is automatic**: API quota exhausted → next API → browser driver as last resort.
8. **Browser drivers run as separate subprocesses** so their RAM is independent and killable.

## Phased Build

### Phase 1 — Foundations (prove the RAM story)
- Project skeleton, `pyproject.toml`, settings loader, structured logging
- SQLite state schema + migrations
- `ram_monitor` + `scheduler` with single-slot mutex
- `ollama_local` adapter with explicit load/unload + keep-alive control
- Smoke test: load qwen2.5:7b → unload → load qwen2.5-coder:7b → assert RAM never exceeds threshold

### Phase 2 — Big-AI providers
- `litellm_router` for Gemini, Groq, Ollama Cloud (real API keys)
- Provider preference + quota tracking + automatic fallback
- Playwright headless drivers for Claude.ai / ChatGPT / Gemini web (separate subprocess pool)
- Unified `BigAI.complete(prompt, prefer=[...])` interface

### Phase 3 — Pipeline
- **`classify` step (NEW, runs first on every request)**: resident reasoning model emits structured `{class, confidence, reason}` choosing among `fast | deep | research | status`. Confidence threshold + simple heuristics (e.g. message contains "status", "what's happening" → status) act as a cheap pre-filter to skip the LLM call entirely when obvious.
- `consolidate` step: gather workspace context, recent history, job state into structured bundle
- `refine` step: reasoning model produces a high-quality prompt for big AI
- `plan` step: big AI returns multi-step plan (JSON schema enforced)
- `parse` step: reasoning model normalizes plan into executable steps
- `execute` step: coder model runs one step using tool registry
- `verify` step: reasoning model checks result, decides continue/replan/done

### Phase 4 — Tool belt
- `fs`, `shell` (sandboxed to workspace dir, command allow/deny lists)
- `web` (fetch + search via DuckDuckGo/Brave API)
- `browser` (Playwright, separate process)
- `git` (local repo ops)
- **MCP client** — config-driven loader (`config/mcp_servers.yaml`) that
  surfaces tools from any number of remote/cloud MCP servers (GitHub,
  Atlassian, Context7, …) through the same registry as the built-in tools
- Tool schemas exposed to coder model in OpenAI-style function-calling format

### Phase 5 — Interfaces
- **OpenAI-compatible API (primary)** — `/v1/chat/completions` (stream + non-stream), `/v1/models`, `/v1/completions`
  - Each request is internally turned into a job; the response stream emits the full pipeline (classify→consolidate→refine→plan→execute→verify) as OpenAI-format `delta` chunks
  - **Default model: `coracle`** — single name, auto-routed. The resident reasoning model classifies every incoming request as `fast | deep | research | status` and dispatches to the right internal pipeline. The user/client never has to choose.
  - Named profiles exist only as **optional overrides** for power users/scripts that want to force a mode:
    - `coracle` (default, auto-router) ← what opencode/Claude Code/codex select
    - `coracle-fast` — force local-only, no big-AI hop
    - `coracle-deep` — force full pipeline with big-AI planning
    - `coracle-research` — force web/browser-heavy mode
    - `coracle-status` — force pure DB/narrator status mode
  - Drop-in usable from opencode, Claude Code (via OpenAI-compat shim), codex, Cursor, Continue, any OpenAI SDK
- **Native HTTP API** — `POST /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/stream` (SSE), `POST /jobs/{id}/status` (3 modes), `POST /jobs/{id}/cancel`
- **MCP server (stdio)** — `submit_job`, `get_status`, `stream_job`, `cancel_job` for clients that prefer tools over a model endpoint
- **CLI** wrapper around native HTTP

### Phase 6 — Status & interrupt UX
- Mode-a: DB-templated instant status
- Mode-b: optional `qwen2.5:1.5b` narrator (toggle in config)
- Mode-c: queued reasoning synthesis at next checkpoint
- Cancel + pause/resume via job-state flags read between coder steps

### Phase 7 — Hardening
- Quota/rate-limit tracking per provider with persistence
- Crash recovery: on startup, resume in-flight jobs from SQLite
- Prompt versioning + eval harness
- Docs + example consumer configs:
  - **opencode**: add provider with `base_url=http://localhost:PORT/v1`, any api_key, model=`coracle-deep`
  - **Claude Code**: via OpenAI-compat shim or MCP server registration
  - **codex / Cursor / Continue**: same OpenAI-compatible base_url pattern
  - Example commands and config snippets for each

## Open Questions / Things to Decide Later
- Exact RAM thresholds (will tune empirically in Phase 1 — propose: hard cap 11GB, soft cap 9GB)
- Whether to run Ollama as system service or coracle-managed subprocess
- Auth for HTTP API (probably localhost-only + token in v1)
- Final recommendation on consumer (Claude Code vs opencode vs codex) — defer to Phase 7 after testing all three against the MCP server

## Success Criteria
- **opencode (and Claude Code / codex) can be configured to use the coracle as a model** by pointing at `http://localhost:PORT/v1` — no code changes in those clients.
- A single query like *"Read this repo, find the slowest function, optimize it, run tests"* runs end-to-end using free APIs + local models with no RAM crash.
- User can interrupt mid-job to ask status and get an answer in <2s without crashing the coder.
- Switching big-AI provider (API → browser fallback) is transparent to the consumer.

## How opencode Will Use This (concrete flow)
1. Start coracle: `coracle serve` → listens on `http://localhost:8765`.
2. In opencode config, register a provider:
   ```
   providers:
     coracle:
       base_url: http://localhost:8765/v1
       api_key: local
   ```
3. Select model `coracle` (just one — no fast/deep/research choice for the user).
4. opencode sends a normal OpenAI chat-completions request → resident reasoning model classifies intent → coracle dispatches to the right internal pipeline (fast/deep/research/status) → streams results back in OpenAI delta format.
5. opencode displays it like any other model response — but under the hood it just used Gemini/Groq + qwen2.5 locally, picked the cheapest pipeline that fits, and stayed RAM-safe.

