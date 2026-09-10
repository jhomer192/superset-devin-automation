"""AUTOPR: one fix session per issue the loop decides to work on, and its outcome on the issue.

Two ways in. A `github:issues` event carrying the `sda-regression` label (an issue TESTING just
filed) starts the fix session for that one issue, and only that one. The Friday sweep triages
every open `ready` issue, deflects at zero ACU, and starts a session for each eligible issue that
does not already have one in flight. With `wait`, the run then polls every session it started
and writes the verdict comment (status, PR, probe exit codes, ACUs) onto the issue.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .devin_api import DevinClient
from .github_api import GitHubClient, closing_issue_numbers
from .ledger import IssueLedger, LedgerEntry, find
from .prompts import fix_session_prompt
from .publish import post_outcome, session_acus, verdict
from .registry import Registry
from .regression import FIX_TAG, REGRESSION_LABEL, regression_record, start_regression_fix
from .schema import FIX_SCHEMA
from .sessions import holds_slot, wait_until_finished
from .status import autopr_lines, post_run
from .triage import Decision, Triage, classify

log = logging.getLogger(__name__)


class NotARegressionIssue(ValueError):
    pass


@dataclass
class AutoprReport:
    trigger: str = "sweep"
    scanned: int = 0
    started: list[dict[str, Any]] = field(default_factory=list)
    deflected: list[dict[str, Any]] = field(default_factory=list)
    skipped_in_flight: list[dict[str, Any]] = field(default_factory=list)
    finished: list[dict[str, Any]] = field(default_factory=list)
    status_issue: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def publish_fixes(
    devin: DevinClient,
    gh: GitHubClient,
    target_repo: str,
    ledger: IssueLedger,
    report: AutoprReport,
    sleep: Callable[[float], None],
) -> None:
    """Wait for every session this run started, put each verdict on its issue, then log the run."""
    acu_cache: dict[str, float] = {}
    for started in report.started:
        session = wait_until_finished(devin, str(started["session_id"]), sleep)
        acus = session_acus(devin, session, acu_cache)
        posted = post_outcome(
            ledger=ledger, session=session, kind="fix", threads=[int(started["issue"])], acus=acus
        )
        output = session.get("structured_output") or {}
        report.finished.append(
            {
                "issue": started["issue"],
                "session_id": started["session_id"],
                "status": session.get("status"),
                "verdict": verdict(session.get("structured_output")),
                "pr_url": output.get("pr_url"),
                "acus": acus,
                "posted": bool(posted),
            }
        )
        log.info("AUTOPR: %s finished: %s", started["session_id"], report.finished[-1]["verdict"])
    verdicts = ", ".join(f"#{row['issue']} {row['verdict']}" for row in report.finished) or "nothing started"
    report.status_issue = post_run(
        gh,
        target_repo,
        "autopr",
        [str(row["session_id"]) for row in report.finished],
        f"AUTOPR ({report.trigger}): {verdicts}",
        {"trigger": report.trigger, "finished": report.finished},
        autopr_lines(report.trigger, report.finished, len(report.deflected), len(report.skipped_in_flight)),
    )


def extract_regression_issue(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the github:issues payload the way the automation trigger does."""
    issue = event.get("issue") or {}
    labels = {str(lb.get("name")) for lb in issue.get("labels") or []}
    labels.add(str((event.get("label") or {}).get("name")))
    if event.get("action") not in {"opened", "labeled"} or REGRESSION_LABEL not in labels:
        raise NotARegressionIssue(f"event is not an issue labelled {REGRESSION_LABEL!r}")
    if not issue.get("number"):
        raise NotARegressionIssue("issue lacks a number")
    return dict(issue)


def run_autopr_for_issue(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    target_repo: str,
    automation_repo: str,
    event: dict[str, Any],
    wait: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    playbook_id: str | None = None,
) -> AutoprReport:
    report = AutoprReport(trigger="github:issues", scanned=1)
    issue = extract_regression_issue(event)
    number = int(issue["number"])
    repo_full = str((event.get("repository") or {}).get("full_name") or target_repo)
    if repo_full.lower() != target_repo.lower():
        report.skipped_in_flight.append(
            {"issue": number, "reason": f"event is for {repo_full}, not {target_repo}"}
        )
        return report
    ledger = IssueLedger(gh, target_repo)
    entries = ledger.read(number)
    record = regression_record(entries)
    if record is None:
        # labelled by hand, or the label event beat TESTING's ledger comment
        report.skipped_in_flight.append(
            {"issue": number, "reason": "no regression record on the issue; TESTING did not file it"}
        )
        return report
    reason = _in_flight_reason(number, entries, gh.list_pulls(target_repo, state="open"), devin, target_repo)
    if reason:
        report.skipped_in_flight.append({"issue": number, "reason": reason})
        log.info("AUTOPR: #%d skipped, %s", number, reason)
        return report
    started = start_regression_fix(
        devin=devin,
        ledger=ledger,
        target_repo=target_repo,
        automation_repo=automation_repo,
        issue=issue,
        record=record,
        trigger=report.trigger,
        playbook_id=playbook_id,
    )
    report.started.append(started)
    if wait:
        publish_fixes(devin, gh, target_repo, ledger, report, sleep)
    return report


def _in_flight_reason(
    issue_number: int,
    entries: list[LedgerEntry],
    open_prs: list[dict[str, Any]],
    devin: DevinClient,
    target_repo: str,
) -> str | None:
    for pr in open_prs:
        text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
        if issue_number in closing_issue_numbers(text, target_repo):
            return f"open PR {pr['html_url']} already closes #{issue_number}"
    for entry in reversed(find(entries, "session_started")):
        session_id = str(entry.data.get("session_id", ""))
        if not session_id:
            continue
        session = devin.get_session(session_id)
        if holds_slot(session.get("status"), session.get("status_detail")):
            return (
                f"session {session_id} is {session.get('status')}"
                f"/{session.get('status_detail') or '-'} and still holds the slot"
            )
        break
    return None


def _already_deflected(entries: list[LedgerEntry], triage: Triage) -> bool:
    return bool(find(entries, "triage_deflected", reason=triage.reason.value))


def run_autopr(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    target_repo: str,
    automation_repo: str,
    ready_label: str,
    playbook_id: str | None = None,
    event: dict[str, Any] | None = None,
    wait: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> AutoprReport:
    if event is not None:
        return run_autopr_for_issue(
            devin=devin,
            gh=gh,
            target_repo=target_repo,
            automation_repo=automation_repo,
            event=event,
            wait=wait,
            sleep=sleep,
            playbook_id=playbook_id,
        )
    report = AutoprReport()
    if not playbook_id:
        log.warning("AUTOPR: PLAYBOOK_ID_FIX unset, using the fully inline remediation prompt")
    ledger = IssueLedger(gh, target_repo)
    issues = gh.list_issues(target_repo, labels=ready_label, state="open")
    open_prs = gh.list_pulls(target_repo, state="open")
    report.scanned = len(issues)
    log.info("AUTOPR: %d open issues labelled %r", len(issues), ready_label)

    for issue in sorted(issues, key=lambda i: int(i["number"])):
        number = int(issue["number"])
        spec = registry.by_number(number)
        triage = classify(issue, spec)
        entries = ledger.read(number)

        if triage.decision is Decision.DEFLECT:
            record = {"issue": number, "reason": triage.reason.value, "detail": triage.detail}
            report.deflected.append(record)
            if _already_deflected(entries, triage):
                log.info("AUTOPR: #%d deflected (%s), already logged", number, triage.reason.value)
                continue
            ledger.append(
                number,
                "Triage: deflected, no session started (0 ACU)",
                LedgerEntry("triage_deflected", data={"reason": triage.reason.value, "acu": 0}),
                [f"reason: `{triage.reason.value}`", triage.detail],
            )
            log.info("AUTOPR: #%d deflected (%s)", number, triage.reason.value)
            continue

        assert spec is not None
        reason = _in_flight_reason(number, entries, open_prs, devin, target_repo)
        if reason:
            report.skipped_in_flight.append({"issue": number, "reason": reason})
            log.info("AUTOPR: #%d skipped, %s", number, reason)
            continue

        session = devin.create_session(
            {
                "prompt": fix_session_prompt(target_repo, automation_repo, issue, spec, playbook_id),
                "title": f"Fix {target_repo}#{number}: {spec.title[:80]}",
                "tags": [FIX_TAG, f"issue-{number}"],
                "structured_output_schema": FIX_SCHEMA,
                "structured_output_required": True,
                "resumable": True,
            }
        )
        session_id = str(session["session_id"])
        ledger.append(
            number,
            "Remediation session started",
            LedgerEntry("session_started", data={"session_id": session_id, "kind": "fix"}),
            [
                f"session: `{session_id}`" + (f" ({session['url']})" if session.get("url") else ""),
                f"deciding probes: {', '.join(p.id for p in spec.probes)}",
            ],
        )
        report.started.append({"issue": number, "session_id": session_id})
        log.info("AUTOPR: #%d -> session %s", number, session_id)
    if wait:
        publish_fixes(devin, gh, target_repo, ledger, report, sleep)
    return report
