"""find-and-fix: the whole loop in one invocation, run to completion.

    verify the merge window (TESTING)            -> regression issues, labelled
    every `sda-regression` issue opened in the
    last N hours without a fix in flight         -> one fix session each
    wait for all of them (none is fine)          -> one find-and-fix report on the status issue

TESTING and AUTOPR still write their own stage telemetry; the find-and-fix report is the rollup a
leader reads: what was verified, what regressed, what got fixed, what it cost.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .autopr_job import AutoprReport, _in_flight_reason, publish_fixes
from .devin_api import DevinClient
from .github_api import GitHubClient
from .ledger import IssueLedger
from .registry import Registry
from .regression import REGRESSION_LABEL, regression_record, start_regression_fix
from .status import autopr_lines, post_run
from .testing_job import TestingReport, run_testing

log = logging.getLogger(__name__)

DEFAULT_ISSUE_WINDOW_HOURS = 24


@dataclass
class FinderReport:
    testing: dict[str, Any] = field(default_factory=dict)
    issue_window_hours: int = DEFAULT_ISSUE_WINDOW_HOURS
    candidates: list[int] = field(default_factory=list)
    fixes: dict[str, Any] = field(default_factory=dict)
    status_issue: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _opened_at(issue: dict[str, Any]) -> datetime | None:
    raw = issue.get("created_at")
    if not raw:
        return None
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def recent_regression_issues(
    gh: GitHubClient,
    target_repo: str,
    hours: int,
    now: datetime | None = None,
    include: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """Open `sda-regression` issues created within the window, oldest first.

    `include` names issues that must be in the result even if the label listing does not
    return them yet (GitHub's list lags a just-created issue by a few seconds).
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=hours)
    issues = gh.list_issues(target_repo, labels=REGRESSION_LABEL, state="open")
    by_number = {int(i["number"]): i for i in issues}
    for number in include:
        if number not in by_number:
            by_number[number] = gh.get_issue(target_repo, number)
    recent = [i for i in by_number.values() if (opened := _opened_at(i)) is not None and opened >= cutoff]
    return sorted(recent, key=lambda i: int(i["number"]))


def fix_recent_regressions(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    target_repo: str,
    automation_repo: str,
    hours: int,
    wait: bool,
    sleep: Callable[[float], None],
    now: datetime | None = None,
    include: Iterable[int] = (),
) -> tuple[list[int], AutoprReport]:
    report = AutoprReport(trigger="find-and-fix")
    ledger = IssueLedger(gh, target_repo)
    issues = recent_regression_issues(gh, target_repo, hours, now, include)
    report.scanned = len(issues)
    open_prs = gh.list_pulls(target_repo, state="open")
    for issue in issues:
        number = int(issue["number"])
        entries = ledger.read(number)
        record = regression_record(entries)
        if record is None:
            report.skipped_in_flight.append(
                {"issue": number, "reason": "no regression record; not filed by TESTING"}
            )
            continue
        reason = _in_flight_reason(number, entries, open_prs, devin, target_repo)
        if reason:
            report.skipped_in_flight.append({"issue": number, "reason": reason})
            log.info("find-and-fix: #%d skipped, %s", number, reason)
            continue
        report.started.append(
            start_regression_fix(
                devin=devin,
                ledger=ledger,
                target_repo=target_repo,
                automation_repo=automation_repo,
                issue=issue,
                record=record,
            )
        )
    if wait and report.started:
        publish_fixes(devin, gh, target_repo, ledger, report, sleep)
    return [int(i["number"]) for i in issues], report


def run_find_and_fix(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    target_repo: str,
    automation_repo: str,
    event: dict[str, Any],
    verify_branch: str,
    every_n: int,
    issue_window_hours: int = DEFAULT_ISSUE_WINDOW_HOURS,
    playbook_id: str | None = None,
    wait: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> FinderReport:
    report = FinderReport(issue_window_hours=issue_window_hours)
    testing: TestingReport = run_testing(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=target_repo,
        automation_repo=automation_repo,
        event=event,
        verify_branch=verify_branch,
        every_n=every_n,
        playbook_id=playbook_id,
        wait=wait,
        sleep=sleep,
    )
    report.testing = testing.as_dict()
    if not wait:
        return report
    filed = testing.regression_filed or {}
    report.candidates, fixes = fix_recent_regressions(
        devin=devin,
        gh=gh,
        target_repo=target_repo,
        automation_repo=automation_repo,
        hours=issue_window_hours,
        wait=True,
        sleep=sleep,
        now=now,
        include=[int(filed["issue"])] if filed.get("issue") else (),
    )
    report.fixes = fixes.as_dict()
    # keyed by the verification alone: a replayed find-and-fix for the same merge reports nothing twice
    session_ids = [testing.session_id] if testing.session_id else []
    verdict = testing.verdict or testing.skipped_reason or "no verification this merge"
    lines = [
        f"verification: {verdict}"
        + (f", session `{testing.session_id}`" if testing.session_id else "")
        + (f", window {', '.join(f'#{p}' for p in testing.window_prs)}" if testing.window_prs else ""),
        f"regression issue filed: {filed.get('issue_url') or 'none'}",
        f"regression issues opened in the last {issue_window_hours}h: "
        + (", ".join(f"#{n}" for n in report.candidates) or "none"),
        *autopr_lines("find-and-fix", fixes.finished, 0, len(fixes.skipped_in_flight)),
    ]
    report.status_issue = post_run(
        gh,
        target_repo,
        "find-and-fix",
        session_ids,
        f"find-and-fix: {verdict}; {len(fixes.finished)} fix session(s)",
        {
            "verdict": testing.verdict,
            "window": list(testing.window_prs),
            "regression_issue": filed.get("issue"),
            "candidates": report.candidates,
            "fixes": fixes.finished,
        },
        lines,
    )
    return report
