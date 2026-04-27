## Context

Part of the **Phase 3 — Pipeline** epic (#3).
See [`docs/PLAN.md` § Phase 3](../blob/main/docs/PLAN.md#phase-3--pipeline).

The classifier is the very first step of every job. It decides whether to route the request to the `fast`, `deep`, `research`, or `status` pipeline. It runs on the resident reasoning model (`qwen2.5:7b` via Ollama) using structured output, with a cheap regex pre-filter to short-circuit obvious status queries without an LLM call. The result is logged to job state for downstream steps and observability.

## Acceptance Criteria

- [ ] `ClassifyResult` Pydantic model with fields `class_: Literal["fast","deep","research","status"]` (aliased `class`), `confidence: float` (0..1), `reason: str`.
- [ ] `classify(user_msg: str, *, ollama: OllamaClient) -> ClassifyResult` is implemented in `orchestrator/core/classifier.py`.
- [ ] Regex pre-filter matches patterns like `^\s*(status|what'?s? happening|progress|where are we)\b` (case-insensitive) and returns `class="status"`, `confidence=1.0`, `reason="regex pre-filter"` without calling Ollama.
- [ ] Otherwise, calls `qwen2.5:7b` with structured-output (JSON schema) using the prompt at `orchestrator/prompts/classify.md`.
- [ ] Invalid / non-conforming model output is retried once, then falls back to `class="deep", confidence=0.0, reason="classifier fallback"`.
- [ ] Result is persisted to job state via the Phase 1 state module (one row / event per classification).
- [ ] Unit tests cover: each regex shortcut class, a happy-path mocked Ollama response per class, a malformed JSON retry case, and the fallback path.
- [ ] No live LLM calls in CI — `OllamaClient` is mocked.

## Files / paths to touch

- `orchestrator/core/classifier.py` (new)
- `orchestrator/prompts/classify.md` (new — versioned prompt with header `# classify v1`)
- `tests/core/test_classifier.py` (new)
- `orchestrator/core/__init__.py` (export `classify`, `ClassifyResult` if appropriate)

## Suggested approach

Define `ClassifyResult` with `pydantic.BaseModel`, using `Field(alias="class")` for the class field and `model_config = ConfigDict(populate_by_name=True)` so callers can use `class_`. Build a tiny `_REGEX_RULES` table that maps compiled patterns to a class name; iterate once and return early if any matches. This keeps the pre-filter cheap and easy to extend.

For the LLM path, load the prompt file once at module import, format it with the user message, and call `ollama.structured(model="qwen2.5:7b", schema=ClassifyResult, prompt=...)`. Wrap the call in a single retry loop that catches `pydantic.ValidationError` and JSON decode errors. On second failure, return the deterministic fallback (`deep`, 0.0). Log every decision (including pre-filter hits) into job state so the eval harness in `p3-classify-evals` can later grade live behavior.

Keep this module pure — no scheduler, no pipeline orchestration; the caller (job runner) is responsible for invoking `classify` and writing the result event. This makes it trivially mockable in downstream tests.

## Dependencies

- Blocked by todo `p1-ollama`
- Blocked by todo `p1-state`

## Definition of Done
- [ ] Acceptance Criteria met
- [ ] Tests added and passing (with mocked models — no live LLM calls in CI)
- [ ] `ruff check` clean
- [ ] Type hints on public surfaces
- [ ] PR body contains `Closes #<this-issue>`
- [ ] Architectural rule respected: every step writes a checkpoint to SQLite before yielding
