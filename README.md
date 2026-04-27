# orchestrator

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

## Status

**🚧 Pre-alpha — design complete, implementation kicking off.**

Issues are organized into **7 phases** (Phase 1 → Phase 7) tracked via GitHub Milestones. Each phase has an Epic issue summarizing scope and linking to its sub-tasks.

This project is **agent-friendly**: every issue contains enough context, acceptance criteria, file paths, and definition-of-done that a coding agent (or human contributor) can pick it up cold, clone the repo, and submit a PR.

## How to contribute (humans and agents)

1. Pick a [ready issue](../../issues?q=is%3Aopen+is%3Aissue+label%3Astatus%3Aready) (label: `status:ready`) — these have no unresolved dependencies.
2. Read the issue's **Context**, **Acceptance Criteria**, and **Definition of Done**.
3. Reference [`docs/PLAN.md`](docs/PLAN.md) for the bigger picture.
4. Open a PR linking the issue (`Closes #N`).
5. Follow [`CONTRIBUTING.md`](CONTRIBUTING.md).

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

## License

Licensed under the **Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License** ([CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)).

[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc-sa/4.0/)

You are free to **share** and **adapt** the material under these terms:

- **Attribution** — credit the original author and link to the license.
- **NonCommercial** — no commercial use.
- **ShareAlike** — distribute derivative works under the same license.

See [`LICENSE`](LICENSE) for the full legal text.
