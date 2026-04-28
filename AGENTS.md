# AGENTS.md — rules for AI coding agents

This file is the operating manual for autonomous AI coding agents (Claude
Code, Copilot CLI, opencode, codex, Cursor, Continue, fleet runners, etc.)
working on `skgandikota/coracle`. Humans should read
[`CONTRIBUTING.md`](CONTRIBUTING.md) instead — this page is the agent-only
addendum.

If anything here conflicts with `CONTRIBUTING.md`, `CONTRIBUTING.md` wins.

## Prime directive

**You are the implementer, not the adviser.** Your job is to land working
code that closes the issue, not to summarize options or ask the user to
choose. Make a defensible decision, write the code, prove it works, open the
PR.

## Picking an issue

1. Filter to [`agent-friendly`](../../issues?q=is%3Aopen+is%3Aissue+label%3Aagent-friendly)
   `+ status:ready`. Skip anything labeled `status:blocked`.
2. Read the **entire** issue body. Note every Acceptance Criterion and the
   "Files / paths to touch" list — that list is your file-scope contract.
3. If the issue says "Blocked by #N" and #N is not closed, stop and pick a
   different issue. Do not invent dependencies.
4. Comment `/take` to claim it.

## Implementation contract

Every PR you open must:

- **Implement every Acceptance Criterion.** Partial PRs are rejected.
- **Stay within file scope.** Only modify files listed in the issue's
  "Files / paths to touch". If you must deviate, justify it explicitly in
  the PR body.
- **Use Conventional Commits** (`feat:`, `fix:`, `docs:`, `chore:`, `test:`,
  `refactor:`, `ops:`, `ci:`, `build:`, `perf:`). Subject ≤72 chars,
  imperative mood. The PR title is the squash-merge commit.
- **Sign off every commit** with `git commit -s` (DCO). CI rejects missing
  `Signed-off-by:` trailers.
- **GPG- or SSH-sign every commit.** `main` enforces `required_signatures`.
- **Keep coverage ≥95%.** Run `pytest --cov=coracle --cov-fail-under=95`
  before pushing. New code without tests fails CI.
- **Run lint locally.** `ruff check .` and `ruff format --check .` must be
  clean.
- **Include `Closes #<issue>`** in the PR body so the issue auto-closes.
- **Tick every checkbox** in [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md).

## Forbidden

- **Do not add a `Co-authored-by: Copilot …` trailer.** This repo's review
  stack flags it as a credit-stuffing signal. Sign off as yourself (or as
  the configured agent identity) and stop there.
- **Do not commit secrets.** Push protection blocks the obvious cases;
  scanners (`gitleaks`, `semgrep`) catch the rest. Never paste an API key,
  even in a test fixture.
- **Do not touch unrelated files.** Drive-by refactors, formatting sweeps,
  and "while I was here" cleanups belong in their own issue.
- **Do not bypass branch protection.** No force-pushes to `main`, no
  admin-merge, no rewriting history on a shared branch.
- **Do not weaken the architectural rules** in `CONTRIBUTING.md`
  (single-LLM slot, status-without-LLM, `job_id` contract, browser
  drivers as subprocesses, no caller-side provider hardcoding, RAM cap).
  If a rule is in your way, open a discussion issue first.

## Branching & PR mechanics

```bash
git checkout -b <type>/<issue>-<slug>     # e.g. feat/42-ram-watchdog
# … implement …
git add -A
git commit -s -S -m "feat: add RAM watchdog (#42)"
git push -u origin <branch>
gh pr create \
  --title "feat: add RAM watchdog" \
  --body "Closes #42

Acceptance Criteria met."
```

The PR body must include both `Closes #<issue>` and the literal phrase
`Acceptance Criteria met` (case-insensitive) — automation greps for them.

## `bot-review-bypass` for config-/docs-only PRs

PRs that touch **only** documentation, GitHub metadata, or non-code config
(e.g. `*.md`, `.github/**`, `config/**.example`, `Makefile`, `DCO`) may
apply the `bot-review-bypass` label so the heavy AI review stack stands
down. Apply it at PR-creation time:

```bash
gh pr create --label bot-review-bypass --label documentation \
  --title "docs: …" --body "Closes #N

Acceptance Criteria met."
```

You are personally responsible for honoring the bypass: if your diff
touches **any** file under `coracle/` or `tests/`, remove the label
and let the bots review. Misuse is grounds for the strict reviewer to
block-merge the PR.

## Backing off when blocked

If you cannot make forward progress:

1. Comment on the issue summarizing what you tried, what failed, and the
   exact error or decision point.
2. Add label `status:blocked` and remove `status:in-progress`.
3. Stop. Do not open a half-done PR. Do not invent scope.

## File-scope discipline checklist

Before pushing, run:

```bash
git diff --name-only origin/main...HEAD
```

Cross-reference that list against the issue's "Files / paths to touch".
Anything extra needs an explicit justification in the PR body or it gets
reverted.
