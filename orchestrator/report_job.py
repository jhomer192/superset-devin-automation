"""REPORT: publish finished session outcomes back onto GitHub, and the metrics digest.

The Automations API has no completion callback and a session cannot comment on the target repo
itself (the loop's sessions are forbidden from touching it outside their own PR), so a verdict
would otherwise only exist inside the session's structured output. TESTING publishes its own
verification verdict because it waits for the session; fix sessions have nobody waiting on them,
so this job polls the Session API for finished `sda-fix` sessions and writes one ledger comment
per session onto the issue it belongs to. It also sweeps `sda-verify` sessions whose TESTING run
did not live to publish them, through the same `publish` code, then appends a metrics digest at
most once per `digest_every_hours`.
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
from .publish import post_outcome, publish_verification, session_acus, tagged_numbers
from .sessions import is_finished

log = logging.getLogger(__name__)


@dataclass
class ReportReport:
    posted: list[dict[str, Any]] = field(default_factory=list)
    regressions_filed: list[dict[str, Any]] = field(default_factory=list)
    escalated: list[dict[str, Any]] = field(default_factory=list)
    unfinished: int = 0
    already_reported: int = 0
    digest_issue: int | None = None
    digest_posted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


def _cost(report: MetricsReport) -> str:
    if report.estimated_cost_usd is None or report.acu_usd is None:
        return "n/a (the API meters ACUs, not money; set ACU_USD to price them)"
    return f"${report.estimated_cost_usd:g} at {report.acu_usd:g} USD/ACU"


def _counts(counts: dict[str, Any]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) or "n/a"


def digest_lines(report: MetricsReport) -> list[str]:
    """The digest body: a markdown table, so the comment reads as a report and not as a repr."""
    rows = [
        ("window", f"{report.window_start} .. {report.window_end}"),
        (
            "sessions",
            f"{report.automation_sessions_total} "
            f"({report.fix_sessions} fix, {report.verify_sessions} verify)",
        ),
        ("merged PRs", f"{report.sessions_with_merged_pr} (merge rate {_num(report.merge_rate)})"),
        (
            "verification passed",
            f"{report.verification_passes}/{report.verification_runs} "
            f"(rate {_num(report.verification_pass_rate)})",
        ),
        ("regressions filed", f"{report.regression_issues} ({report.regression_fix_prs} fix PRs opened)"),
        ("triage deflections (0 ACU)", str(report.triage_deflections)),
        ("ACUs (this loop)", _num(report.total_acus)),
        ("ACUs per session", _num(report.acu_per_session)),
        ("ACUs per merged PR", _num(report.acu_per_merged_pr)),
        ("ACUs polling (REPORT itself)", _num(report.polling_acus)),
        ("ACUs org-wide, all origins", _num(report.org_total_acus)),
        ("cost", _cost(report)),
        ("Devin-authored PRs org-wide", _counts(report.pr_metrics)),
        ("session liveness", _counts(report.liveness)),
    ]
    table = ["| metric | value |", "|--------|-------|"]
    table += [f"| {name} | {value} |" for name, value in rows]
    return table


def _digest_due(entries: list[LedgerEntry], now: datetime, every: timedelta) -> bool:
    previous = find(entries, "metrics_digest")
    if not previous:
        return True
    try:
        last = datetime.fromisoformat(previous[-1].at)
    except ValueError:
        return True
    return now - last >= every


def run_report(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    target_repo: str,
    automation_repo: str,
    deflections: Callable[[], int],
    days: int = 30,
    digest_issue: int | None = None,
    digest_every_hours: int = 24,
    acu_usd: float | None = None,
    now: datetime | None = None,
) -> ReportReport:
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    # Tag filtering happens in the query: REPORT starts a session of its own every hour, and
    # those would otherwise fill the pages this query reads.
    sessions = devin.list_sessions(
        origins="automation",
        tags=[FIX_TAG, VERIFY_TAG],
        created_after=int(start.timestamp()),
        created_before=int(end.timestamp()),
    )
    ledger = IssueLedger(gh, target_repo)
    report = ReportReport(digest_issue=digest_issue)
    acu_cache: dict[str, float] = {}

    for session in sessions:
        tags = {str(t) for t in session.get("tags") or []}
        if FIX_TAG in tags:
            kind, threads = "fix", tagged_numbers(session, "issue-")
        elif VERIFY_TAG in tags:
            kind, threads = "verify", tagged_numbers(session, "pr-")
        else:
            continue
        if not is_finished(session.get("status"), session.get("status_detail")):
            report.unfinished += 1
            continue

        session_id = str(session.get("session_id"))
        if kind == "verify":
            posted, filed = publish_verification(
                devin=devin,
                gh=gh,
                ledger=ledger,
                target_repo=target_repo,
                automation_repo=automation_repo,
                session=session,
                threads=threads,
                acu_cache=acu_cache,
            )
            if filed and filed.get("escalated"):
                report.escalated.append(filed)
            elif filed:
                report.regressions_filed.append(filed)
        else:
            pending = [
                n for n in threads if not find(ledger.read(n), "session_reported", session_id=session_id)
            ]
            # ACUs are only needed for a comment that is about to be written, and the lookup is one
            # HTTP request per session on a job that runs every hour.
            acus = session_acus(devin, session, acu_cache) if pending else None
            posted = post_outcome(ledger=ledger, session=session, kind="fix", threads=pending, acus=acus)
        report.already_reported += len(threads) - len(posted)
        report.posted.extend({"thread": n, "kind": kind, "session_id": session_id} for n in posted)

    if digest_issue is not None and _digest_due(
        ledger.read(digest_issue), end, timedelta(hours=digest_every_hours)
    ):
        summary = metrics.collect(
            devin,
            deflections=deflections(),
            days=days,
            now=end,
            acu_usd=acu_usd,
            sessions=sessions,
            consumption=acu_cache,
        )
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
