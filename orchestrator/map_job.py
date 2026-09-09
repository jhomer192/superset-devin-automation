"""MAP: Friday sweep. Triage every open `ready` issue, deflect at zero ACU, start one fix
session per eligible issue that does not already have one in flight."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .devin_api import DevinClient
from .github_api import GitHubClient, closing_issue_numbers
from .ledger import IssueLedger, LedgerEntry, find
from .prompts import fix_session_prompt
from .registry import Registry
from .schema import FIX_SCHEMA
from .sessions import holds_slot
from .triage import Decision, Triage, classify

log = logging.getLogger(__name__)

FIX_TAG = "sda-fix"


@dataclass
class MapReport:
    scanned: int = 0
    started: list[dict[str, Any]] = field(default_factory=list)
    deflected: list[dict[str, Any]] = field(default_factory=list)
    skipped_in_flight: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "started": self.started,
            "deflected": self.deflected,
            "skipped_in_flight": self.skipped_in_flight,
        }


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


def run_map(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    target_repo: str,
    automation_repo: str,
    ready_label: str,
) -> MapReport:
    report = MapReport()
    ledger = IssueLedger(gh, target_repo)
    issues = gh.list_issues(target_repo, labels=ready_label, state="open")
    open_prs = gh.list_pulls(target_repo, state="open")
    report.scanned = len(issues)
    log.info("MAP: %d open issues labelled %r", len(issues), ready_label)

    for issue in sorted(issues, key=lambda i: int(i["number"])):
        number = int(issue["number"])
        spec = registry.by_number(number)
        triage = classify(issue, spec)
        entries = ledger.read(number)

        if triage.decision is Decision.DEFLECT:
            record = {"issue": number, "reason": triage.reason.value, "detail": triage.detail}
            report.deflected.append(record)
            if _already_deflected(entries, triage):
                log.info("MAP: #%d deflected (%s), already logged", number, triage.reason.value)
                continue
            ledger.append(
                number,
                "Triage: deflected, no session started (0 ACU)",
                LedgerEntry("triage_deflected", data={"reason": triage.reason.value, "acu": 0}),
                [f"reason: `{triage.reason.value}`", triage.detail],
            )
            log.info("MAP: #%d deflected (%s)", number, triage.reason.value)
            continue

        assert spec is not None
        reason = _in_flight_reason(number, entries, open_prs, devin, target_repo)
        if reason:
            report.skipped_in_flight.append({"issue": number, "reason": reason})
            log.info("MAP: #%d skipped, %s", number, reason)
            continue

        session = devin.create_session(
            {
                "prompt": fix_session_prompt(target_repo, automation_repo, issue, spec),
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
        log.info("MAP: #%d -> session %s", number, session_id)
    return report
