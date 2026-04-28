# Security model (internal)

This document describes the architectural security properties of
`coracle`. It is the long-form complement to the top-level
[`SECURITY.md`](../SECURITY.md), which covers vulnerability reporting.

`coracle` is a **personal-machine** tool. The operator (a developer
running it on their own laptop) is inside the trust boundary; everything
outside that boundary — third-party APIs, downloaded models, generated code,
shell commands suggested by an LLM — is treated as untrusted.

## Threat model summary

| Asset                              | Threat                                            | Mitigation                                                                 |
| ---------------------------------- | ------------------------------------------------- | -------------------------------------------------------------------------- |
| Operator's machine (RAM, CPU)      | Concurrent model loads OOM-kill the host          | Single-LLM-slot scheduler; hard RAM cap per model                          |
| Operator's filesystem              | LLM-generated code escapes the working directory  | Workspace jail; allow-list of writable paths                               |
| Operator's shell                   | LLM-generated shell command runs destructive ops  | Sandboxed shell tool; deny-list + per-command confirmation for risky verbs |
| API keys / cloud credentials       | Keys exfiltrated via prompt injection or logs     | Keys never injected into model context; redacted from logs and traces      |
| Source repositories                | Secret leaks in committed code                    | Gitleaks + GitHub secret scanning + push protection                        |
| Supply chain (Python + Actions)    | Malicious dep update or pinned-action takeover    | Dependabot (pip + github-actions), OSV-Scanner, Trivy, pinned `@vN` tags   |
| CI runners                         | Workflow privilege escalation                     | Least-privilege `permissions:` blocks; no `write-all`; no `pull_request_target` |

## Runtime guarantees

### Single-LLM-slot scheduler

At most one 7B-class model is resident in RAM at any time. Routing decisions
made by the classifier may unload one model and load another; they never run
in parallel. This is a **safety** property (prevents the host from being
OOM-killed) more than a security one, but it also bounds the blast radius of
a misbehaving model.

### RAM caps

Every LLM invocation declares a maximum resident-set size. The scheduler
refuses to load a model whose declared cap exceeds available RAM minus a
reserved headroom for the OS and editor. Caps are enforced before model
load, not after; an exceeded cap aborts the request rather than swapping.

### Sandboxed shell

The `shell` tool offered to the executor model:

- runs commands inside the active workspace directory only;
- rejects commands that reference paths outside the workspace jail;
- requires explicit operator confirmation for a deny-list of verbs
  (`rm -rf`, `git push --force`, `curl … | sh`, package installs, etc.);
- captures stdout/stderr into the job record but never re-injects raw
  secrets read from the environment back into the model context.

### Workspace jail

File-system tools (`fs.read`, `fs.write`, `fs.list`, …) resolve every path
against the workspace root and reject anything that escapes it via
symlinks or `..` traversal. The workspace root is set per-job and is
never the operator's home directory.

### Secrets handling

- API keys live in the operator's `.env` and are loaded into the
  coracle process, **not** the child model's context window.
- Tool outputs are scrubbed for known credential shapes
  (`sk-…`, `ghp_…`, AWS access keys, etc.) before being persisted to the
  SQLite job log.
- CI never has access to live operator secrets; workflows only see
  `secrets.GITHUB_TOKEN` plus any explicitly-declared repository secrets.

## CI / supply-chain controls

The repository ships with an opinionated security stack (issue #49):

- **CodeQL** (`.github/workflows/codeql.yml`) — Python SAST, weekly + on PR.
- **Semgrep** — `p/default p/python p/security-audit p/owasp-top-ten`.
- **Trivy** — filesystem scan for vulnerable deps and IaC misconfig.
- **Gitleaks** — secret scanning (full history on `main`, diff on PRs).
- **OSV-Scanner** — Python dep vulnerabilities cross-referenced against OSV.
- **Dependabot** — weekly `pip` and `github-actions` updates, grouped
  minor + patch.
- **Copilot code review** — requested automatically on PRs to `main`.

All scanners upload SARIF to GitHub Code Scanning, so findings appear in
the **Security** tab and inline on PRs. Workflows declare the minimum
`permissions:` they need (`contents: read`, plus `security-events: write`
where SARIF is uploaded), pin actions to major version tags, and use a
`concurrency:` group so duplicate runs are cancelled.

## Optional add-ons (not enabled by default)

These integrate cleanly with the current stack and can be enabled if the
project grows beyond a single maintainer:

- **SonarCloud** — broader code-quality + security hotspots.
- **Snyk** — alternative SCA + container scanning with a richer UI.
- **CodeRabbit** — AI PR review focused on bugs and security.
- **DeepSource** — Python-first static analysis with autofix PRs.
- **Codecov** — hosted coverage trend reporting.

Enable them later by adding their workflow file and a status badge; none
should be required to run the project.
