"""Observability: "if I were an engineering leader, how would I know this is working?"

Sources (all in https://docs.devin.ai/v3-openapi.yaml):
  GET /v3/organizations/{org_id}/metrics/sessions?time_after&time_before
        -> sessions_created_count, sessions_with_merged_prs_count, avg_acus_per_session
  GET /v3/organizations/{org_id}/sessions?origins=automation&tags&created_after&created_before
        -> per-session status, pull_requests[].pr_state, acus_consumed, structured_output
  GET /v3/organizations/{org_id}/consumption/daily/sessions/{session_id} -> total_acus
  GET /v3/organizations/{org_id}/consumption/daily -> org-wide total_acus for the window
  GET /v3/organizations/{org_id}/metrics/prs -> PRs Devin authored, by state

The API prices nothing, so a dollar figure only appears when ACU_USD supplies the rate the
organization actually pays; otherwise the report stays in ACUs rather than inventing a number.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .devin_api import DevinClient
from .sessions import Liveness, merged_pr_urls, session_liveness

log = logging.getLogger(__name__)

FIX_TAG = "sda-fix"
VERIFY_TAG = "sda-verify"
REGRESSION_TAG = "sda-regression"
REPORT_TAG = "sda-report"


@dataclass
class MetricsReport:
    window_start: str
    window_end: str
    org_metrics: dict[str, Any]
    automation_sessions_total: int
    fix_sessions: int
    verify_sessions: int
    sessions_with_merged_pr: int
    merged_pr_urls: list[str]
    merge_rate: float | None
    total_acus: float
    acu_per_session: float | None
    acu_per_merged_pr: float | None
    verification_pass_rate: float | None
    verification_runs: int
    verification_passes: int
    triage_deflections: int
    pr_metrics: dict[str, Any] = field(default_factory=dict)
    org_total_acus: float | None = None
    acu_usd: float | None = None
    estimated_cost_usd: float | None = None
    regression_issues: int = 0
    regression_fix_prs: int = 0
    regressions: list[dict[str, Any]] = field(default_factory=list)
    polling_acus: float | None = None
    liveness: dict[str, int] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def _tags(session: dict[str, Any]) -> set[str]:
    return {str(t) for t in session.get("tags") or []}


def _issue_tag(session: dict[str, Any]) -> int | None:
    for tag in _tags(session):
        if tag.startswith("issue-") and tag[len("issue-") :].isdigit():
            return int(tag[len("issue-") :])
    return None


def _regressions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per issue this loop filed against itself, with the PR that answers it."""
    rows = []
    for s in sessions:
        if REGRESSION_TAG not in _tags(s):
            continue
        out = s.get("structured_output") or {}
        rows.append(
            {
                "issue": _issue_tag(s),
                "session_id": s.get("session_id"),
                "status": s.get("status"),
                "pr_url": out.get("pr_url"),
                "acceptance_met": out.get("acceptance_met"),
                "title": s.get("title"),
            }
        )
    return sorted(rows, key=lambda r: (r["issue"] is None, r["issue"] or 0))


def compute(
    sessions: list[dict[str, Any]],
    org_metrics: dict[str, Any],
    consumption: dict[str, float],
    deflections: int,
    window: tuple[datetime, datetime],
    pr_metrics: dict[str, Any] | None = None,
    org_total_acus: float | None = None,
    acu_usd: float | None = None,
    polling_acus: float | None = None,
) -> MetricsReport:
    fix = [s for s in sessions if FIX_TAG in _tags(s)]
    verify = [s for s in sessions if VERIFY_TAG in _tags(s)]
    merged_urls: list[str] = []
    merged_sessions = 0
    for s in fix:
        urls = merged_pr_urls(s)
        if urls:
            merged_sessions += 1
            merged_urls.extend(urls)

    total_acus = sum(
        consumption.get(str(s.get("session_id")), float(s.get("acus_consumed") or 0.0)) for s in sessions
    )
    liveness: dict[str, int] = {lv.value: 0 for lv in Liveness}
    for s in sessions:
        liveness[session_liveness(s).value] += 1

    regressions = _regressions(sessions)
    runs = 0
    passes = 0
    for s in verify:
        out = s.get("structured_output") or {}
        if out.get("status") == "ok" and "acceptance_met" in out:
            runs += 1
            passes += 1 if out["acceptance_met"] else 0

    return MetricsReport(
        window_start=window[0].isoformat(timespec="seconds"),
        window_end=window[1].isoformat(timespec="seconds"),
        org_metrics={
            k: org_metrics.get(k)
            for k in ("sessions_created_count", "sessions_with_merged_prs_count", "avg_acus_per_session")
        },
        automation_sessions_total=len(sessions),
        fix_sessions=len(fix),
        verify_sessions=len(verify),
        sessions_with_merged_pr=merged_sessions,
        merged_pr_urls=sorted(set(merged_urls)),
        merge_rate=_ratio(merged_sessions, len(fix)),
        total_acus=round(total_acus, 3),
        acu_per_session=_ratio(total_acus, len(sessions)),
        acu_per_merged_pr=_ratio(total_acus, len(set(merged_urls))),
        verification_pass_rate=_ratio(passes, runs),
        verification_runs=runs,
        verification_passes=passes,
        triage_deflections=deflections,
        pr_metrics=dict(pr_metrics or {}),
        org_total_acus=org_total_acus,
        acu_usd=acu_usd,
        estimated_cost_usd=round(total_acus * acu_usd, 2) if acu_usd else None,
        regression_issues=len(regressions),
        regression_fix_prs=sum(1 for r in regressions if r["pr_url"]),
        regressions=regressions,
        polling_acus=polling_acus,
        liveness=liveness,
        limitations=[
            "org_metrics come from /metrics/sessions and cover the whole organization for the "
            "window, not only automation sessions; the per-session numbers below are filtered to "
            "origin=automation and to this repo's tags.",
            "merge_rate counts fix sessions whose pull_requests[] contains pr_state=merged; a PR "
            "merged after the query window closes is missed until the next run.",
            "acu_per_merged_pr divides ALL automation ACUs (fix + verify + errored) by merged PRs; "
            "it is a cost-of-outcome number, not a per-PR cost.",
            "triage_deflections is counted from ledger comments on issues, so it needs GitHub "
            "access; it is 0 when the GitHub token is absent.",
            "the API reports ACUs, not money; estimated_cost_usd is total_acus x ACU_USD and is "
            "null unless that rate is configured.",
            "pr_metrics and org_total_acus come from org-wide endpoints and include work this "
            "automation did not do.",
            "polling_acus is what REPORT spends on itself: one session per hour whether or not "
            "anything finished. It is read from the sessions list, not the consumption endpoint.",
        ],
    )


def collect(
    devin: DevinClient,
    *,
    deflections: int,
    days: int = 30,
    now: datetime | None = None,
    acu_usd: float | None = None,
    sessions: list[dict[str, Any]] | None = None,
    consumption: dict[str, float] | None = None,
) -> MetricsReport:
    """`sessions` and `consumption` let a caller that already listed the window pass it in
    rather than paying for the same requests twice."""
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    after, before = int(start.timestamp()), int(end.timestamp())
    org_metrics = devin.session_metrics(after, before)
    pr_metrics = devin.pr_metrics(after, before)
    org_total_acus = float(devin.org_consumption(after, before).get("total_acus") or 0.0)
    if sessions is None:
        sessions = devin.list_sessions(
            origins="automation",
            tags=[FIX_TAG, VERIFY_TAG],
            created_after=after,
            created_before=before,
        )
    sessions = [s for s in sessions if _tags(s) & {FIX_TAG, VERIFY_TAG}]
    polling = devin.list_sessions(
        origins="automation", tags=[REPORT_TAG], created_after=after, created_before=before
    )
    acus = dict(consumption or {})
    for s in sessions:
        sid = str(s.get("session_id"))
        if sid in acus:
            continue
        try:
            acus[sid] = float(devin.session_consumption(sid).get("total_acus") or 0.0)
        except Exception as exc:  # noqa: BLE001 - one bad session must not sink the report
            log.warning("consumption lookup failed for %s: %s", sid, exc)
    return compute(
        sessions,
        org_metrics,
        acus,
        deflections,
        (start, end),
        pr_metrics=pr_metrics,
        org_total_acus=org_total_acus,
        acu_usd=acu_usd,
        polling_acus=round(sum(float(s.get("acus_consumed") or 0.0) for s in polling), 3),
    )
