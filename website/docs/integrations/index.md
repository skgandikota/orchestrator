---
sidebar_position: 4
title: Integrations
---

# Integrations

The coracle exposes an **OpenAI-compatible** `/v1/chat/completions` endpoint, so any client that supports a configurable `base_url` can plug in.

> Per-integration walkthroughs (opencode, Claude Code, codex, Cursor, Continue, custom MCP servers) will land here as they ship. Track progress in the [Phase 7 epic](https://github.com/skgandikota/coracle/issues/7).

## Pattern

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1
export OPENAI_API_KEY=anything-non-empty
```

Then point your tool of choice at model name `coracle`. Routing between free-tier big-AI and local Ollama is invisible to the caller.

## MCP servers

External MCP servers (stdio / http / sse) are wired via `config/mcp_servers.yaml`. See the [README](https://github.com/skgandikota/coracle#wiring-external-mcp-servers) for the current shape.
