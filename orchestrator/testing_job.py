"""TESTING: on a merged PR, start exactly one verification session for (pr_url, merge_commit_sha),
wait for its verdict, publish it on every PR of the window, and file the regression issue that
starts AUTOPR when it failed.

The ledger for a PR is its own comment thread (PRs are issues to the GitHub API), so the
idempotency record survives orchestrator restarts and is visible to reviewers.

Cadence: only PRs merged into `verify_branch` count. The trigger fires on every merge (the
Automations API has no counter), so the counter is derived from GitHub itself: this PR's
position k among all PRs ever merged into the branch. When k % every_n != 0 the run records
"merge k, deferred" on the PR and exits; otherwise it verifies the window of the last `every_n`
merges, with BASE = the first parent of the oldest merge in the window.
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
from .prompts import verification_prompt
from .publish import TRIGGER_TAG_PREFIX, publish_verification, session_acus, verdict
from .registry import Probe, Registry
from .schema import VERIFICATION_SCHEMA
from .sessions import holds_slot, wait_until_finished
from .status import post_run, testing_lines

log = logging.getLogger(__name__)

VERIFY_TAG = "sda-verify"


class NotAMergedPR(ValueError):
    pass


@dataclass
class TestingReport:
    pr_url: str
    merge_commit_sha: str
    base_sha: str | None = None
    closes: list[int] = field(default_factory=list)
    regression_issues: list[int] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    session_id: str | None = None
    skipped_reason: str | None = None
    merge_index: int | None = None
    window_prs: list[int] = field(default_factory=list)
    verdict: str | None = None
    posted_to: list[int] = field(default_factory=list)
    regression_filed: dict[str, Any] | None = None
    status_issue: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def extract_merged_pr(
    event: dict[str, Any], gh: GitHubClient | None = None, target_repo: str | None = None
) -> dict[str, Any]:
    """Validate the github:pull_request payload the way the automation trigger does.

    The payload Devin delivers is a trimmed pull_request (no merge_commit_sha); missing fields
    are filled from the GitHub API when a client is given.
    """
    pr = dict(event.get("pull_request") or {})
    if event.get("action") != "closed" or not pr.get("merged"):
        raise NotAMergedPR("event is not a merged pull_request (action=closed, merged=true)")
    if not pr.get("number"):
        raise NotAMergedPR("pull_request lacks a number")
    if gh is not None and target_repo and not (pr.get("merge_commit_sha") and pr.get("html_url")):
        full = gh.get_pull(target_repo, int(pr["number"]))
        pr = {**full, **{k: v for k, v in pr.items() if v not in (None, "", {})}}
        pr["merge_commit_sha"] = full.get("merge_commit_sha") or pr.get("merge_commit_sha")
    if not pr.get("merge_commit_sha") or not pr.get("html_url"):
        raise NotAMergedPR("pull_request lacks merge_commit_sha or html_url")
    return pr


def dedup_key(pr_url: str, merge_commit_sha: str) -> str:
    return f"{pr_url}@{merge_commit_sha}"


def merge_window(merged: list[dict[str, Any]], number: int, every_n: int) -> tuple[int, list[dict[str, Any]]]:
    """(1-based position of PR `number` among merges, PRs in its window; [] when k % n != 0)."""
    numbers = [int(p["number"]) for p in merged]
    if number not in numbers:
        raise NotAMergedPR(f"PR #{number} is not among the PRs merged into the branch")
    k = numbers.index(number) + 1
    if k % every_n != 0:
        return k, []
    return k, merged[k - every_n : k]


def regression_issue_numbers(
    gh: GitHubClient, registry: Registry, target_repo: str, exclude: set[int]
) -> list[int]:
    """Issues whose fixes already landed: their probes guard against regressions at HEAD."""
    out: list[int] = []
    for spec in registry.issues:
        if spec.number in exclude or not spec.probes:
            continue
        issue = gh.get_issue(target_repo, spec.number)
        if issue.get("state") == "closed" and issue.get("state_reason") == "completed":
            out.append(spec.number)
    return out


def run_testing(
    *,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    target_repo: str,
    automation_repo: str,
    event: dict[str, Any],
    verify_branch: str = "master",
    every_n: int = 5,
    playbook_id: str | None = None,
    wait: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> TestingReport:
    repo_full = str((event.get("repository") or {}).get("full_name") or target_repo)
    pr = extract_merged_pr(event, gh if repo_full.lower() == target_repo.lower() else None, target_repo)
    pr_url = str(pr["html_url"])
    head_sha = str(pr["merge_commit_sha"])
    number = int(pr["number"])
    report = TestingReport(pr_url=pr_url, merge_commit_sha=head_sha)

    if repo_full.lower() != target_repo.lower():
        report.skipped_reason = f"event is for {repo_full}, not {target_repo}"
        return report
    base_ref = str((pr.get("base") or {}).get("ref") or "")
    if base_ref != verify_branch:
        report.skipped_reason = f"PR merged into {base_ref!r}; only {verify_branch!r} is verified"
        return report

    ledger = IssueLedger(gh, target_repo)
    entries = ledger.read(number)
    key = dedup_key(pr_url, head_sha)
    if find(entries, "merge_counted", key=key):
        report.skipped_reason = f"merge already counted for {key}"
        return report
    merged = gh.list_merged_pulls(target_repo, verify_branch)
    k, window = merge_window(merged, number, every_n)
    report.merge_index = k
    if not window:
        report.skipped_reason = (
            f"merge {k} into {verify_branch}: {k % every_n} of {every_n} since last verification"
        )
        ledger.append(
            number,
            "Merge counted, verification deferred",
            LedgerEntry("merge_counted", data={"key": key, "merge_index": k, "every_n": every_n}),
            [f"merge #{k} into `{verify_branch}`; verification runs every {every_n} merges"],
        )
        log.info("TESTING: %s counted (%d %% %d != 0), no session", key, k, every_n)
        return report
    report.window_prs = [int(p["number"]) for p in window]

    oldest = window[0]
    parents = gh.get_commit_parents(target_repo, str(oldest["merge_commit_sha"]))
    base_sha = parents[0] if parents else str((oldest.get("base") or {}).get("sha") or "")
    report.base_sha = base_sha or None

    for entry in reversed(find(entries, "verification_started", key=key)):
        session_id = str(entry.data.get("session_id", ""))
        session = devin.get_session(session_id) if session_id else {}
        if not session:
            continue
        report.session_id = session_id
        if session.get("structured_output"):
            state = "completed"
        elif holds_slot(session.get("status"), session.get("status_detail")):
            state = "in flight"
        else:
            state = "finished without a result"
        if not wait:
            report.skipped_reason = f"verification {session_id} already {state} for {key}"
            return report
        # A replay with --wait picks the earlier session back up: publication is idempotent per
        # session, so an invocation that died mid-wait loses nothing.
        log.info("TESTING: %s already %s for %s, waiting on it", session_id, state, key)
        return _finish(devin, gh, ledger, target_repo, automation_repo, report, session_id, sleep)

    closes: list[int] = []
    for wpr in window:
        text = f"{wpr.get('title', '')}\n{wpr.get('body', '')}"
        closes.extend(n for n in closing_issue_numbers(text, target_repo) if n not in closes)
    report.closes = closes
    regression = regression_issue_numbers(gh, registry, target_repo, set(closes))
    report.regression_issues = regression

    requirements = registry.requirement_ids()
    report.requirements = requirements

    probes: list[Probe] = registry.probes_for(closes)
    regression_probes: list[Probe] = registry.probes_for(regression)
    seen = {p.id for p in probes + regression_probes}
    requirement_probes: list[Probe] = [
        p for _, p in registry.requirement_probes(requirements) if p.id not in seen
    ]

    if not playbook_id:
        log.warning("TESTING: PLAYBOOK_ID_VERIFY unset, using the fully inline verification prompt")
    prompt = verification_prompt(
        target_repo,
        automation_repo,
        head_sha,
        base_sha,
        pr_url,
        closes,
        probes + regression_probes + requirement_probes,
        playbook_id,
        requirements,
    )
    if regression:
        prompt += (
            "\nRegression guards (already-landed fixes, must pass at HEAD; BASE result is recorded "
            "but not required to fail): pass them with "
            f'--regression "{",".join(str(n) for n in regression)}".\n'
        )
    session = devin.create_session(
        {
            "prompt": prompt,
            "title": f"Verify {target_repo} {verify_branch} @ {head_sha[:10]} (merge {k})",
            # `pr-<n>` threads receive the verdict; `trigger-pr-<n>` anchors the one remediation
            "tags": [VERIFY_TAG, f"{TRIGGER_TAG_PREFIX}{number}"] + [f"pr-{p}" for p in report.window_prs],
            "structured_output_schema": VERIFICATION_SCHEMA,
            "structured_output_required": True,
            "resumable": True,
        }
    )
    session_id = str(session["session_id"])
    report.session_id = session_id
    ledger.append(
        number,
        "Regression verification session started",
        LedgerEntry(
            "verification_started",
            data={
                "key": key,
                "session_id": session_id,
                "head": head_sha,
                "base": base_sha,
                "window": list(report.window_prs),
                "requirements": requirements,
            },
        ),
        [
            f"session: `{session_id}`" + (f" ({session['url']})" if session.get("url") else ""),
            f"head `{head_sha}` vs base `{base_sha}`",
            f"window: merges {k - every_n + 1}..{k} into `{verify_branch}` "
            f"({', '.join(f'#{p}' for p in report.window_prs)})",
            f"closes: {', '.join(f'#{n}' for n in closes) or 'none'}",
            f"regression guards: {', '.join(f'#{n}' for n in regression) or 'none'}",
            f"PRD requirements guarded at HEAD: {', '.join(requirements) or 'none'}",
        ],
    )
    log.info("TESTING: %s -> session %s", key, session_id)
    if not wait:
        return report
    return _finish(devin, gh, ledger, target_repo, automation_repo, report, session_id, sleep)


def _finish(
    devin: DevinClient,
    gh: GitHubClient,
    ledger: IssueLedger,
    target_repo: str,
    automation_repo: str,
    report: TestingReport,
    session_id: str,
    sleep: Callable[[float], None],
) -> TestingReport:
    """Wait for the verification, publish its verdict to the window, log the run."""
    head_sha = report.merge_commit_sha
    base_sha = report.base_sha or ""
    finished = wait_until_finished(devin, session_id, sleep)
    report.verdict = verdict(finished.get("structured_output"))
    acu_cache: dict[str, float] = {}
    posted, filed = publish_verification(
        devin=devin,
        gh=gh,
        ledger=ledger,
        target_repo=target_repo,
        automation_repo=automation_repo,
        session=finished,
        threads=list(report.window_prs),
        acu_cache=acu_cache,
    )
    report.posted_to = posted
    report.regression_filed = filed
    log.info("TESTING: %s finished: %s", session_id, report.verdict)
    report.status_issue = post_run(
        gh,
        target_repo,
        "testing",
        [session_id],
        f"TESTING: merge #{report.merge_index} verified: {report.verdict}",
        {
            "head_sha": head_sha,
            "base_sha": base_sha,
            "window": list(report.window_prs),
            "verdict": report.verdict,
            "regression_issue": (filed or {}).get("issue"),
        },
        testing_lines(
            base_sha=base_sha,
            head_sha=head_sha,
            window_prs=list(report.window_prs),
            merge_index=report.merge_index,
            session=finished,
            acus=session_acus(devin, finished, acu_cache),
            regression_filed=filed,
        ),
    )
    return report
