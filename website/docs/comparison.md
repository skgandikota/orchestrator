---
sidebar_position: 3
title: Comparison vs LiteLLM
---

> Mirrored from [docs/VS_LITELLM.md](https://github.com/skgandikota/coracle/blob/main/docs/VS_LITELLM.md). Edit there.

# How `coracle` differs from LiteLLM

[LiteLLM](https://github.com/BerriAI/litellm) is an excellent, commercially-backed **AI Gateway**. We use its Python SDK as our provider abstraction layer (#8). But the **product** is a different thing entirely. This page exists so contributors and users can understand the line in 30 seconds.

## TL;DR

| | **LiteLLM** | **`coracle`** |
|---|---|---|
| **Audience** | Teams / orgs running paid LLM workloads at scale | One person on a laptop, paying nothing |
| **Cost model** | Pay-per-token via your provider keys | $0 — free tiers + local Ollama + headless-browser fallback |
| **Topology** | Stateless proxy / gateway | Stateful coracle with a job lifecycle |
| **Inference location** | Cloud-first; local is just-another-provider | Local-first; cloud is just-the-planner |
| **RAM model** | Server-class (32GB+); concurrency is the goal | 16GB Mac M1; concurrency would crash the box |
| **Request shape** | One call → one provider → one response | One call → classify → consolidate → refine → big-AI plan → parse → local execute → verify |
| **What sees the LLM** | Every request | Only after a local classifier decides the request needs one |
| **Tool execution** | Returns the tool-call JSON; caller executes | Coracle **executes** tools (fs, shell, git, browser, MCP) inside a sandbox |
| **Failure mode for "free quota exhausted"** | Caller's problem | Automatic provider fallback, then headless-browser fallback to web UIs |
| **Status / progress UX** | None — proxy is stateless | First-class: status query never loads an LLM |
| **MCP** | Gateway: proxy upstream MCPs to LLMs (#45 parity here) | Same gateway shape **plus** coracle-as-MCP-server (#17) and config-driven MCP-client (#45) |
| **A2A** | First-class agent gateway | Out of scope (single-agent personal tool) |
| **Multi-tenancy** | Virtual keys, spend tracking, admin dashboard | Single-user; localhost-bound by default |
| **Latency target** | 8 ms P95 at 1k RPS | Doesn't matter; correctness > throughput |

## What LiteLLM does that we deliberately do **not** do

- **Virtual keys / RBAC / admin dashboard** — single-user tool, irrelevant
- **A2A agent protocol** — out of scope
- **Embeddings, image, audio, batch, rerank endpoints** — only chat completions for now (passthrough is tracked as #56 but P3)
- **Sub-10ms P95 routing** — we are bottlenecked by classifier latency anyway
- **Enterprise guardrails (Lakera, Aporia, etc.)** — we ship a thin local guardrail layer (#55), no commercial integrations
- **100+ providers** — we curate ~5 free-tier providers; LiteLLM's SDK gets us the rest if anyone needs them

## What LiteLLM does that we **adopted** (or should)

- **OpenAI-compatible API** as the primary surface — yes (#11)
- **Drop-in `base_url` swap** — yes
- **Provider abstraction** — yes, via `litellm` SDK as a dependency (#8)
- **MCP gateway shape** (`tools[].type="mcp"` in `/v1/chat/completions`) — yes, **#56**
- **Spend-equivalent observability** — yes, but for free-tier *quota* not money — **#54** (audit log) + #20 (quota tracking)
- **Guardrails / prompt-injection** — yes, **#55** (local-only, no SaaS dependencies)
- **Streaming SSE** — yes (#11)

## What we do that LiteLLM does **not** (and architecturally cannot)

1. **Single-LLM-slot scheduler** with hard RAM ceiling (#34, #33). LiteLLM never holds an LLM resident — it never had to solve this.
2. **Two-model split** (reasoning 7B + coder 7B, never co-resident) (#35, p3-*).
3. **Local classifier auto-router** — the user sees one model name; intent classification happens locally before any cloud call (#37).
4. **Prompt-refinement pipeline** — local model consolidates context and rewrites the prompt before posting upstream (#39, #40).
5. **Headless-browser fallback** to Claude.ai / ChatGPT / Gemini-web when API quotas are exhausted (#9). This is explicitly out of scope for any commercial gateway because it sits in a ToS grey area; for a personal tool it's fair use of the user's own session.
6. **Job lifecycle with checkpoints** — every coder step writes to SQLite before yielding the LLM slot, so a crash is recoverable (#21, #32).
7. **Status without an LLM** — three-tier status mode (DB-templated → 1.5B narrator → queued reasoning) so progress checks never force a 2nd 7B load (#12, #14, #16).
8. **Sandboxed tool execution** for fs/shell/git/browser (#26-#29).
9. **Free-tier quota bookkeeping** persisted across restarts — knows it has 1500 Gemini RPD and counts down (#20).
10. **CC BY-NC-SA license** — explicitly non-commercial. LiteLLM is MIT + Enterprise tier.

## Bottom line

LiteLLM is the right answer if you have a budget, a fleet of paid LLM keys, and need ten thousand requests per second routed through one endpoint with audit and RBAC.

`coracle` is the right answer if you have **one laptop**, **zero budget**, **two 7B local models**, and want a coding agent that *just works* by spending free-tier credits intelligently and falling back to a browser when those run out.

They're complementary, not competing. We import `litellm` as a library; LiteLLM users will never need us.

