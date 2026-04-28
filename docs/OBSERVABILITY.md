# Observability — structured audit log

The coracle emits a single, append-only stream of structured events
to a local SQLite database. Every classifier decision, model swap, tool
call, big-AI call and error is recorded as one row in `audit_events`.

## Why

LiteLLM ships polished spend tracking because their users pay per token.
We don't pay per token but we still need to know **which provider
answered, how long the call took, and whether a guardrail intervened**.
That visibility is a hard prerequisite for quotas, guardrails and evals.

## Schema

`audit_events` (SQLite, append-only):

| column              | type    | notes                                    |
| ------------------- | ------- | ---------------------------------------- |
| `id`                | TEXT PK | UUIDv4 generated client-side             |
| `ts`                | TEXT    | ISO-8601 UTC                             |
| `actor`             | TEXT    | model name / tool name / "scheduler"     |
| `action`            | TEXT    | e.g. `model_swap`, `tool_call`, `error`  |
| `target`            | TEXT    | optional — what the actor acted on       |
| `status`            | TEXT    | `ok` / `error` / `warn`                  |
| `latency_ms`        | REAL    | optional                                 |
| `tokens_in`         | INT     | optional                                 |
| `tokens_out`        | INT     | optional                                 |
| `cost_estimate_usd` | REAL    | best-effort; `0` for browser/local       |
| `payload_json`      | TEXT    | optional, **truncated to 8 KB**          |

Indexes on `ts` and `(actor, action)` for fast tail / filter queries.

## Public API

```python
from coracle.observability import AuditLog, record, query

# Use the process-wide default (lazy in-memory until configured):
record("scheduler", "model_swap", target="llama3:8b",
       latency_ms=12.5, payload={"reason": "ram_pressure"})

for ev in query(actor="scheduler", limit=20):
    print(ev.ts, ev.action, ev.target)

# Or own the lifecycle yourself:
with AuditLog("/tmp/orch.sqlite") as log:
    log.record("router", "tool_call", target="fs.read", status="ok")
```

`record()` is **synchronous and fast**: it serialises the event in the
caller's thread, drops it onto a bounded in-memory queue and returns.
A daemon writer thread batches inserts to SQLite (and to the optional
OTel exporter).

## Queue overflow

The queue is bounded by `queue_size` (default 10 000). When full, the
**oldest** pending event is dropped so the producer never blocks. The
loss is visible two ways:

1. `AuditLog.dropped` is incremented on each drop.
2. A synthetic `audit / queue_overflow` event is persisted with the
   running `dropped_total` so dashboards can alert on it.

## Optional OpenTelemetry exporter

The OTel SDK is **not** a hard dependency. Install the extra and pass an
exporter to opt in:

```bash
pip install 'coracle[otel]'
```

```python
from coracle.observability import AuditLog, OTelExporter

exporter = OTelExporter(endpoint="http://localhost:4318/v1/traces")
log = AuditLog("/var/lib/orch/audit.sqlite", exporter=exporter)
```

Without the extra, constructing `OTelExporter` (without a test
`transport`) raises a clear `RuntimeError` so the misconfiguration
surfaces at startup, not on the first hot-path event.

## Example queries

```sql
-- Per-provider average latency in the last hour
SELECT actor, AVG(latency_ms) AS p_avg, COUNT(*) AS n
FROM audit_events
WHERE ts > datetime('now', '-1 hour')
GROUP BY actor
ORDER BY n DESC;

-- All errors in the last day
SELECT ts, actor, action, target, payload_json
FROM audit_events
WHERE status = 'error' AND ts > datetime('now', '-1 day')
ORDER BY ts DESC;
```
