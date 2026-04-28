# orchestrator

[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc-sa/4.0/)
[![Status: Pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange.svg)](#status)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Platform: macOS (Apple Silicon)](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-lightgrey.svg)](#hardware-target)
[![Agent-friendly](https://img.shields.io/badge/agent--friendly-yes-brightgreen.svg)](CONTRIBUTING.md)

> A personal-machine AI orchestrator that intelligently splits work between **free-tier "big" cloud AI** (planning) and **local Ollama models** (reasoning + execution), without ever spiking RAM enough to crash the machine. Built to be consumed as a drop-in OpenAI-compatible "model" by [opencode](https://github.com/sst/opencode), [Claude Code](https://github.com/anthropics/claude-code), [codex](https://github.com/openai/codex), Cursor, Continue, etc.

## Why this exists

Big AI models are great at planning. Small local models are great at executing. Free API tiers run out. Browser-driven web AIs are flaky. RAM on a 16GB Mac is precious. None of the existing tools combine all of these gracefully — so this one does:

- **Resident reasoning model** (`qwen2.5:7b`) classifies every request and routes it to the right pipeline.
- **Big AI** (Gemini, Groq, Ollama Cloud, headless-browser fallback to Claude.ai/ChatGPT/Gemini-web) handles deep planning when the classifier asks for it.
- **Coder model** (`qwen2.5-coder:7b`) executes steps locally with a full tool belt (fs, shell, web, browser, git).
- **Single-LLM-slot scheduler** ensures only one 7B model is in RAM at a time.
- **SQLite job state** powers instant status responses with zero RAM cost.
- **One model name to the consumer:** `orchestrator`. Auto-routing is invisible.

## Architecture at a glance

```
opencode / Claude Code / codex
            │  (OpenAI-compatible /v1/chat/completions)
            ▼
┌─────────────────────────────────────────────────────────────┐
│ Resident reasoning model (qwen2.5:7b) — CLASSIFIER          │
│  → fast | deep | research | status                           │
└─────────────────────────────────────────────────────────────┘
            │
   ┌────────┼────────┬─────────────────┐
   ▼        ▼        ▼                 ▼
 status   fast      deep             research
 (DB     (local-   (reason →         (deep + web
  read)  only)     big AI →          tools biased)
                   parse →
                   coder →
                   verify)
```

Full design details: [`docs/PLAN.md`](docs/PLAN.md).

## Integrations

Per-tool how-to guides for plugging orchestrator into the coding agents
that consume it as either an MCP server or an OpenAI-compatible model:

| Tool | Guide | Status |
|---|---|---|
| Claude Code | [`docs/integrations/claude-code.md`](docs/integrations/claude-code.md) | ✅ documented |
| opencode | _coming via #23_ | 🚧 placeholder |
| codex | _coming via #25_ | 🚧 placeholder |

## How is this different from LiteLLM?

Short version: **LiteLLM is a paid-API gateway built for throughput; `orchestrator` is a personal-machine scheduler built for $0 budgets and a 16GB RAM ceiling.** We use LiteLLM's SDK as our provider abstraction, but the product is a different thing entirely — see [`docs/VS_LITELLM.md`](docs/VS_LITELLM.md) for the full table.

| | LiteLLM | `orchestrator` |
|---|---|---|
| Cost model | Pay-per-token | $0 — free tiers + local + headless-browser fallback |
| Topology | Stateless proxy | Stateful job orchestrator |
| Inference | Cloud-first | Local-first |
| RAM target | Server-class | 16GB Mac M1 |
| Tool execution | Caller's job | Orchestrator runs the tools (sandbox + MCP) |
| Status / progress | None | First-class, never loads an LLM |

## Status

**🚧 Pre-alpha — implementation underway.**

Skeleton (package layout, settings loader, structured logging) landed in #31.

Issues are organized into **7 phases** (Phase 1 → Phase 7) tracked via GitHub Milestones. Each phase has an Epic issue summarizing scope and linking to its sub-tasks.

This project is **agent-friendly**: every issue contains enough context, acceptance criteria, file paths, and definition-of-done that a coding agent (or human contributor) can pick it up cold, clone the repo, and submit a PR.

## How to contribute (humans and agents)

1. Pick a [ready issue](../../issues?q=is%3Aopen+is%3Aissue+label%3Astatus%3Aready) (label: `status:ready`) — these have no unresolved dependencies.
2. Read the issue's **Context**, **Acceptance Criteria**, and **Definition of Done**.
3. Reference [`docs/PLAN.md`](docs/PLAN.md) for the bigger picture.
4. Open a PR linking the issue (`Closes #N`).
5. Follow [`CONTRIBUTING.md`](CONTRIBUTING.md).
6. PRs are reviewed by a layered AI bot stack — see [`docs/REVIEW_BOTS.md`](docs/REVIEW_BOTS.md). Only our strict `code-reviewer-001` bot has merge authority; it waits for the AI bots to weigh in before approving.

## Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| Local models | Ollama (`qwen2.5:7b`, `qwen2.5-coder:7b`) |
| Big AI providers | `litellm` → Gemini, Groq, Ollama Cloud + Playwright headless fallback |
| External interface | OpenAI-compatible HTTP (primary) + MCP stdio + native HTTP + CLI |
| Server | FastAPI + Uvicorn |
| State | SQLite |
| Browser | Playwright (headless, separate subprocess per provider) |
| RAM monitor | psutil |

## Hardware target

Mac M1 Pro, 16 GB RAM. Designed to never exceed ~11 GB resident.

## Wiring external MCP servers

The orchestrator can consume any number of remote/cloud MCP servers as
local tools. Copy the example config and edit it:

```bash
cp config/mcp_servers.yaml.example config/mcp_servers.yaml
# edit config/mcp_servers.yaml — supports stdio | http | sse transports
orchestrator mcp list      # show connected servers + tool counts
orchestrator mcp reload    # re-read the config without restarting
```

Environment variables in the config (e.g. `${GITHUB_TOKEN}`) are expanded
at load time, so secrets stay out of source control.

## License

Licensed under the **Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License** ([CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)).

[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc-sa/4.0/)

You are free to **share** and **adapt** the material under these terms:

- **Attribution** — credit the original author and link to the license.
- **NonCommercial** — no commercial use.
- **ShareAlike** — distribute derivative works under the same license.

See [`LICENSE`](LICENSE) for the full legal text.
