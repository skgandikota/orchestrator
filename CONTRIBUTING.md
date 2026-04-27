# Contributing to orchestrator

Welcome! This project is **agent-friendly** — issues are written so that humans *and* coding agents (Claude Code, opencode, codex, Cursor, etc.) can pick them up cold and submit a working pull request.

## TL;DR for coding agents

1. Pick an open issue labeled `status:ready` (no unresolved dependencies).
2. Read the issue body — every issue includes **Context**, **Files to touch**, **Acceptance Criteria**, and **Definition of Done**.
3. Read [`docs/PLAN.md`](docs/PLAN.md) for the architecture.
4. Clone, branch, code, test, PR.
5. Use the branch name pattern: `<issue-number>-<short-slug>` (e.g. `12-ram-monitor`).
6. PR title pattern: `[#<issue>] <type>: <short summary>` (e.g. `[#12] feat: psutil RAM watchdog`).
7. PR body must include `Closes #<issue-number>` so the issue auto-closes on merge.
8. Keep PRs small and focused on one issue.

## Workflow

```bash
# 1. Pick an issue (assume #12)
gh issue view 12

# 2. Branch
git checkout -b 12-ram-monitor

# 3. Code + tests
# ... make your changes ...

# 4. Run checks (once tooling lands in Phase 1)
ruff check .
ruff format --check .
pytest

# 5. Commit, push, PR
git add -A
git commit -m "[#12] feat: psutil RAM watchdog with soft/hard caps"
git push -u origin 12-ram-monitor
gh pr create --fill --body "Closes #12"
```

## Issue labels — quick reference

**Phase** (which milestone this belongs to):
`phase:1-foundations` · `phase:2-providers` · `phase:3-pipeline` · `phase:4-tools` · `phase:5-interfaces` · `phase:6-status` · `phase:7-hardening`

**Type** (what kind of work):
`type:feat` · `type:test` · `type:docs` · `type:chore` · `type:research` · `type:bug`

**Area** (which subsystem):
`area:core` · `area:models` · `area:tools` · `area:interfaces` · `area:prompts` · `area:config`

**Priority**:
`priority:p0-blocker` · `priority:p1-high` · `priority:p2-normal` · `priority:p3-low`

**Status**:
`status:ready` (no blockers, pick me up!) · `status:blocked` (waiting on a dependency) · `status:in-progress`

**Special**:
`good-first-issue` · `help-wanted` · `agent-friendly` · `epic`

## Code style

- **Python 3.11+**.
- Format with `ruff format` (PEP 8, line length 100).
- Lint with `ruff check`.
- Type hints on all public functions; check with `mypy` in strict mode for new modules where possible.
- Docstrings: short, useful — no boilerplate. Document non-obvious behavior, not signatures.
- Tests use `pytest`. Aim for behavior-level tests, not implementation-bound.

## Project structure

See [`docs/PLAN.md`](docs/PLAN.md#high-level-architecture). In short:

```
orchestrator/
├── core/         # scheduler, state, pipeline, ram_monitor, classifier
├── models/       # ollama_local, big_ai/, narrator
├── tools/        # fs, shell, web, browser, git, registry
├── interfaces/   # openai_compat, mcp_server, http_api, cli
├── prompts/      # versioned prompt templates
└── tests/
```

## Testing rules

- **Unit tests** for pure logic — no model calls, no network.
- **Integration tests** that need Ollama: mark with `@pytest.mark.ollama` and skip by default in CI.
- **Integration tests** that need API keys: mark with `@pytest.mark.live` and skip by default.
- Mock all big-AI providers via `litellm` mock backends in unit tests.
- **Coverage gate:** `pytest --cov=orchestrator --cov-fail-under=95` is enforced in CI. PRs that drop coverage below 95% will fail. Use `# pragma: no cover` only for genuinely-untestable branches (e.g. real subprocess fork paths).

## Signed commits (mandatory)

The `main` branch enforces `required_signatures` via a repository ruleset. **Every commit must be GPG- or SSH-signed**, otherwise push and merge are blocked.

One-time setup:

```bash
# Option A: GPG
gpg --full-generate-key                          # ed25519 or rsa4096
gpg --list-secret-keys --keyid-format=long       # copy KEYID
git config --global user.signingkey <KEYID>
git config --global commit.gpgsign true
git config --global tag.gpgsign true
gpg --armor --export <KEYID>                     # paste at https://github.com/settings/gpg/new

# Option B: SSH (simpler, uses existing ~/.ssh/id_ed25519)
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
# upload the .pub at https://github.com/settings/ssh/new (key type = "Signing Key")
```

Verify:
```bash
git log --show-signature -1
gh api /repos/skgandikota/orchestrator/commits/<sha> --jq '.commit.verification'
# expect: verified=true, reason=valid
```

Bots (GitHub Apps) sign automatically with GitHub's web-flow signature when committing via the API. No setup needed for the strict reviewer's squash-merge.

## Definition of Done (applies to every issue)

A PR is mergeable when:

1. Code implements the **Acceptance Criteria** in the issue.
2. Tests for the new behavior exist and pass locally (`pytest`).
3. `ruff check` and `ruff format --check` pass.
4. **Coverage stays at or above 95%** (`--cov-fail-under=95` in CI).
5. **Every commit on the branch is signed** (`verified=true` on GitHub).
6. Public APIs have type hints.
7. Docs/`docs/PLAN.md` updated **only if the change deviates from the plan** — otherwise leave the plan alone.
8. PR body says `Closes #<issue-number>`.

## Architectural rules of the road

These are non-negotiable for any change:

1. **Never load two 7B models simultaneously.** Always go through `core.scheduler`.
2. **Every coder step is a checkpoint** — write progress to SQLite before yielding the slot.
3. **Status queries must not require an LLM** by default — read from SQLite first.
4. **All long-running work is a job with an ID.** APIs return `job_id` immediately; clients stream/poll.
5. **Provider fallback is automatic** — no caller should hardcode a provider.
6. **Browser drivers are separate subprocesses** — never in-process with the main app.
7. **One external model name: `orchestrator`.** The classifier picks the pipeline, not the user.

If your change conflicts with one of these, open a discussion issue before coding.

## Questions?

Open a [Question issue](../../issues/new?template=question.yml) or comment on the issue you're working on. We'll respond.
