## Context

Part of the **Phase 3 — Pipeline** epic (#3).
See [`docs/PLAN.md` § Phase 3](../blob/main/docs/PLAN.md#phase-3--pipeline).

The classifier is on the hot path of every request, so prompt regressions silently degrade the entire system. This task builds a hand-curated dataset of ~50 labeled prompts and a pytest harness that scores classifier accuracy against it. The harness gates prompt or model changes — promoting an updated `classify.md` requires meeting a configurable accuracy threshold (default 0.90).

## Acceptance Criteria

- [ ] Dataset at `tests/evals/classifier_dataset.yaml` with ≥ 50 entries, each `{prompt: str, expected_class: "fast"|"deep"|"research"|"status", notes: str}`.
- [ ] Dataset is balanced — at least 8 examples per class, including edge cases (e.g. status-like wording that's actually a deep request).
- [ ] Pytest module `tests/evals/test_classifier_eval.py` loads the dataset, runs `classify()` against a real local Ollama, and computes per-class precision/recall + overall accuracy.
- [ ] Tests are marked `@pytest.mark.ollama` and skipped by default when env var `RUN_OLLAMA_EVALS` is unset.
- [ ] Threshold is configurable via env var `CLASSIFIER_MIN_ACCURACY` (default `0.90`); harness asserts overall accuracy ≥ threshold.
- [ ] On failure, harness prints a confusion matrix and lists every misclassified prompt with expected vs actual class.
- [ ] Prompt file `orchestrator/prompts/classify.md` carries a `version:` header (e.g. `version: 1`) so eval reports include it.
- [ ] README snippet under `tests/evals/README.md` explains how to run the eval locally.

## Files / paths to touch

- `tests/evals/classifier_dataset.yaml` (new)
- `tests/evals/test_classifier_eval.py` (new)
- `tests/evals/README.md` (new)
- `tests/evals/__init__.py` (new, empty)
- `orchestrator/prompts/classify.md` (add `version:` front-matter)
- `pyproject.toml` or `pytest.ini` (register the `ollama` marker)

## Suggested approach

Write the dataset in YAML for human readability; load with `pyyaml`. Each entry is a dict — keep `notes` as a freeform field explaining the labeling rationale (this becomes self-documenting when prompts change). Aim for class distribution roughly 12/12/12/12 with the remaining 2 reserved for tricky boundary cases like "what's the status of the deep refactor I asked for yesterday" (status, not deep).

The harness should iterate the dataset, call `classify()` once per prompt against the actual local Ollama (so the `ollama` marker is required). Collect predictions in a list of `(expected, actual, prompt)` tuples, compute counts, and use `collections.Counter` to build the confusion matrix. Report results via `pytest`'s standard output and only assert at the end. This way a single run produces a full report whether it passes or fails.

Make the threshold env-driven so CI can later run a "soft" check (e.g. 0.85) on PRs and a "hard" check (0.92) on main. Keep the harness separate from `tests/core/test_classifier.py` (which uses mocked Ollama) — the eval is opt-in and slow.

## Dependencies

- Blocked by todo `p3-classify`

## Definition of Done
- [ ] Acceptance Criteria met
- [ ] Tests added and passing (with mocked models — no live LLM calls in CI)
- [ ] `ruff check` clean
- [ ] Type hints on public surfaces
- [ ] PR body contains `Closes #<this-issue>`
- [ ] Architectural rule respected: every step writes a checkpoint to SQLite before yielding
