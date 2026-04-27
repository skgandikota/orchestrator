# Branch protection / required reviewers — `main`

This repo enforces the following rules on the `main` branch (configured via
the GitHub API by the maintainer; mirrored here for transparency):

## Required reviewers

| Reviewer | Type | How it is enforced |
|---|---|---|
| `@skgandikota` | User | Listed in [`.github/CODEOWNERS`](./CODEOWNERS); enforced by **Require review from Code Owners**. |
| `code-reviewer-001` | GitHub App | Listed as a required reviewer in the Repository Ruleset for `main`. **The app must be installed on this repository** for its review to be requested automatically. |

GitHub Apps cannot be placed in `CODEOWNERS` — apps are only valid as
required reviewers via **Repository Rulesets** or **Branch Protection** UI.

## Other rules applied to `main`

- `require_pull_request_before_merging`: **on**
- `required_approving_review_count`: **1**
- `require_code_owner_review`: **on**
- `dismiss_stale_reviews_on_push`: **on**
- `require_last_push_approval`: **on**
- `require_conversation_resolution`: **on**
- `allow_force_pushes`: **off**
- `allow_deletions`: **off**
- `required_linear_history`: **on**

## Installing `code-reviewer-001`

If the app review is not being requested automatically:

1. Visit the app's page on GitHub Marketplace and click **Install**.
2. Grant access to `skgandikota/orchestrator`.
3. Confirm the app appears under
   `Settings → Code security → Branch protection rules` (or
   `Rulesets → main → Bypass / Required reviewers`).
4. Open a test PR — the app should be automatically requested for review.

## Re-applying the rules after edits

The maintainer can re-apply branch protection by running:

```bash
gh api -X PUT repos/skgandikota/orchestrator/branches/main/protection \
  --input .github/branch-protection.json
```

(JSON payload is checked into this folder when the protection is configured.)
