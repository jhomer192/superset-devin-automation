"""Close the loop on a failed verification: file an issue, then fix it from the issue event.

A verification session that reports `acceptance_met == false` has found a probe that fails on
the merged commit. That is a regression in the target repo, and the loop treats it exactly like
any other piece of work it owns: an issue with binding acceptance criteria, then a fix session
whose verdict is a probe exit code.

The two halves run in different automations. TESTING files the issue with the `sda-regression`
label and records what failed in a `regression_depth` ledger entry on the issue. GitHub's
`github:issues` event for that label triggers AUTOPR, which reads the entry back and starts the
one fix session. The filed issue is tagged into the fix session so AUTOPR can publish its outcome
back onto the issue.

A verification covers a window of merges but one failure is one piece of work: exactly one issue
per failed verification, whatever the cadence. The issue names the PR whose merge triggered the
verification and lists the whole window, since any merge in it may be the cause.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .devin_api import DevinClient
from .github_api import GitHubClient, closing_issue_numbers
from .ledger import IssueLedger, LedgerEntry, find
from .prompts import regression_fix_prompt
from .schema import FIX_SCHEMA

log = logging.getLogger(__name__)

FIX_TAG = "sda-fix"
REGRESSION_TAG = "sda-regression"
# The label is the trigger: AUTOPR's `github:issues` condition matches it and nothing else, so
# issues humans file never start a session by themselves.
REGRESSION_LABEL = "sda-regression"
REGRESSION_LABELS = [REGRESSION_LABEL, "regression"]
# A fix that regresses again is filed once more; beyond that the chain stops and waits for a human,
# so a Devin that cannot solve the problem cannot spend the organization's ACUs in a find-and-fix.
MAX_CHAIN_DEPTH = 2


@dataclass(frozen=True)
class Failure:
    """One probe that failed on the merged commit."""

    probe: str
    issue: int | None
    head_exit_code: int | None
    evidence: str
    requirements: tuple[str, ...] = ()


def failures(output: dict[str, Any]) -> list[Failure]:
    """The probes behind an `acceptance_met == false` verdict."""
    results = output.get("results") or []
    failed = [
        Failure(
            probe=str(r.get("probe")),
            issue=r.get("issue"),
            head_exit_code=r.get("head_exit_code"),
            evidence=str(r.get("evidence") or ""),
            requirements=tuple(str(x) for x in r.get("requirements") or []),
        )
        for r in results
        if not r.get("acceptance_met")
    ]
    if failed:
        return failed
    # No per-probe breakdown: the run as a whole failed, so the command line is the evidence.
    return [
        Failure(
            probe=str(output.get("probe_command") or "verify/run_all.sh"),
            issue=None,
            head_exit_code=output.get("probe_exit_code"),
            evidence=str(output.get("evidence") or ""),
        )
    ]


def issue_title(pr_number: int, items: list[Failure]) -> str:
    first = items[0].probe.split()[0]
    more = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    reqs = sorted({r for f in items for r in f.requirements})
    if reqs:
        return f"PRD violated after PR #{pr_number}: {', '.join(reqs)} ({first}{more})"
    return f"PRD verification failed after PR #{pr_number}: {first}{more}"


def issue_body(
    *,
    target_repo: str,
    automation_repo: str,
    pr_url: str,
    window_prs: list[int],
    head_sha: str,
    items: list[Failure],
    session_url: str,
) -> str:
    rows = "\n".join(
        f"| {f.probe} | {', '.join(f.requirements) or '-'} | {f.head_exit_code} |" for f in items
    )
    evidence = "\n\n".join(
        f"<details><summary>{f.probe}</summary>\n\n```\n{f.evidence[-4000:]}\n```\n</details>"
        for f in items
        if f.evidence
    )
    window = "\n".join(f"- https://github.com/{target_repo}/pull/{n}" for n in window_prs)
    return f"""Filed automatically by the PRD verification triggered by {pr_url}.

The merged commit `{head_sha}` fails a probe of a PRD.md requirement. Verification session: {session_url}

| probe | PRD requirement | exit code |
|-------|-----------------|-----------|
{rows}

## Verification window

Every merge in the window is a candidate cause; the triggering PR is only the one that hit the
cadence:

{window}

## Acceptance criteria

Every probe in the table above exits 0 against a checkout of `{target_repo}` at the fixed
revision, run from a clone of https://github.com/{automation_repo}:

```
probes/run.sh <probe id>          # SUPERSET_SRC=<superset checkout> PROBE_PYTHON=<venv python>
```

The probes are the contract: they must not be edited, skipped or relaxed. A change that makes a
probe pass by weakening it does not satisfy this issue.

{evidence}
"""


def chain_depth(gh: GitHubClient, ledger: IssueLedger, target_repo: str, pr_number: int) -> int:
    """How many regressions deep the PR under verification already is.

    A PR that closes a regression issue inherits that issue's depth, so a fix that regresses again
    is one link further down the same chain rather than a fresh failure.
    """
    pr = gh.get_pull(target_repo, pr_number)
    closes = closing_issue_numbers(f"{pr.get('title', '')}\n{pr.get('body', '')}", target_repo)
    depths = [
        int(entry.data.get("depth", 0))
        for number in closes
        for entry in find(ledger.read(number), "regression_depth")
    ]
    return max(depths, default=0)


def file_regression_issue(
    *,
    gh: GitHubClient,
    ledger: IssueLedger,
    target_repo: str,
    automation_repo: str,
    session: dict[str, Any],
    pr_number: int,
    pr_url: str,
    window_prs: list[int],
    head_sha: str,
    depth: int = 0,
) -> dict[str, Any]:
    """Open one regression issue for a failed verification and record what the fix must satisfy.

    `pr_number` is the merge that triggered the verification; `window_prs` is every merge it
    covered, `head_sha` the commit it ran against.
    """
    output = session.get("structured_output") or {}
    items = failures(output)
    session_url = str(session.get("url") or session.get("session_id"))
    issue = gh.create_issue(
        target_repo,
        issue_title(pr_number, items),
        issue_body(
            target_repo=target_repo,
            automation_repo=automation_repo,
            pr_url=pr_url,
            window_prs=window_prs,
            head_sha=head_sha,
            items=items,
            session_url=session_url,
        ),
        [label for label in REGRESSION_LABELS if label != REGRESSION_LABEL],
    )
    number = int(issue["number"])
    issue_url = str(issue.get("html_url") or f"https://github.com/{target_repo}/issues/{number}")
    probes = [f.probe for f in items]
    ledger.append(
        number,
        "Regression lineage",
        LedgerEntry(
            "regression_depth",
            data={
                "depth": depth,
                "regression_of": pr_url,
                "head": head_sha,
                "window": list(window_prs),
                "probes": probes,
                "verification_session_id": session.get("session_id"),
            },
        ),
        [f"chain depth {depth} of {MAX_CHAIN_DEPTH}", f"deciding probes: {', '.join(probes)}"],
    )
    # the trigger label goes on last: its github:issues event runs AUTOPR, which needs the record
    gh.add_labels(target_repo, number, [REGRESSION_LABEL])
    log.info("regression #%d filed for %s", number, pr_url)
    return {
        "issue": number,
        "issue_url": issue_url,
        "probes": probes,
        "depth": depth,
        "window": list(window_prs),
    }


def regression_record(entries: list[LedgerEntry]) -> LedgerEntry | None:
    """The `regression_depth` entry TESTING wrote when it filed the issue, if this is one."""
    found = find(entries, "regression_depth")
    return found[-1] if found else None


def start_regression_fix(
    *,
    devin: DevinClient,
    ledger: IssueLedger,
    target_repo: str,
    automation_repo: str,
    issue: dict[str, Any],
    record: LedgerEntry,
    trigger: str,
    playbook_id: str | None = None,
) -> dict[str, Any]:
    """Start the one fix session for a regression issue, from what TESTING recorded on it."""
    number = int(issue["number"])
    issue_url = str(issue.get("html_url") or f"https://github.com/{target_repo}/issues/{number}")
    pr_url = str(record.data.get("regression_of") or "")
    head_sha = str(record.data.get("head") or "")
    probes = [str(p) for p in record.data.get("probes") or []]
    fix = devin.create_session(
        {
            "prompt": regression_fix_prompt(
                target_repo=target_repo,
                automation_repo=automation_repo,
                issue_number=number,
                issue_url=issue_url,
                pr_url=pr_url,
                head_sha=head_sha,
                probes=probes,
                playbook_id=playbook_id,
            ),
            "title": f"Fix regression {target_repo}#{number} from {pr_url}",
            "tags": [FIX_TAG, REGRESSION_TAG, f"issue-{number}"],
            "structured_output_schema": FIX_SCHEMA,
            "structured_output_required": True,
            "resumable": True,
        }
    )
    fix_id = str(fix["session_id"])
    ledger.append(
        number,
        "Remediation session started",
        LedgerEntry(
            "session_started",
            data={
                "session_id": fix_id,
                "kind": "fix",
                "regression_of": pr_url,
                "window": list(record.data.get("window") or []),
                "depth": record.data.get("depth", 0),
                "trigger": trigger,
            },
        ),
        [
            f"session: `{fix_id}`" + (f" ({fix['url']})" if fix.get("url") else ""),
            f"deciding probes: {', '.join(probes)}",
        ],
    )
    log.info("%s: regression #%d -> session %s", trigger, number, fix_id)
    return {"issue": number, "session_id": fix_id, "probes": probes}
