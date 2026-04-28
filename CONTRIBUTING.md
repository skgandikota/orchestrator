# Contributing to `coracle`

Welcome — this project is **agent-friendly**. Every issue is written so that
humans *and* coding agents (Claude Code, Copilot CLI, opencode, codex, Cursor,
Continue, …) can pick it up cold and submit a working pull request.

This guide is the single source of truth for how work flows from issue → PR →
merge. If something here disagrees with another file in the repo, this file
wins; please open an issue to reconcile.

## Project overview

`coracle` is a personal-machine AI coracle that splits work between
free-tier "big" cloud models (planning) and local Ollama models
(reasoning + execution) without spiking RAM. See [`README.md`](README.md) for
the high-level pitch and [`docs/PLAN.md`](docs/PLAN.md) for the full design.

## Code of Conduct

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md). Report unacceptable behavior via the
contact listed there.

## Quick start

```bash
git clone https://github.com/skgandikota/coracle.git
cd coracle
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
make test
```

Full local-development walkthrough — Ollama setup, browser drivers,
environment variables, debugging tips — lives in
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Issue workflow

1. Browse the [`agent-friendly`](../../issues?q=is%3Aopen+is%3Aissue+label%3Aagent-friendly)
   or [`status:ready`](../../issues?q=is%3Aopen+is%3Aissue+label%3Astatus%3Aready)
   filters — these issues have no unresolved blockers.
2. Comment **`/take`** on the issue to claim it (prevents duplicate work).
3. Read the issue end-to-end. Every issue ships with **Context**,
   **Files / paths to touch**, **Acceptance Criteria**, and
   **Definition of Done**. Implement *every* AC.
4. Stick to the listed file scope. If you must touch something else, say so in
   the PR description and explain why.

### Label cheat sheet

| Group | Labels |
|---|---|
| Phase | `phase:1-foundations` … `phase:7-hardening` |
| Type | `type:feat` `type:fix` `type:docs` `type:chore` `type:test` `type:refactor` `type:research` |
| Area | `area:core` `area:models` `area:tools` `area:interfaces` `area:prompts` `area:config` |
| Priority | `priority:p0-blocker` `priority:p1-high` `priority:p2-normal` `priority:p3-low` |
| Status | `status:ready` `status:in-progress` `status:blocked` |
| Special | `agent-friendly` `good-first-issue` `help-wanted` `epic` `bot-review-bypass` |

## Branching model

Branch names follow `<type>/<issue>-<short-slug>`:

```
feat/42-ram-watchdog
fix/57-classifier-timeout
docs/50-contrib-dco-agents
chore/61-dependabot-grouping
ops/52-make-targets
```

`<type>` mirrors the Conventional Commit type (see below). One issue per
branch, one branch per PR.

## Commit conventions

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional-scope>): <imperative subject ≤72 chars>

<body wrapped at 80 cols, explaining *why* not *what*>

Closes #<issue>
Signed-off-by: Your Name <you@example.com>
```

Allowed types: `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `ops`,
`perf`, `build`, `ci`, `revert`.

The PR title **must** be a valid Conventional Commit message — it becomes the
squash-merge commit.

## DCO sign-off (mandatory)

Every commit must carry a `Signed-off-by:` trailer asserting the
[Developer Certificate of Origin v1.1](DCO). Run:

```bash
git commit -s -m "feat: add RAM watchdog"
```

CI rejects PRs whose commits are missing a `Signed-off-by:` line. We enforce
DCO because this project is licensed under
[CC BY-NC-SA 4.0](LICENSE) — sign-off keeps the provenance of contributions
unambiguous and protects everyone downstream.

To retro-sign an existing branch:

```bash
git rebase --signoff main
git push --force-with-lease
```

## GPG-signed commits (mandatory)

The `main` branch enforces `required_signatures` via a repository ruleset.
Every commit must be GPG- or SSH-signed; unsigned pushes are rejected.

One-time setup (GPG):

```bash
gpg --full-generate-key                          # ed25519 or rsa4096
gpg --list-secret-keys --keyid-format=long       # copy KEYID
git config --global user.signingkey <KEYID>
git config --global commit.gpgsign true
git config --global tag.gpgsign true
gpg --armor --export <KEYID>                     # paste at https://github.com/settings/gpg/new
```

Or SSH (simpler, reuses `~/.ssh/id_ed25519`):

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
# upload the .pub at https://github.com/settings/ssh/new (key type = "Signing Key")
```

Verify:

```bash
git log --show-signature -1
gh api /repos/skgandikota/coracle/commits/<sha> --jq '.commit.verification'
# expect: verified=true, reason=valid
```

GitHub Apps (bots) sign automatically with GitHub's web-flow signature.

## Pull-request process

1. Push your branch and open a PR against `main`.
2. The PR title must be a Conventional Commit (`<type>: <subject>`).
3. The PR body must include `Closes #<issue>` so the issue auto-closes on merge.
4. Fill out [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)
   in full — every checkbox is load-bearing.
5. Keep PRs **small and focused**: one issue per PR. Draft PRs are welcome
   while you iterate.
6. Re-running CI without changes? Push an empty signed commit:
   `git commit --allow-empty -s -m "ci: rerun"`.

### Config-/docs-only PRs

PRs that only touch documentation, configuration, GitHub metadata, or other
non-code surfaces may apply the **`bot-review-bypass`** label to skip the
heavier AI review stack. Code changes (anything under `coracle/` or
`tests/`) **must not** use this bypass.

## Review process

- [`@skgandikota`](https://github.com/skgandikota) is a code owner and is
  auto-assigned via [`.github/CODEOWNERS`](.github/CODEOWNERS).
- A layered AI review stack runs on every PR — see
  [`docs/REVIEW_BOTS.md`](docs/REVIEW_BOTS.md) and
  [`.github/review-bots.yml`](.github/review-bots.yml). Only the strict
  `code-reviewer-001` bot has merge authority; it waits for the advisory
  bots (CodeRabbit, Sourcery, PR-Agent, Gemini, Copilot) to weigh in before
  approving or requesting changes.
- At least **one approving review** is required.
- Stale reviews are dismissed automatically when new commits are pushed.
- The last pusher cannot self-approve.
- Address every actionable comment with either a code change or a reasoned
  reply; do not silently resolve threads.

## Build, test, lint

Canonical commands (all defined in the [`Makefile`](Makefile)):

```bash
make lint        # ruff check + ruff format --check
make typecheck   # mypy on changed modules
make test        # pytest
make cov         # pytest with coverage gate
```

**Coverage gate: 95%.** CI runs
`pytest --cov=coracle --cov-fail-under=95`. PRs that drop coverage below
95% fail. Use `# pragma: no cover` only for genuinely-untestable branches
(real subprocess fork paths, platform-specific stubs, etc.).

Test conventions:

- **Unit** tests are pure: no model calls, no network.
- **Ollama** integration tests use `@pytest.mark.ollama` (skipped by default).
- **Live API** tests use `@pytest.mark.live` (skipped by default).
- Mock big-AI providers via `litellm` mock backends.

## Architectural rules of the road

These are non-negotiable for any change:

```
1. Never load two 7B models simultaneously. Always go through core.scheduler.
2. Every coder step is a checkpoint — write progress to SQLite before yielding the slot.
3. Status queries must not require an LLM by default — read from SQLite first.
4. All long-running work is a job with an ID. APIs return job_id immediately; clients stream/poll.
5. Provider fallback is automatic — no caller hardcodes a provider.
6. Browser drivers are separate subprocesses — never in-process with the main app.
7. One external model name: coracle. The classifier picks the pipeline, not the user.
8. Honor the RAM cap. psutil-based watchdog must be respected by every subprocess.
```

If your change conflicts with any of these, open a discussion issue first.

## Security

- **Never commit secrets.** Push protection is enabled but treat it as a
  safety net, not a substitute for review.
- Report vulnerabilities privately per [`SECURITY.md`](SECURITY.md).
- Scanners that gate `main`: `ruff`, `mypy`, `pytest --cov`, `gitleaks`,
  `semgrep`, `trivy`, DCO. See
  [`.github/branch-protection.md`](.github/branch-protection.md) for the
  authoritative list.

## License & contribution terms

By submitting a pull request you agree your contribution is licensed under
[CC BY-NC-SA 4.0](LICENSE). That means downstream users may share and adapt
your work non-commercially, attributing you and re-sharing under the same
terms. Sign-off (the DCO) is your assertion that you have the right to make
that grant.

## Questions?

Open an issue or comment on the one you're working on. We answer.
