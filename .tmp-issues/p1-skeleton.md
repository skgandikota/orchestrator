## Context

Part of the **Phase 1 — Foundations** epic (#1).
See [`docs/PLAN.md` § Phase 1](../blob/main/docs/PLAN.md#phase-1--foundations-prove-the-ram-story) for the bigger picture.

This is the very first issue: nothing else can land until the repo has a working Python package layout, a config loader, and structured logging. Every later module (`ram_monitor`, `scheduler`, `state`, `ollama_local`) imports from this skeleton, so getting the package name, settings keys, and log conventions right now avoids churn later. CONTRIBUTING.md fixes Python 3.11+, `ruff` for lint/format (line length 100), and `pytest` — this issue wires those tools in.

## Acceptance Criteria

- [ ] `pyproject.toml` declares package `orchestrator`, Python `>=3.11`, build backend (`hatchling` or `setuptools`), and runtime deps `psutil`, `tomli` (or stdlib `tomllib` on 3.11+), `pydantic>=2`, `structlog`.
- [ ] Dev deps under `[project.optional-dependencies] dev`: `pytest`, `pytest-cov`, `ruff`, `mypy`.
- [ ] `ruff` config in `pyproject.toml` sets `line-length = 100` and enables a sensible default rule set (`E`, `F`, `I`, `UP`, `B`).
- [ ] Repo layout matches `docs/PLAN.md § High-Level Architecture`: `orchestrator/{core,models,tools,interfaces,prompts,config}/__init__.py` and `tests/__init__.py` all exist (empty stubs OK).
- [ ] `orchestrator/config/settings.py` exposes `load_settings(path: Path | None = None) -> Settings` that reads `orchestrator/config/settings.toml`, validates via a `pydantic.BaseModel`, and supports env-var overrides of any key via `ORCHESTRATOR__SECTION__KEY` (double-underscore = nesting).
- [ ] `orchestrator/config/settings.toml` ships with default sections: `[ram]`, `[scheduler]`, `[ollama]`, `[logging]` (placeholder values are fine; later issues populate them).
- [ ] `orchestrator/core/logging.py` exposes `configure_logging(level: str = "INFO", json: bool = False) -> None` using `structlog` — JSON renderer when `json=True`, console renderer otherwise. Logs include timestamp, level, logger name, and event.
- [ ] `orchestrator/__init__.py` exports `__version__` (read from package metadata) and a top-level logger.
- [ ] `tests/test_settings.py` covers: defaults load, env-var override, missing-file error, invalid-type validation error.
- [ ] `tests/test_logging.py` asserts `configure_logging` is idempotent and respects the `json` flag.
- [ ] `ruff check .` and `ruff format --check .` are clean. `pytest` passes.

## Files / paths to touch

- `pyproject.toml` (new) — package metadata, deps, ruff/mypy/pytest config
- `orchestrator/__init__.py` (new) — package init, `__version__`
- `orchestrator/core/__init__.py`, `orchestrator/core/logging.py` (new)
- `orchestrator/models/__init__.py`, `orchestrator/tools/__init__.py`, `orchestrator/interfaces/__init__.py`, `orchestrator/prompts/__init__.py` (new, empty)
- `orchestrator/config/__init__.py`, `orchestrator/config/settings.py`, `orchestrator/config/settings.toml` (new)
- `tests/__init__.py`, `tests/test_settings.py`, `tests/test_logging.py` (new)
- `.gitignore` (new or modify) — exclude `.venv`, `__pycache__`, `*.egg-info`, `.pytest_cache`, `.ruff_cache`, `*.db`

## Suggested approach

Use `tomllib` from the stdlib on 3.11+ — no need to add `tomli`. Define one `Settings` pydantic model with nested submodels (`RamSettings`, `SchedulerSettings`, `OllamaSettings`, `LoggingSettings`) so later issues just add fields without breaking the loader. Implement env-var override by walking the nested model fields and looking up `ORCHESTRATOR__<SECTION>__<KEY>` (uppercased) before falling back to the TOML value; cast via pydantic's validators so types stay safe.

For logging, use `structlog.configure` with a `processors` chain that ends in `structlog.dev.ConsoleRenderer()` or `structlog.processors.JSONRenderer()`. Keep it simple — no file handlers, no rotation; downstream code just calls `structlog.get_logger(__name__)`. Make `configure_logging` idempotent by tracking a module-level "_configured" flag so tests calling it twice don't double-stack processors.

This issue is the foundation for the architectural rule "every step is a checkpoint" — later issues will rely on consistent structured logging to trace step boundaries. Do not add scheduler, RAM-monitor, or DB code here; keep it tightly scoped to package skeleton, settings, and logging.

## Dependencies

None — ready to start.

## Definition of Done

- [ ] Acceptance Criteria met
- [ ] Tests added and passing locally (`pytest tests/`)
- [ ] `ruff check` and `ruff format --check` clean
- [ ] Type hints on public surfaces
- [ ] PR body contains `Closes #<this-issue>`
- [ ] No deviation from `docs/PLAN.md` (or plan updated to reflect deviation)
- [ ] Architectural rules in `CONTRIBUTING.md` respected
