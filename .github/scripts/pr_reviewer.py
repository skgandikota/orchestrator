#!/usr/bin/env python3
"""Strict, context-isolated PR reviewer.

Runs in GitHub Actions. The reviewer has *no* knowledge of the project beyond:
  1. The PR title / body / diff
  2. The body of the issue the PR claims to close (acceptance criteria,
     "files / paths to touch", definition of done)
  3. The list of CI check-runs on the head SHA

Decisions:
  * Any blocking violation     -> REQUEST_CHANGES (no approval, no merge)
  * Any warning OR pending CI  -> COMMENT only (no approval)
  * Clean + all checks green   -> APPROVE, squash-merge, delete branch

The reviewer is intentionally strict. False positives are preferred over
false approvals.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any

import requests

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["REPO"]
PR_NUM = int(os.environ["PR_NUMBER"])

H = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
API = "https://api.github.com"
CONV_PREFIX = r"(feat|fix|chore|docs|test|refactor|ops|perf|build|ci|revert)"
SECRET_PATTERNS = [
    (r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", "private key"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "OpenAI-style secret key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"\bAIza[0-9A-Za-z_\-]{35}\b", "Google API key"),
    (r"\bghp_[A-Za-z0-9]{36}\b", "GitHub PAT"),
    (r"\bghs_[A-Za-z0-9]{36}\b", "GitHub App secret"),
    (r"\bgithub_pat_[A-Za-z0-9_]{82}\b", "GitHub fine-grained PAT"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    (r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b", "SendGrid key"),
]


def gh(method: str, path: str, **kw: Any) -> Any:
    r = requests.request(method, f"{API}{path}", headers=H, timeout=30, **kw)
    if r.status_code >= 400:
        sys.stderr.write(f"GitHub API {method} {path} -> {r.status_code}: {r.text}\n")
        r.raise_for_status()
    return r.json() if r.text else {}


def get_diff(url: str) -> str:
    return requests.get(
        url,
        headers={"Authorization": H["Authorization"], "Accept": "application/vnd.github.v3.diff"},
        timeout=60,
    ).text


def find_linked_issue(pr: dict[str, Any]) -> int | None:
    text = (pr.get("body") or "") + "\n" + (pr.get("title") or "")
    m = re.search(r"(?:close[sd]?|fixe[sd]?|resolve[sd]?)\s+#(\d+)", text, re.I)
    return int(m.group(1)) if m else None


def looks_like_path_match(touched: str, allowed: str) -> bool:
    a = allowed.strip().rstrip("/")
    if a == touched:
        return True
    if "/" in a:
        if touched == a or touched.startswith(a + "/") or touched.startswith(a):
            return True
    base = a.split("/")[-1]
    if base and (touched.endswith("/" + base) or touched == base):
        return True
    return False


def main() -> int:
    pr = gh("GET", f"/repos/{REPO}/pulls/{PR_NUM}")
    if pr.get("state") != "open":
        print(f"PR #{PR_NUM} is not open (state={pr.get('state')}); nothing to do.")
        return 0
    if pr.get("draft"):
        print(f"PR #{PR_NUM} is draft; skipping review.")
        return 0

    files = gh("GET", f"/repos/{REPO}/pulls/{PR_NUM}/files?per_page=100")
    diff = get_diff(pr["diff_url"])
    head_sha = pr["head"]["sha"]
    head_ref = pr["head"]["ref"]
    is_same_repo = pr["head"]["repo"] and pr["head"]["repo"]["full_name"] == REPO

    violations: list[str] = []
    warnings: list[str] = []
    info: list[str] = []

    issue_num = find_linked_issue(pr)
    if not issue_num:
        violations.append(
            "PR body must contain `Closes #<issue>` (or `Fixes`/`Resolves`) referencing the issue this PR addresses."
        )
        issue: dict[str, Any] = {}
        issue_body = ""
    else:
        issue = gh("GET", f"/repos/{REPO}/issues/{issue_num}")
        issue_body = issue.get("body") or ""
        info.append(f"Linked issue: #{issue_num} - {issue.get('title','?')}")
        if issue.get("state") != "open":
            warnings.append(f"Linked issue #{issue_num} is not open (state={issue.get('state')}).")

    if not re.match(rf"^{CONV_PREFIX}(\([^)]+\))?:\s+\S", pr["title"]):
        violations.append(
            "PR title must follow Conventional Commits, e.g. `feat: short summary` or "
            "`fix(scope): summary`. See CONTRIBUTING.md."
        )

    if issue_num and not re.match(rf"^{CONV_PREFIX}/{issue_num}-[a-z0-9-]+$", head_ref):
        warnings.append(
            f"Branch `{head_ref}` should be named `<type>/{issue_num}-<short-slug>` "
            f"(lowercase, hyphenated)."
        )

    body_lower = (pr.get("body") or "").lower()
    if "acceptance criteria" not in body_lower and "all ac" not in body_lower:
        warnings.append(
            "PR description should explicitly confirm the issue's Acceptance Criteria are met."
        )

    paths_section = re.search(r"## Files / paths? to touch(.*?)(?=\n## |\Z)", issue_body, re.S | re.I)
    if paths_section:
        allowed = re.findall(r"`([^`\n]+)`", paths_section.group(1))
        if allowed:
            unexpected = []
            for f in files:
                fn = f["filename"]
                if fn in {"README.md", "CHANGELOG.md"} or fn.startswith("docs/"):
                    continue
                if not any(looks_like_path_match(fn, p) for p in allowed):
                    unexpected.append(fn)
            if unexpected:
                warnings.append(
                    "Files modified outside the issue's *Files / paths to touch* list:\n"
                    + "\n".join(f"  - `{u}`" for u in unexpected[:15])
                    + ("\n  - ..." if len(unexpected) > 15 else "")
                )

    for pat, label in SECRET_PATTERNS:
        if re.search(pat, diff):
            violations.append(f"Possible secret detected in diff (`{label}`). **Block.**")

    total_added = sum(f.get("additions", 0) for f in files)
    test_added = sum(
        f.get("additions", 0)
        for f in files
        if "/test" in f["filename"] or f["filename"].startswith("tests/")
    )
    is_docs_only = all(
        f["filename"].endswith((".md", ".rst", ".txt"))
        or f["filename"].startswith(("docs/", "."))
        for f in files
    )
    if total_added > 60 and test_added == 0 and not is_docs_only:
        violations.append(
            f"Non-trivial change ({total_added} lines added) without any test changes. "
            f"Add unit tests under `tests/` covering at least the happy path + 2 failure modes."
        )

    runs_resp = gh("GET", f"/repos/{REPO}/commits/{head_sha}/check-runs?per_page=100")
    runs = [c for c in runs_resp.get("check_runs", []) if "review" not in c.get("name", "").lower()]
    failing = [c for c in runs if c["status"] == "completed" and c["conclusion"] not in {"success", "neutral", "skipped"}]
    pending = [c for c in runs if c["status"] != "completed"]
    if failing:
        violations.append(
            "Failing CI checks:\n" + "\n".join(f"  - **{c['name']}** -> {c['conclusion']}" for c in failing)
        )
    if pending:
        warnings.append(
            "Pending CI checks (will not approve until green):\n"
            + "\n".join(f"  - {c['name']} -> {c.get('status')}" for c in pending)
        )

    if total_added > 1500 and not is_docs_only:
        warnings.append(
            f"PR adds {total_added} lines - consider splitting into smaller, single-purpose PRs."
        )

    commits = gh("GET", f"/repos/{REPO}/pulls/{PR_NUM}/commits?per_page=100")
    merges = [c for c in commits if len(c.get("parents", [])) > 1]
    if merges:
        warnings.append(
            "Branch contains merge commit(s); rebase onto `main` for linear history "
            "(branch protection enforces linear history on merge)."
        )

    parts = ["## Strict PR reviewer"]
    if info:
        parts.append("\n".join(f"- {i}" for i in info))
    if violations:
        parts.append("### Blocking issues\n\n" + "\n\n".join(f"- {v}" for v in violations))
    if warnings:
        parts.append("### Warnings\n\n" + "\n\n".join(f"- {w}" for w in warnings))
    if not violations and not warnings:
        parts.append("All checks passed. Approving and merging.")
    body = "\n\n".join(parts)

    if violations:
        gh("POST", f"/repos/{REPO}/pulls/{PR_NUM}/reviews", json={"body": body, "event": "REQUEST_CHANGES"})
        print(f"Requested changes on PR #{PR_NUM}.")
        return 0

    if warnings or pending:
        gh("POST", f"/repos/{REPO}/pulls/{PR_NUM}/reviews", json={"body": body, "event": "COMMENT"})
        print(f"Commented on PR #{PR_NUM} (warnings or pending CI).")
        return 0

    gh("POST", f"/repos/{REPO}/pulls/{PR_NUM}/reviews", json={"body": body, "event": "APPROVE"})
    print(f"Approved PR #{PR_NUM}.")

    merge_resp = requests.put(
        f"{API}/repos/{REPO}/pulls/{PR_NUM}/merge",
        headers=H,
        json={
            "merge_method": "squash",
            "commit_title": f"{pr['title']} (#{PR_NUM})",
            "commit_message": (pr.get("body") or "").strip()[:4000],
        },
        timeout=30,
    )
    if merge_resp.status_code >= 400:
        sys.stderr.write(
            f"Merge failed: {merge_resp.status_code} {merge_resp.text}\n"
            f"(Approval still posted; manual merge required.)\n"
        )
        return 1
    print(f"Merged PR #{PR_NUM}.")

    if is_same_repo:
        del_resp = requests.delete(
            f"{API}/repos/{REPO}/git/refs/heads/{head_ref}",
            headers=H,
            timeout=30,
        )
        if del_resp.status_code in (200, 204):
            print(f"Deleted branch `{head_ref}`.")
        else:
            sys.stderr.write(
                f"Branch delete returned {del_resp.status_code}: {del_resp.text}\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
