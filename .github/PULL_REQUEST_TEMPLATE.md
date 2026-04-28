<!--
PR title MUST be a Conventional Commit (it becomes the squash-merge commit):
  <type>(<optional-scope>): <imperative subject ≤72 chars>
Allowed types: feat, fix, docs, chore, test, refactor, ops, perf, build, ci, revert.
-->

## Linked issue

Closes #

<!-- The literal "Closes #<n>" trailer is required so the issue auto-closes on merge. -->

## Summary

<!-- 2–4 sentences. User-visible behavior, not implementation detail. -->

## How was it tested?

```
make lint
make test
make cov
```

<!-- Add manual verification steps, screenshots, or sample output if relevant. -->

## File scope

<!-- Paste the issue's "Files / paths to touch" list. Confirm `git diff --name-only origin/main...HEAD` matches. Justify any deviation. -->

## Checklist (every box must be ticked)

- [ ] PR title is a valid Conventional Commit (`<type>: <subject>`, ≤72 chars)
- [ ] PR body contains `Closes #<issue>`
- [ ] **Acceptance Criteria met** — every AC from the linked issue is implemented
- [ ] Diff stays within the issue's "Files / paths to touch" (deviations justified above)
- [ ] Every commit is **signed off** (`git commit -s`, DCO trailer present)
- [ ] Every commit is **GPG- or SSH-signed** (`verified=true` on GitHub)
- [ ] `ruff check .` and `ruff format --check .` are clean
- [ ] `pytest --cov=coracle --cov-fail-under=95` passes locally — **coverage ≥95%**
- [ ] Tests added/updated for new behavior
- [ ] Type hints on public surfaces
- [ ] No secrets committed (keys, tokens, `.env` files, fixtures with real creds)
- [ ] Architectural rules in `CONTRIBUTING.md` respected
- [ ] No `Co-authored-by: Copilot …` trailer added
- [ ] Docs updated where behavior changed (or N/A)

## Notes for reviewers

<!-- Tricky bits, alternatives considered, follow-up issues to file. -->
