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
from .github_api import GitHubClient, closing_issue_numbers
from .ledger import IssueLedger, LedgerEntry, find
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


@dataclass(frozen=True)
class Coverage:
    """A fix for `issue` that already covers a probe: in flight, or merged at `merged_at`."""

    issue: int
    reason: str
    merged_at: datetime | None = None

    def covers(self, candidate: dict[str, Any]) -> bool:
        if self.merged_at is None:
            return True
        opened = _opened_at(candidate)
        return opened is not None and opened < self.merged_at


def _record_probes(record: LedgerEntry) -> list[str]:
    return [str(p) for p in record.data.get("probes") or []]


def covered_probes(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    ledger: IssueLedger,
    target_repo: str,
    open_prs: list[dict[str, Any]],
) -> dict[str, Coverage]:
    """Every probe some other regression issue's fix already covers.

    An open issue with a fix in flight (live session or open PR that closes it) covers its probes
    outright. A closed issue whose fix PR merged covers its probes for every candidate filed
    before that merge: the candidate's failure predates the fix, so it has not been re-verified.
    """
    covered: dict[str, Coverage] = {}
    for issue in gh.list_issues(target_repo, labels=REGRESSION_LABEL, state="open"):
        number = int(issue["number"])
        entries = ledger.read(number)
        record = regression_record(entries)
        if record is None:
            continue
        reason = _in_flight_reason(number, entries, open_prs, devin, target_repo)
        if reason:
            for probe in _record_probes(record):
                covered.setdefault(probe, Coverage(number, reason))
    merged = [p for p in gh.list_pulls(target_repo, state="closed") if p.get("merged_at")]
    for issue in gh.list_issues(target_repo, labels=REGRESSION_LABEL, state="closed"):
        number = int(issue["number"])
        record = regression_record(ledger.read(number))
        if record is None:
            continue
        for pr in merged:
            text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
            if number not in closing_issue_numbers(text, target_repo):
                continue
            merged_at = datetime.fromisoformat(str(pr["merged_at"]).replace("Z", "+00:00"))
            coverage = Coverage(number, f"merged PR {pr['html_url']} closed #{number}", merged_at)
            for probe in _record_probes(record):
                covered.setdefault(probe, coverage)
    return covered


def _covering(
    record: LedgerEntry, candidate: dict[str, Any], covered: dict[str, Coverage]
) -> Coverage | None:
    """The first coverage of one of the candidate's probes by another issue's fix, if any."""
    number = int(candidate["number"])
    for probe in _record_probes(record):
        coverage = covered.get(probe)
        if coverage is not None and coverage.issue != number and coverage.covers(candidate):
            return coverage
    return None


def _note_covered(ledger: IssueLedger, number: int, coverage: Coverage, probes: list[str]) -> str:
    reason = f"{', '.join(probes)} already covered by #{coverage.issue}: {coverage.reason}"
    if not find(ledger.read(number), "fix_covered", by=coverage.issue):
        ledger.append(
            number,
            "Fix deferred: another regression issue's fix covers these probes",
            LedgerEntry("fix_covered", data={"by": coverage.issue, "probes": probes}),
            [reason, "the probes are re-verified when that fix merges; a failure then files anew"],
        )
    return reason


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
    covered = covered_probes(devin=devin, gh=gh, ledger=ledger, target_repo=target_repo, open_prs=open_prs)
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
        coverage = _covering(record, issue, covered)
        if coverage is not None:
            shared = [p for p in _record_probes(record) if covered.get(p) == coverage]
            reason = _note_covered(ledger, number, coverage, shared)
            report.skipped_in_flight.append({"issue": number, "reason": reason})
            log.info("find-and-fix: #%d skipped, %s", number, reason)
            continue
        started = start_regression_fix(
            devin=devin,
            ledger=ledger,
            target_repo=target_repo,
            automation_repo=automation_repo,
            issue=issue,
            record=record,
        )
        report.started.append(started)
        started_now = Coverage(number, f"fix session {started['session_id']} started this run")
        for probe in _record_probes(record):
            covered.setdefault(probe, started_now)
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
