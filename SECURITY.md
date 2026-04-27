# Security Policy

`skgandikota/orchestrator` is a personal, single-maintainer project. There is
**no bug bounty**, but security reports are taken seriously and triaged ahead
of feature work.

## Supported versions

Only the `main` branch is supported. There are no released versions yet
(pre-alpha). Fixes ship as ordinary commits to `main`.

## Reporting a vulnerability

**Please do not open public issues for security problems.**

Use GitHub's **Private Vulnerability Reporting** to file a confidential report:

- <https://github.com/skgandikota/orchestrator/security/advisories/new>

Include, where possible:

- A description of the issue and the impact you believe it has.
- Steps to reproduce (PoC, command transcript, or minimal repo).
- The commit SHA / branch you tested against.
- Any suggested mitigation.

You should receive an acknowledgement within **7 days**. I aim to provide a
fix or a documented mitigation within **90 days** of the initial report;
coordinated public disclosure happens after that window or when a fix lands
on `main`, whichever comes first.

## Scope

In scope:

- The `orchestrator` Python package and shipped configuration.
- CI workflows under `.github/workflows/` (supply-chain and permission issues).
- Documented integration surfaces (the OpenAI-compatible HTTP interface and
  the local tool belt described in `docs/PLAN.md`).

Out of scope:

- Vulnerabilities that require a malicious operator on the host machine
  (this is a personal-machine tool — the operator is the trust boundary).
- Issues in upstream models, third-party APIs, or browser providers.
- Denial of service via resource exhaustion on the local machine
  (RAM / CPU caps are best-effort, not a security boundary).

## Hardening references

- Internal architectural security model: [`docs/SECURITY.md`](docs/SECURITY.md).
- Automated scanners that run on every PR: CodeQL, Semgrep, Trivy, Gitleaks,
  OSV-Scanner, plus Dependabot updates and Copilot code review. Findings
  surface in the repository's **Security** tab.
