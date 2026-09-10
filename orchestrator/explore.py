"""Exploratory issue finding: the tier that files defects nobody has specified yet.

The verification tier only proves what the registry already names. After a verification has run
on a merge, one exploration session boots the same HEAD, walks the app as each SECURITY.md role,
reads the logs and reports reproduced candidates. The orchestrator files each one as an
`sda-candidate` issue carrying the reproduction and a proposed probe script.

A candidate is not a regression: nothing fixes it or guards it until a human promotes it (adds
the probe under probes/ and the issue to probes/registry.json). Agent judgement never reaches the
fix loop on its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .devin_api import DevinClient
from .github_api import GitHubClient
from .ledger import IssueLedger, LedgerEntry, find
from .prompts import exploration_prompt
from .registry import Registry
from .schema import EXPLORE_SCHEMA
from .sessions import wait_until_finished

log = logging.getLogger(__name__)

EXPLORE_TAG = "sda-explore"
CANDIDATE_LABEL = "sda-candidate"


@dataclass
class ExploreReport:
    session_id: str | None = None
    skipped_reason: str | None = None
    booted: bool | None = None
    areas_covered: list[str] = field(default_factory=list)
    filed: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def known_issues(gh: GitHubClient, registry: Registry, target_repo: str) -> list[tuple[int, str]]:
    """Registry issues plus open candidates: what the session must not report again."""
    known = [(spec.number, spec.title) for spec in registry.issues]
    for issue in gh.list_issues(target_repo, labels=CANDIDATE_LABEL, state="open"):
        known.append((int(issue["number"]), str(issue.get("title") or "")))
    return known


def open_candidates_by_fingerprint(gh: GitHubClient, ledger: IssueLedger, target_repo: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for issue in gh.list_issues(target_repo, labels=CANDIDATE_LABEL, state="open"):
        number = int(issue["number"])
        for entry in find(ledger.read(number), "candidate_filed"):
            out.setdefault(str(entry.data.get("fingerprint") or ""), number)
    return out


def candidate_title(candidate: dict[str, Any]) -> str:
    return f"[{candidate['category']}/{candidate['severity']}] {candidate['title']}"


def candidate_body(
    *,
    target_repo: str,
    automation_repo: str,
    head_sha: str,
    pr_url: str,
    session_url: str,
    candidate: dict[str, Any],
) -> str:
    security = ""
    if candidate.get("security_matrix_row") or candidate.get("attacker_role"):
        security = (
            f"\nSECURITY.md matrix row: {candidate.get('security_matrix_row') or 'not named'}\n"
            f"Attacker role held: {candidate.get('attacker_role') or 'not named'}\n"
        )
    return f"""Candidate found by the exploratory session {session_url} on `{head_sha}` after {pr_url} merged.

Location: {candidate["location"]}
Fingerprint: `{candidate["fingerprint"]}`
{security}
## Reproduction

{candidate["repro"]}

Expected: {candidate["expected"]}

Actual: {candidate["actual"]}

<details><summary>Evidence</summary>

```
{str(candidate["evidence"])[-8000:]}
```
</details>

## Proposed probe (`{candidate["probe_kind"]}`, exits non-zero at `{head_sha[:10]}`)

```bash
{candidate["probe_script"]}
```

## Promotion

This issue is a candidate: the loop neither fixes nor guards it until a human promotes it. To
promote, in https://github.com/{automation_repo}: save the probe as
`probes/issue_<n>/probe_<name>.sh`, add this issue with that probe to `probes/registry.json`,
run `python verify/gen_issues_md.py`, and open a PR. Once merged, `python -m orchestrator autopr`
starts the fix session and every later verification guards the probe. Close the issue as not
planned if the candidate does not hold up.
"""


def run_exploration(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    ledger: IssueLedger,
    target_repo: str,
    automation_repo: str,
    pr_number: int,
    pr_url: str,
    head_sha: str,
    key: str,
    wait: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> ExploreReport:
    """One exploration per verified merge (keyed like the verification), then file its candidates."""
    report = ExploreReport()
    entries = ledger.read(pr_number)
    started = find(entries, "exploration_started", key=key)
    if started:
        session_id = str(started[-1].data.get("session_id") or "")
        log.info("EXPLORE: %s already started for %s", session_id, key)
    else:
        session = devin.create_session(
            {
                "prompt": exploration_prompt(
                    target_repo=target_repo,
                    automation_repo=automation_repo,
                    head_sha=head_sha,
                    pr_url=pr_url,
                    known=known_issues(gh, registry, target_repo),
                ),
                "title": f"Explore {target_repo} @ {head_sha[:10]} for new issues",
                "tags": [EXPLORE_TAG, f"pr-{pr_number}"],
                "structured_output_schema": EXPLORE_SCHEMA,
                "structured_output_required": True,
                "resumable": True,
            }
        )
        session_id = str(session["session_id"])
        ledger.append(
            pr_number,
            "Exploratory issue-finding session started",
            LedgerEntry("exploration_started", data={"key": key, "session_id": session_id, "head": head_sha}),
            [
                f"session: `{session_id}`" + (f" ({session['url']})" if session.get("url") else ""),
                f"head `{head_sha}`; candidates are filed with the `{CANDIDATE_LABEL}` label",
            ],
        )
        log.info("EXPLORE: %s -> session %s", key, session_id)
    report.session_id = session_id
    if not wait:
        return report
    finished = wait_until_finished(devin, session_id, sleep)
    output = finished.get("structured_output") or {}
    if not output:
        report.error = "the session finished without emitting its required structured output"
        return report
    if output.get("status") == "error":
        report.error = str(output.get("error_message") or "error")
        return report
    report.booted = bool(output.get("booted"))
    report.areas_covered = [str(a) for a in output.get("areas_covered") or []]
    session_url = str(finished.get("url") or session_id)
    file_candidates(
        gh=gh,
        ledger=ledger,
        target_repo=target_repo,
        automation_repo=automation_repo,
        head_sha=head_sha,
        pr_url=pr_url,
        session_id=session_id,
        session_url=session_url,
        candidates=list(output.get("candidates") or []),
        report=report,
    )
    return report


def file_candidates(
    *,
    gh: GitHubClient,
    ledger: IssueLedger,
    target_repo: str,
    automation_repo: str,
    head_sha: str,
    pr_url: str,
    session_id: str,
    session_url: str,
    candidates: list[dict[str, Any]],
    report: ExploreReport,
) -> None:
    """File each candidate once: by fingerprint against open candidates, by session on replay."""
    existing = open_candidates_by_fingerprint(gh, ledger, target_repo)
    for candidate in candidates:
        fingerprint = str(candidate["fingerprint"])
        if fingerprint in existing:
            number = existing[fingerprint]
            report.duplicates.append({"fingerprint": fingerprint, "issue": number})
            if not find(ledger.read(number), "candidate_seen", session_id=session_id):
                ledger.append(
                    number,
                    "Candidate reproduced again",
                    LedgerEntry(
                        "candidate_seen",
                        data={"session_id": session_id, "head": head_sha, "exploration_of": pr_url},
                    ),
                    [f"exploration {session_url} reproduced this on `{head_sha}` after {pr_url}"],
                )
            continue
        issue = gh.create_issue(
            target_repo,
            candidate_title(candidate),
            candidate_body(
                target_repo=target_repo,
                automation_repo=automation_repo,
                head_sha=head_sha,
                pr_url=pr_url,
                session_url=session_url,
                candidate=candidate,
            ),
            [CANDIDATE_LABEL, str(candidate["category"])],
        )
        number = int(issue["number"])
        ledger.append(
            number,
            "Candidate filed",
            LedgerEntry(
                "candidate_filed",
                data={
                    "fingerprint": fingerprint,
                    "session_id": session_id,
                    "head": head_sha,
                    "exploration_of": pr_url,
                    "severity": candidate["severity"],
                    "probe_kind": candidate["probe_kind"],
                },
            ),
            [f"fingerprint `{fingerprint}`", f"found by {session_url} on `{head_sha}`"],
        )
        existing[fingerprint] = number
        report.filed.append(
            {
                "issue": number,
                "issue_url": str(
                    issue.get("html_url") or f"https://github.com/{target_repo}/issues/{number}"
                ),
                "fingerprint": fingerprint,
                "severity": candidate["severity"],
            }
        )
        log.info("EXPLORE: candidate #%d filed (%s)", number, fingerprint)


def exploration_lines(report: ExploreReport) -> list[str]:
    if report.skipped_reason:
        return [f"exploration: skipped, {report.skipped_reason}"]
    if report.error:
        return [f"exploration: session `{report.session_id}` error: {report.error}"]
    lines = [
        f"exploration: session `{report.session_id}`, booted={report.booted}, "
        f"{len(report.areas_covered)} area(s) covered, {len(report.filed)} candidate(s) filed, "
        f"{len(report.duplicates)} already open"
    ]
    lines.extend(
        f"candidate {row['issue_url']} ({row['severity']}, `{row['fingerprint']}`)" for row in report.filed
    )
    return lines
