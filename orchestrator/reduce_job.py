"""REDUCE: on a merged PR, start exactly one verification session for (pr_url, merge_commit_sha).

The ledger for a PR is its own comment thread (PRs are issues to the GitHub API), so the
idempotency record survives orchestrator restarts and is visible to reviewers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .devin_api import DevinClient
from .github_api import GitHubClient, closing_issue_numbers
from .ledger import IssueLedger, LedgerEntry, find
from .prompts import verification_prompt
from .registry import Probe, Registry
from .schema import VERIFICATION_SCHEMA
from .sessions import holds_slot

log = logging.getLogger(__name__)

VERIFY_TAG = "sda-verify"


class NotAMergedPR(ValueError):
    pass


@dataclass
class ReduceReport:
    pr_url: str
    merge_commit_sha: str
    base_sha: str | None = None
    closes: list[int] = field(default_factory=list)
    regression_issues: list[int] = field(default_factory=list)
    session_id: str | None = None
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def extract_merged_pr(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the github:pull_request payload the way the automation trigger does."""
    pr = event.get("pull_request") or {}
    if event.get("action") != "closed" or not pr.get("merged"):
        raise NotAMergedPR("event is not a merged pull_request (action=closed, merged=true)")
    if not pr.get("merge_commit_sha") or not pr.get("html_url"):
        raise NotAMergedPR("pull_request lacks merge_commit_sha or html_url")
    return dict(pr)


def dedup_key(pr_url: str, merge_commit_sha: str) -> str:
    return f"{pr_url}@{merge_commit_sha}"


def regression_issue_numbers(
    gh: GitHubClient, registry: Registry, target_repo: str, exclude: set[int]
) -> list[int]:
    """Issues whose fixes already landed: their probes guard against regressions at HEAD."""
    out: list[int] = []
    for spec in registry.issues:
        if spec.number in exclude or not spec.probes:
            continue
        issue = gh.get_issue(target_repo, spec.number)
        if issue.get("state") == "closed" and issue.get("state_reason") == "completed":
            out.append(spec.number)
    return out


def run_reduce(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    target_repo: str,
    automation_repo: str,
    event: dict[str, Any],
) -> ReduceReport:
    pr = extract_merged_pr(event)
    pr_url = str(pr["html_url"])
    head_sha = str(pr["merge_commit_sha"])
    number = int(pr["number"])
    report = ReduceReport(pr_url=pr_url, merge_commit_sha=head_sha)

    repo_full = str((event.get("repository") or {}).get("full_name") or target_repo)
    if repo_full.lower() != target_repo.lower():
        report.skipped_reason = f"event is for {repo_full}, not {target_repo}"
        return report

    ledger = IssueLedger(gh, target_repo)
    entries = ledger.read(number)
    key = dedup_key(pr_url, head_sha)
    for entry in reversed(find(entries, "verification_started", key=key)):
        session_id = str(entry.data.get("session_id", ""))
        session = devin.get_session(session_id) if session_id else {}
        if session and holds_slot(session.get("status"), session.get("status_detail")):
            report.skipped_reason = f"verification {session_id} already in flight for {key}"
            report.session_id = session_id
            return report
        if session and session.get("structured_output"):
            report.skipped_reason = f"verification {session_id} already completed for {key}"
            report.session_id = session_id
            return report

    parents = gh.get_commit_parents(target_repo, head_sha)
    base_sha = parents[0] if parents else str((pr.get("base") or {}).get("sha") or "")
    report.base_sha = base_sha or None

    closes = closing_issue_numbers(f"{pr.get('title', '')}\n{pr.get('body', '')}", target_repo)
    report.closes = closes
    regression = regression_issue_numbers(gh, registry, target_repo, set(closes))
    report.regression_issues = regression

    probes: list[Probe] = registry.probes_for(closes)
    regression_probes: list[Probe] = registry.probes_for(regression)

    prompt = verification_prompt(
        target_repo,
        automation_repo,
        head_sha,
        base_sha,
        pr_url,
        closes,
        probes + regression_probes,
    )
    if regression:
        prompt += (
            "\nRegression guards (already-landed fixes, must pass at HEAD; BASE result is recorded "
            "but not required to fail): pass them with "
            f'--regression "{",".join(str(n) for n in regression)}".\n'
        )
    session = devin.create_session(
        {
            "prompt": prompt,
            "title": f"Verify {target_repo} PR #{number} @ {head_sha[:10]}",
            "tags": [VERIFY_TAG, f"pr-{number}"],
            "structured_output_schema": VERIFICATION_SCHEMA,
            "structured_output_required": True,
            "resumable": True,
        }
    )
    session_id = str(session["session_id"])
    report.session_id = session_id
    ledger.append(
        number,
        "Regression verification session started",
        LedgerEntry(
            "verification_started",
            data={"key": key, "session_id": session_id, "head": head_sha, "base": base_sha},
        ),
        [
            f"session: `{session_id}`" + (f" ({session['url']})" if session.get("url") else ""),
            f"head `{head_sha}` vs base `{base_sha}`",
            f"closes: {', '.join(f'#{n}' for n in closes) or 'none'}",
            f"regression guards: {', '.join(f'#{n}' for n in regression) or 'none'}",
        ],
    )
    log.info("REDUCE: %s -> session %s", key, session_id)
    return report
