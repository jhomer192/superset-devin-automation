"""REPORT: publish finished session outcomes back onto GitHub.

The Automations API has no completion callback and a session cannot comment on the target repo
itself (the loop's sessions are forbidden from touching it outside their own PR), so a verdict
would otherwise only exist inside the session's structured output. This job polls the Session API
for finished `sda-fix` / `sda-verify` sessions and writes one ledger comment per session onto the
issue or PR it belongs to, then appends a metrics digest at most once per `digest_every_hours`.

A verification that failed also becomes work: REPORT files one regression issue on the target
repo and starts one fix session for it (see `regression`), whose PR re-enters the same loop when
it merges. The verdict comment goes on every PR of the verified window; the remediation happens
once per failed verification, anchored on the PR that triggered it.

Reporting is idempotent the same way the rest of the loop is: a `session_reported` marker keyed by
session id already on the thread means the outcome has been published, and a `regression_filed`
marker keyed by session id on any PR of the window means the failure already has an issue.
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
from .reduce_job import TRIGGER_TAG_PREFIX
from .regression import MAX_CHAIN_DEPTH, chain_depth, file_regression
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


def _is_regression(output: dict[str, Any] | None) -> bool:
    """A verdict of "the merged commit fails a probe", as opposed to a run that never happened."""
    if not output:
        return False
    return output.get("status") == "ok" and output.get("acceptance_met") is False


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


def _acus(devin: DevinClient, session: dict[str, Any], cache: dict[str, float]) -> float | None:
    """ACUs for one session, cached so the digest does not re-bill the same lookup."""
    session_id = str(session.get("session_id"))
    if session_id in cache:
        return cache[session_id]
    try:
        total = float(devin.session_consumption(session_id).get("total_acus") or 0.0)
    except Exception as exc:  # noqa: BLE001 - a missing consumption row must not block the report
        log.warning("consumption lookup failed for %s: %s", session_id, exc)
        acus = session.get("acus_consumed")
        return float(acus) if acus is not None else None
    cache[session_id] = total
    return total


@dataclass(frozen=True)
class VerificationScope:
    """What one verification session covered: the merge that triggered it and the window."""

    trigger_pr: int
    window_prs: list[int]
    head_sha: str
    base_sha: str | None


def verification_scope(session: dict[str, Any], ledger: IssueLedger, window: list[int]) -> VerificationScope:
    """Resolve the scope from the session's tags and REDUCE's `verification_started` record.

    The trigger is the `trigger-pr-<n>` tag; the range comes from the ledger entry REDUCE wrote
    on that thread, falling back to the session's own structured output.
    """
    session_id = str(session.get("session_id"))
    triggers = _tagged_numbers(session, TRIGGER_TAG_PREFIX)
    # the trigger is the newest merge of the window, so an untagged session resolves to the same PR
    trigger = triggers[-1] if triggers else max(window)
    output = session.get("structured_output") or {}
    head, base = str(output.get("head_sha") or ""), output.get("base_sha")
    window_prs = list(window)
    for entry in find(ledger.read(trigger), "verification_started", session_id=session_id):
        head = str(entry.data.get("head") or head)
        base = entry.data.get("base") or base
        recorded = entry.data.get("window")
        if recorded:
            window_prs = [int(n) for n in recorded]
    return VerificationScope(trigger, window_prs, head, base)


def _remediate(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    ledger: IssueLedger,
    target_repo: str,
    automation_repo: str,
    session: dict[str, Any],
    scope: VerificationScope,
    report: ReportReport,
) -> None:
    """Turn one failed verification into exactly one issue plus the one session that fixes it."""
    session_id = str(session.get("session_id"))
    pr_number = scope.trigger_pr
    if any(
        find(ledger.read(n), "regression_filed", session_id=session_id)
        for n in {pr_number, *scope.window_prs}
    ):
        return
    pr_url = f"https://github.com/{target_repo}/pull/{pr_number}"
    # any merge in the window may be the cause, so the chain is as deep as its deepest member
    depth = max(chain_depth(gh, ledger, target_repo, n) for n in {pr_number, *scope.window_prs}) + 1
    if depth > MAX_CHAIN_DEPTH:
        ledger.append(
            pr_number,
            "Regression not remediated automatically: chain depth exhausted",
            LedgerEntry("regression_escalated", data={"session_id": session_id, "depth": depth}),
            [
                f"{MAX_CHAIN_DEPTH} automated attempts have already failed on this chain; "
                "a human needs to look at it",
            ],
        )
        report.escalated.append({"pr": pr_number, "session_id": session_id, "depth": depth})
        log.warning("REPORT: regression chain on PR #%d exhausted at depth %d", pr_number, depth)
        return

    filed = file_regression(
        devin=devin,
        gh=gh,
        ledger=ledger,
        target_repo=target_repo,
        automation_repo=automation_repo,
        session=session,
        pr_number=pr_number,
        pr_url=pr_url,
        window_prs=scope.window_prs,
        head_sha=scope.head_sha,
        base_sha=scope.base_sha,
        depth=depth,
    )
    ledger.append(
        pr_number,
        "Regression filed from the failed verification",
        LedgerEntry(
            "regression_filed",
            data={
                "session_id": session_id,
                "issue": filed["issue"],
                "fix_session_id": filed["session_id"],
                "depth": depth,
                "window": list(scope.window_prs),
            },
        ),
        [
            f"issue: {filed['issue_url']}",
            f"fix session: `{filed['session_id']}`",
            f"failing probes: {', '.join(filed['probes'])}",
            f"window: {', '.join(f'#{n}' for n in scope.window_prs)}",
        ],
    )
    report.regressions_filed.append({"pr": pr_number, **filed})


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
            kind, threads = "fix", _tagged_numbers(session, "issue-")
        elif VERIFY_TAG in tags:
            kind, threads = "verify", _tagged_numbers(session, "pr-")
        else:
            continue
        if not is_finished(session.get("status"), session.get("status_detail")):
            report.unfinished += 1
            continue

        session_id = str(session.get("session_id"))
        pending = [n for n in threads if not find(ledger.read(n), "session_reported", session_id=session_id)]
        report.already_reported += len(threads) - len(pending)
        # ACUs are only needed for a comment that is about to be written, and the lookup is one
        # HTTP request per session on a job that runs every hour.
        acus = _acus(devin, session, acu_cache) if pending else None
        lines = outcome_lines(session, acus) if pending else []
        output = session.get("structured_output") or {}
        verdict = _verdict(session.get("structured_output"))
        for number in pending:
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

        if kind == "verify" and threads and _is_regression(session.get("structured_output")):
            _remediate(
                devin=devin,
                gh=gh,
                ledger=ledger,
                target_repo=target_repo,
                automation_repo=automation_repo,
                session=session,
                scope=verification_scope(session, ledger, threads),
                report=report,
            )

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
