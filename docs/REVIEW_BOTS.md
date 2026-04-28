# AI & Static Code Review Bots

This repository uses a **layered review model**: many bots post review comments;
only **one** bot has merge authority.

## The merge-gating bot

| Bot | Role | Authority |
|---|---|---|
| `code-reviewer-001` (our GitHub App) | Strict reviewer — validates Conventional Commits, branch naming, linked-issue Acceptance Criteria, secret scanning, test coverage, CI status. | **Approves + squash-merges + deletes branch** when clean. Requests changes when violations exist. |

Implementation lives in [`.github/scripts/pr_reviewer.py`](../.github/scripts/pr_reviewer.py)
and [`.github/workflows/pr-reviewer.yml`](../.github/workflows/pr-reviewer.yml).

## Comment-only AI review bots (free for public OSS)

These bots post review comments but **never** gate the merge. They run in
parallel with `code-reviewer-001`:

| Bot | What it adds | Config | Install link |
|---|---|---|---|
| **GitHub Copilot review** | First-party AI review, summary + inline | none (workflow auto-requests) | Built into GitHub — free for public repos |
| **CodeRabbit** | Line-by-line AI review, walkthrough, learnings | [`.coderabbit.yaml`](../.coderabbit.yaml) | <https://github.com/marketplace/coderabbitai> |
| **Qodo Merge** (Codium PR-Agent) | `/review` `/improve` `/describe` `/ask` slash commands, auto-run on open | [`.pr_agent.toml`](../.pr_agent.toml) | <https://github.com/marketplace/qodo-merge-pro-for-open-source> |
| **Gemini Code Assist** | Google AI review summary + inline issues | [`.gemini/config.yaml`](../.gemini/config.yaml) | <https://github.com/marketplace/gemini-code-assist> |
| **Sourcery** | Python-specific refactoring suggestions | [`.sourcery.yaml`](../.sourcery.yaml) | <https://github.com/marketplace/sourcery-ai> |

## Why this layering?

1. **Signal vs. noise** — multiple AI reviewers catch different classes of
   issue (CodeRabbit finds patterns, Sourcery finds Pythonic refactors,
   Gemini/Copilot find logic bugs). Stacking is cheap on public repos.
2. **Single source of merge truth** — only `code-reviewer-001` enforces our
   project conventions (Conventional Commits, AC linkage, secret hygiene,
   coverage). No third-party bot can merge.
3. **Free and OSS-friendly** — every bot above is free for public
   repositories. No paid tiers required.

## Installation (one-time, by repo owner)

The four third-party bots require a manual install via GitHub Marketplace
(there is no API to install third-party Apps). Click each link above, choose
**only this repository**, and complete the install. Configs are already
checked in and will activate automatically on the first PR after install.

## Opting out per PR

Each bot supports a label or comment to skip review:

| Bot | How to skip |
|---|---|
| CodeRabbit | comment `@coderabbitai pause` |
| Qodo Merge | label `qodo-ignore` |
| Gemini | comment `/gemini ignore` |
| Sourcery | label `sourcery-ignore` |
| Copilot | un-request the reviewer |

`code-reviewer-001` **cannot** be skipped — it is the merge gate.

## Static analysis (separate stack)

Static analysis tools (CodeQL, Semgrep, Trivy, Gitleaks, Dependabot) are
tracked separately in issue [#49](https://github.com/skgandikota/coracle/issues/49)
and configured under [`.github/workflows/`](../.github/workflows/).
