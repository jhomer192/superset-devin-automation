"""One run, one report: the last step of a TESTING or AUTOPR run that waited for its sessions.

The report is a comment on one long-lived issue in the target repository, found by the
`sda-status` label and created on first use. A `run_reported` marker keyed by the run's session
ids makes a replayed run append nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from .github_api import GitHubClient
from .ledger import IssueLedger, LedgerEntry, find
from .publish import outcome_lines

log = logging.getLogger(__name__)

STATUS_LABEL = "sda-status"
STATUS_TITLE = "superset-devin-automation: run log"
STATUS_BODY = (
    "Every TESTING and AUTOPR run that started sessions appends one comment here once those "
    "sessions have finished. Per-thread detail lives on the PRs and issues each comment links to."
)


def status_issue(gh: GitHubClient, repo: str) -> int:
    for issue in gh.list_issues(repo, labels=STATUS_LABEL, state="open"):
        if "pull_request" not in issue:
            return int(issue["number"])
    created = gh.create_issue(repo, STATUS_TITLE, STATUS_BODY, [STATUS_LABEL])
    log.info("status issue created: #%s", created["number"])
    return int(created["number"])


def _run_key(stage: str, session_ids: list[str]) -> str:
    return f"{stage}:{','.join(sorted(session_ids))}"


def post_run(
    gh: GitHubClient,
    repo: str,
    stage: str,
    session_ids: list[str],
    title: str,
    data: dict[str, Any],
    lines: list[str],
) -> int | None:
    """Append the run's report to the status issue; return its number, None if already there."""
    if not session_ids:
        return None
    number = status_issue(gh, repo)
    ledger = IssueLedger(gh, repo)
    key = _run_key(stage, session_ids)
    if find(ledger.read(number), "run_reported", run=key):
        log.info("%s run %s already on #%d", stage, key, number)
        return None
    ledger.append(
        number, title, LedgerEntry("run_reported", data={"run": key, "stage": stage, **data}), lines
    )
    return number


def testing_lines(
    *,
    base_sha: str | None,
    head_sha: str,
    window_prs: list[int],
    merge_index: int | None,
    session: dict[str, Any],
    acus: float | None,
    regression_filed: dict[str, Any] | None,
) -> list[str]:
    lines = [
        f"merge #{merge_index}, window {', '.join(f'#{p}' for p in window_prs)}",
        f"head `{head_sha}` vs base `{base_sha}`",
        *outcome_lines(session, acus),
    ]
    if regression_filed and regression_filed.get("escalated"):
        lines.append(f"escalated: chain depth {regression_filed['depth']} exhausted, no issue filed")
    elif regression_filed:
        lines.append(
            f"regression issue: {regression_filed['issue_url']} (chain depth {regression_filed['depth']})"
        )
    return lines


def autopr_lines(trigger: str, finished: list[dict[str, Any]], deflected: int, skipped: int) -> list[str]:
    lines = [
        f"trigger: {trigger}; {len(finished)} fix session(s) finished, "
        f"{deflected} deflected, {skipped} in flight"
    ]
    total = 0.0
    for row in finished:
        acus = row.get("acus")
        if acus is not None:
            total += float(acus)
        lines.append(
            f"#{row['issue']}: **{row['verdict']}**, session `{row['session_id']}` ({row['status']})"
            + (f", PR {row['pr_url']}" if row.get("pr_url") else "")
            + (f", {float(acus):g} ACU" if acus is not None else "")
        )
    lines.append(f"total ACUs: {total:g}")
    return lines
