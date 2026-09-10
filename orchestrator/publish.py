"""Publish a finished session's verdict onto GitHub, and turn a failed verification into work.

Both TESTING (after waiting for the verification it started) and REPORT (sweeping up sessions
whose TESTING run did not live to see them finish) call into here, so the comment a reviewer sees
and the idempotency markers behind it are the same whichever stage wrote them.

A `session_reported` marker keyed by session id on a thread means the verdict is already there. A
failed verification files one regression issue for the whole window, keyed by the verification
session id on the trigger PR; the issue carries the `sda-regression` label, and that label's
`github:issues` event is what starts the AUTOPR fix session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .devin_api import DevinClient
from .github_api import GitHubClient
from .ledger import IssueLedger, LedgerEntry, find
from .regression import MAX_CHAIN_DEPTH, chain_depth, file_regression_issue

log = logging.getLogger(__name__)

TRIGGER_TAG_PREFIX = "trigger-pr-"


def tagged_numbers(session: dict[str, Any], prefix: str) -> list[int]:
    out = []
    for tag in session.get("tags") or []:
        name = str(tag)
        if name.startswith(prefix) and name[len(prefix) :].isdigit():
            out.append(int(name[len(prefix) :]))
    return sorted(out)


def verdict(output: dict[str, Any] | None) -> str:
    if output is None:
        return "no structured output"
    if output.get("status") == "error":
        return "error"
    return "acceptance met" if output.get("acceptance_met") else "acceptance NOT met"


def is_regression(output: dict[str, Any] | None) -> bool:
    """A verdict of "the merged commit fails a probe", as opposed to a run that never happened."""
    if not output:
        return False
    return output.get("status") == "ok" and output.get("acceptance_met") is False


def outcome_lines(session: dict[str, Any], acus: float | None) -> list[str]:
    """Human-readable body of a report comment: the verdict and the evidence behind it."""
    output = session.get("structured_output")
    lines = [
        f"verdict: **{verdict(output)}**",
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


def session_acus(devin: DevinClient, session: dict[str, Any], cache: dict[str, float]) -> float | None:
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


def post_outcome(
    *,
    ledger: IssueLedger,
    session: dict[str, Any],
    kind: str,
    threads: list[int],
    acus: float | None,
) -> list[int]:
    """Write the verdict comment on every thread that does not have it yet; return those threads."""
    session_id = str(session.get("session_id"))
    pending = [n for n in threads if not find(ledger.read(n), "session_reported", session_id=session_id)]
    if not pending:
        return []
    output = session.get("structured_output") or {}
    lines = outcome_lines(session, acus)
    label = "Remediation" if kind == "fix" else "Verification"
    for number in pending:
        ledger.append(
            number,
            f"{label} session finished: {verdict(session.get('structured_output'))}",
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
        log.info("%s session %s -> #%d", kind, session_id, number)
    return pending


@dataclass(frozen=True)
class VerificationScope:
    """What one verification session covered: the merge that triggered it and the window."""

    trigger_pr: int
    window_prs: list[int]
    head_sha: str
    base_sha: str | None


def verification_scope(session: dict[str, Any], ledger: IssueLedger, window: list[int]) -> VerificationScope:
    """Resolve the scope from the session's tags and TESTING's `verification_started` record.

    The trigger is the `trigger-pr-<n>` tag; the range comes from the ledger entry written on
    that thread, falling back to the session's own structured output.
    """
    session_id = str(session.get("session_id"))
    triggers = tagged_numbers(session, TRIGGER_TAG_PREFIX)
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


def remediate(
    *,
    gh: GitHubClient,
    ledger: IssueLedger,
    target_repo: str,
    automation_repo: str,
    session: dict[str, Any],
    scope: VerificationScope,
) -> dict[str, Any] | None:
    """One failed verification -> exactly one regression issue (or one escalation record).

    Returns the `file_regression_issue` record, `{"escalated": ...}` when the chain is exhausted,
    or None when this failure was already filed.
    """
    session_id = str(session.get("session_id"))
    pr_number = scope.trigger_pr
    if any(
        find(ledger.read(n), "regression_filed", session_id=session_id)
        for n in {pr_number, *scope.window_prs}
    ):
        return None
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
        log.warning("regression chain on PR #%d exhausted at depth %d", pr_number, depth)
        return {"escalated": True, "pr": pr_number, "session_id": session_id, "depth": depth}

    filed = file_regression_issue(
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
                "depth": depth,
                "window": list(scope.window_prs),
            },
        ),
        [
            f"issue: {filed['issue_url']}",
            f"failing probes: {', '.join(filed['probes'])}",
            f"window: {', '.join(f'#{n}' for n in scope.window_prs)}",
            "the issue's `sda-regression` label starts the AUTOPR fix session",
        ],
    )
    return {"pr": pr_number, **filed}


def publish_verification(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    ledger: IssueLedger,
    target_repo: str,
    automation_repo: str,
    session: dict[str, Any],
    threads: list[int],
    acu_cache: dict[str, float],
) -> tuple[list[int], dict[str, Any] | None]:
    """Verdict comment on every PR of the window, then the regression issue if the run failed."""
    acus = session_acus(devin, session, acu_cache) if threads else None
    posted = post_outcome(ledger=ledger, session=session, kind="verify", threads=threads, acus=acus)
    filed = None
    if threads and is_regression(session.get("structured_output")):
        filed = remediate(
            gh=gh,
            ledger=ledger,
            target_repo=target_repo,
            automation_repo=automation_repo,
            session=session,
            scope=verification_scope(session, ledger, threads),
        )
    return posted, filed
