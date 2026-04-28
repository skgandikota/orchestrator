# Prompt evaluation harness

The eval harness lives under [`evals/`](../evals) and is intentionally
decoupled from `coracle.models` / `coracle.providers` so that
suites can run against any callable model — a real local model, a hosted
provider, or an in-process stub.

## Running a suite

```bash
# Run a single suite via the package CLI:
python -m evals run evals/suites/baseline.yaml --fake-client

# Run a named suite or every suite via the project script:
python scripts/run_evals.py classify
python scripts/run_evals.py --all
```

The runner prints a per-case `PASS`/`FAIL` line and a summary like
`classify: 5/5 (v1)`. A Markdown report is written to
`reports/evals-<timestamp>.md`. The process exits non-zero when any
suite falls below its declared `min_pass_rate`, making the harness safe
to use as a CI gate.

## Adding a case

1. Pick (or create) a YAML suite under `evals/suites/`.
2. Append a new entry to `cases:`. Supported fields:

   | Field | Purpose |
   | --- | --- |
   | `name` | Stable identifier shown in reports |
   | `prompt` | Input passed to the model |
   | `expected_substrings` | All must appear in the response |
   | `forbidden_substrings` | None may appear in the response |
   | `expected_regex` | Each pattern must match somewhere |
   | `expected_intent` | Compared to `ModelResponse.intent` |
   | `json_schema` | JSON Schema validated against the response text |
   | `classification_label` | Reserved for richer classification scorers |
   | `no_leak` | Fail if PII / secret regexes appear |
   | `max_latency_ms` | Fail if the call exceeds this budget |
   | `min_confidence` | Fail if `ModelResponse.confidence` is lower |

3. Run the suite locally to make sure it passes against the stub model
   client before opening a PR.

## Ollama-backed cases

Live cases that hit a local Ollama instance are marked with
`@pytest.mark.ollama` and are *excluded* from the default `pytest`
invocation — they are opt-in via `pytest -m ollama`. Offline cases are
the default and use canned outputs from `FakeModelClient`.

## Bumping a prompt version

Every file in `coracle/prompts/` carries a `# version: N` header
(integer, monotonically increasing). The loader in
`coracle/prompts/_loader.py` parses that header and exposes
`prompt.version`. To bump:

1. Edit the prompt file.
2. Increment the integer on the `# version:` header line.
3. Re-run the relevant eval suite — the report records the prompt
   version for each suite so regressions are attributable to a specific
   template revision.

If the header is missing or malformed the loader fails fast with a
`ValueError`, which keeps the contract obvious without introducing a
prompt-registry service.
