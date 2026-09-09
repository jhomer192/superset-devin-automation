"""Observability: "if I were an engineering leader, how would I know this is working?"

Sources (all in https://docs.devin.ai/v3-openapi.yaml):
  GET /v3/organizations/{org_id}/metrics/sessions?time_after&time_before
        -> sessions_created_count, sessions_with_merged_prs_count, avg_acus_per_session
  GET /v3/organizations/{org_id}/sessions?origins=automation&created_after&created_before
        -> per-session status, pull_requests[].pr_state, acus_consumed, structured_output
  GET /v3/organizations/{org_id}/sessions/insights (same filters)
  GET /v3/organizations/{org_id}/consumption/daily/sessions/{session_id} -> total_acus
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
    liveness: dict[str, int] = field(default_factory=dict)
    insights_count: int = 0
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def _tags(session: dict[str, Any]) -> set[str]:
    return {str(t) for t in session.get("tags") or []}


def compute(
    sessions: list[dict[str, Any]],
    org_metrics: dict[str, Any],
    consumption: dict[str, float],
    deflections: int,
    window: tuple[datetime, datetime],
    insights_count: int = 0,
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
        liveness=liveness,
        insights_count=insights_count,
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
        ],
    )


def collect(
    devin: DevinClient,
    *,
    deflections: int,
    days: int = 30,
    now: datetime | None = None,
    with_consumption: bool = True,
) -> MetricsReport:
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    org_metrics = devin.session_metrics(int(start.timestamp()), int(end.timestamp()))
    sessions = devin.list_sessions(
        origins="automation",
        created_after=start.isoformat(timespec="seconds"),
        created_before=end.isoformat(timespec="seconds"),
    )
    sessions = [s for s in sessions if _tags(s) & {FIX_TAG, VERIFY_TAG}]
    insights = devin.sessions_insights(
        origins="automation",
        created_after=start.isoformat(timespec="seconds"),
        created_before=end.isoformat(timespec="seconds"),
    )
    consumption: dict[str, float] = {}
    if with_consumption:
        for s in sessions:
            sid = str(s.get("session_id"))
            try:
                consumption[sid] = float(devin.session_consumption(sid).get("total_acus") or 0.0)
            except Exception as exc:  # noqa: BLE001 - one bad session must not sink the report
                log.warning("consumption lookup failed for %s: %s", sid, exc)
    return compute(sessions, org_metrics, consumption, deflections, (start, end), len(insights))
