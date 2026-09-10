"""REPORT: publish finished session outcomes back onto GitHub.

The Automations API has no completion callback and a session cannot comment on the target repo
itself (the loop's sessions are forbidden from touching it outside their own PR), so a verdict
would otherwise only exist inside the session's structured output. This job polls the Session API
for finished `sda-fix` / `sda-verify` sessions and writes one ledger comment per session onto the
issue or PR it belongs to, then appends a metrics digest at most once per `digest_every_hours`.

Reporting is idempotent the same way the rest of the loop is: a `session_reported` marker keyed by
session id already on the thread means the outcome has been published.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import metrics
from .devin_api import DevinClient
from .github_api import GitHubClient
from .ledger import IssueLedger, LedgerEntry, find
from .metrics import FIX_TAG, VERIFY_TAG, MetricsReport
from .sessions import is_finished

log = logging.getLogger(__name__)


@dataclass
class ReportReport:
    posted: list[dict[str, Any]] = field(default_factory=list)
    unfinished: int = 0
    already_reported: int = 0
    digest_issue: int | None = None
    digest_posted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _tagged_numbers(session: dict[str, Any], prefix: str) -> list[int]:
    out = []
    for tag in session.get("tags") or []:
        name = str(tag)
        if name.startswith(prefix) and name[len(prefix) :].isdigit():
            out.append(int(name[len(prefix) :]))
    return sorted(out)


def _verdict(output: dict[str, Any] | None) -> str:
    if output is None:
        return "no structured output"
    if output.get("status") == "error":
        return "error"
    return "acceptance met" if output.get("acceptance_met") else "acceptance NOT met"


def outcome_lines(session: dict[str, Any], acus: float | None) -> list[str]:
    """Human-readable body of a report comment: the verdict and the evidence behind it."""
    output = session.get("structured_output")
    lines = [
        f"verdict: **{_verdict(output)}**",
        f"session: `{session.get('session_id')}`"
        + (f" ({session['url']})" if session.get("url") else "")
        + f", status `{session.get('status')}`",
    ]
    if acus is not None:
        lines.append(f"ACUs: {acus:g}")
    if output is None:
        lines.append("the session finished without emitting its required structured output")
        return lines
    if output.get("status") == "error":
        lines.append(f"error: {output.get('error_message')}")
        return lines
    if output.get("pr_url"):
        lines.append(f"pull request: {output['pr_url']}")
    for result in output.get("results") or []:
        lines.append(
            f"probe `{result['probe']}` (#{result['issue']}): base exit {result['base_exit_code']}, "
            f"head exit {result['head_exit_code']}"
        )
    if not output.get("results") and output.get("probe_command"):
        lines.append(
            f"probe `{output['probe_command']}`: head exit {output.get('probe_exit_code')}"
            + (
                f", base exit {output['base_probe_exit_code']}"
                if output.get("base_probe_exit_code") is not None
                else ""
            )
        )
    return lines


def digest_lines(report: MetricsReport) -> list[str]:
    return [
        f"window: {report.window_start} .. {report.window_end}",
        f"sessions: {report.automation_sessions_total} "
        f"({report.fix_sessions} fix, {report.verify_sessions} verify)",
        f"merged PRs: {report.sessions_with_merged_pr}, merge rate {report.merge_rate}",
        f"verification: {report.verification_passes}/{report.verification_runs} passed "
        f"(rate {report.verification_pass_rate})",
        f"ACUs: {report.total_acus} total, {report.acu_per_session} per session, "
        f"{report.acu_per_merged_pr} per merged PR",
        f"triage deflections (0 ACU): {report.triage_deflections}",
        f"liveness: {report.liveness}",
    ]


def _digest_due(entries: list[LedgerEntry], now: datetime, every: timedelta) -> bool:
    previous = find(entries, "metrics_digest")
    if not previous:
        return True
    try:
        last = datetime.fromisoformat(previous[-1].at)
    except ValueError:
        return True
    return now - last >= every


def _acus(devin: DevinClient, session: dict[str, Any]) -> float | None:
    session_id = str(session.get("session_id"))
    try:
        return float(devin.session_consumption(session_id).get("total_acus") or 0.0)
    except Exception as exc:  # noqa: BLE001 - a missing consumption row must not block the report
        log.warning("consumption lookup failed for %s: %s", session_id, exc)
        acus = session.get("acus_consumed")
        return float(acus) if acus is not None else None


def run_report(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    target_repo: str,
    deflections: Callable[[], int],
    days: int = 30,
    digest_issue: int | None = None,
    digest_every_hours: int = 24,
    now: datetime | None = None,
) -> ReportReport:
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    sessions = devin.list_sessions(
        origins="automation",
        created_after=start.isoformat(timespec="seconds"),
        created_before=end.isoformat(timespec="seconds"),
    )
    ledger = IssueLedger(gh, target_repo)
    report = ReportReport(digest_issue=digest_issue)

    for session in sessions:
        tags = {str(t) for t in session.get("tags") or []}
        if FIX_TAG in tags:
            kind, threads = "fix", _tagged_numbers(session, "issue-")
        elif VERIFY_TAG in tags:
            kind, threads = "verify", _tagged_numbers(session, "pr-")
        else:
            continue
        if not is_finished(session.get("status"), session.get("status_detail")):
            report.unfinished += 1
            continue

        session_id = str(session.get("session_id"))
        acus = _acus(devin, session)
        lines = outcome_lines(session, acus)
        output = session.get("structured_output") or {}
        verdict = _verdict(session.get("structured_output"))
        for number in threads:
            if find(ledger.read(number), "session_reported", session_id=session_id):
                report.already_reported += 1
                continue
            ledger.append(
                number,
                f"{'Remediation' if kind == 'fix' else 'Verification'} session finished: {verdict}",
                LedgerEntry(
                    "session_reported",
                    data={
                        "session_id": session_id,
                        "kind": kind,
                        "status": session.get("status"),
                        "acceptance_met": output.get("acceptance_met"),
                        "acus": acus,
                    },
                ),
                lines,
            )
            report.posted.append({"thread": number, "kind": kind, "session_id": session_id})
            log.info("REPORT: %s session %s -> #%d", kind, session_id, number)

    if digest_issue is not None and _digest_due(
        ledger.read(digest_issue), end, timedelta(hours=digest_every_hours)
    ):
        summary = metrics.collect(devin, deflections=deflections(), days=days, now=end)
        ledger.append(
            digest_issue,
            "Automation metrics digest",
            # `at` carries the run's clock so the next run's interval check reads this run's time
            LedgerEntry(
                "metrics_digest",
                at=end.isoformat(timespec="seconds"),
                data={"window_end": summary.window_end},
            ),
            digest_lines(summary),
        )
        report.digest_posted = True
        log.info("REPORT: digest appended to #%d", digest_issue)
    return report
