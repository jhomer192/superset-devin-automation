"""CLI: python -m orchestrator {map,reduce,report,metrics,register,simulate} [--simulate]"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import automations, metrics
from .config import Settings, load_settings
from .devin_api import DevinClient, LiveDevinClient
from .github_api import GitHubClient, LiveGitHubClient
from .ledger import IssueLedger, find
from .map_job import run_map
from .reduce_job import run_reduce
from .registry import Registry, load_registry
from .report_job import run_report
from .simulate import FakeDevin, FakeGitHub, load_event, pr_number

log = logging.getLogger("orchestrator")


def _clients(settings: Settings) -> tuple[DevinClient, GitHubClient]:
    if settings.simulate:
        return FakeDevin(), FakeGitHub.from_fixtures()
    settings.require_live()
    return (
        LiveDevinClient(settings.devin_api_base, settings.devin_api_key, settings.devin_org_id),
        LiveGitHubClient(settings.github_token),
    )


def count_deflections(gh: GitHubClient, registry: Registry, repo: str) -> int:
    ledger = IssueLedger(gh, repo)
    return sum(len(find(ledger.read(spec.number), "triage_deflected")) for spec in registry.issues)


def cmd_map(settings: Settings, devin: DevinClient, gh: GitHubClient, registry: Registry) -> dict[str, Any]:
    return run_map(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=settings.target_repo,
        automation_repo=settings.automation_repo,
        ready_label=settings.ready_label,
    ).as_dict()


def cmd_reduce(
    settings: Settings,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    event_json: Path | None,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if event_json is None and event is None and not settings.simulate:
        raise SystemExit("reduce requires --event-json <path> unless --simulate")
    event = event if event is not None else load_event(event_json)
    return run_reduce(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=settings.target_repo,
        automation_repo=settings.automation_repo,
        event=event,
        verify_branch=settings.verify_branch,
        every_n=settings.verify_every_n_merges,
    ).as_dict()


def cmd_metrics(
    settings: Settings, devin: DevinClient, gh: GitHubClient, registry: Registry, days: int
) -> dict[str, Any]:
    return metrics.collect(
        devin, deflections=count_deflections(gh, registry, settings.target_repo), days=days
    ).as_dict()


def cmd_report(
    settings: Settings, devin: DevinClient, gh: GitHubClient, registry: Registry, days: int
) -> dict[str, Any]:
    return run_report(
        devin=devin,
        gh=gh,
        target_repo=settings.target_repo,
        deflections=lambda: count_deflections(gh, registry, settings.target_repo),
        days=days,
        digest_issue=settings.report_digest_issue,
        digest_every_hours=settings.report_digest_every_hours,
    ).as_dict()


def cmd_register(settings: Settings, devin: DevinClient, dry_run: bool) -> list[dict[str, Any]]:
    return automations.register(
        devin,
        settings.target_repo,
        settings.automation_repo,
        verify_branch=settings.verify_branch,
        every_n=settings.verify_every_n_merges,
        digest_issue=settings.report_digest_issue,
        digest_every_hours=settings.report_digest_every_hours,
        dry_run=dry_run,
    )


def cmd_simulate(settings: Settings, registry: Registry) -> dict[str, Any]:
    """The whole loop, offline: Friday MAP, a fix landing, REDUCE on the merge, REPORT, metrics.

    Everything printed here is produced by the same code paths the live automations run; only
    the Devin and GitHub clients are in-memory doubles.
    """
    devin, gh = FakeDevin(), FakeGitHub.from_fixtures()
    out: dict[str, Any] = {}

    out["friday_1_map"] = cmd_map(settings, devin, gh, registry)
    out["friday_1_map_rerun_is_idempotent"] = cmd_map(settings, devin, gh, registry)

    # Sessions finish: one lands a PR that closes #5, one errors, one is suspended awaiting a human.
    started = out["friday_1_map"]["started"]
    by_issue = {s["issue"]: s["session_id"] for s in started}
    event = load_event()
    pr = event["pull_request"]
    if 5 in by_issue:
        devin.advance(by_issue[5], outcome="ok", acus=3.2, pr_url=pr["html_url"])
        gh.pulls[int(pr["number"])] = pr
        gh.close_completed(5)
    for issue, sid in by_issue.items():
        if issue == 5:
            continue
        if issue % 2 == 0:
            devin.advance(sid, outcome="error", acus=0.4)
        else:
            devin.suspend(sid, "waiting_for_user")

    # Cadence: with VERIFY_EVERY_N_MERGES=n, the n-1 merges before the fix are counted, not verified.
    branch = settings.verify_branch
    n = settings.verify_every_n_merges
    gh.pulls.pop(int(pr["number"]))
    gh.branch_heads[branch] = pr["base"]["sha"]
    out["merges_counted_not_verified"] = []
    for i in range(1, n):
        filler = gh.merge(900 + i, branch=branch, sha=f"{i:040x}", title=f"chore: unrelated merge {i}")
        filler_event = {**event, "number": filler["number"], "pull_request": filler}
        result = cmd_reduce(settings, devin, gh, registry, None, event=filler_event)
        out["merges_counted_not_verified"].append(
            {"pr": filler["number"], "merge_index": result["merge_index"], "reason": result["skipped_reason"]}
        )
    event["pull_request"] = gh.merge(
        int(pr["number"]), branch=branch, sha=pr["merge_commit_sha"], title=pr["title"], body=pr["body"]
    )
    event["pull_request"]["html_url"] = pr["html_url"]

    out["merge_reduce"] = cmd_reduce(settings, devin, gh, registry, None, event=event)
    out["merge_reduce_replay_is_deduplicated"] = cmd_reduce(settings, devin, gh, registry, None, event=event)
    verify_sid = out["merge_reduce"]["session_id"]
    devin.advance(verify_sid, outcome="ok", acus=6.1)
    out["verification_structured_output"] = devin.get_session(verify_sid)["structured_output"]

    # REPORT publishes every finished session's verdict; #1 stands in for the digest tracking issue.
    reporting = replace(settings, report_digest_issue=1)
    out["report"] = cmd_report(reporting, devin, gh, registry, days=30)
    out["report_rerun_is_idempotent"] = cmd_report(reporting, devin, gh, registry, days=30)

    out["friday_2_map"] = cmd_map(settings, devin, gh, registry)
    out["metrics"] = cmd_metrics(settings, devin, gh, registry, days=30)
    out["automations_dry_run"] = [
        {"name": r["name"], "triggers": r["payload"]["triggers"]}
        for r in cmd_register(settings, devin, dry_run=True)
    ]
    out["issue_comment_ledger"] = {
        f"#{n}": [c["body"].splitlines()[0] for c in cs] for n, cs in sorted(gh.comments.items())
    }
    out["pr_comment_ledger_pr"] = pr_number(pr["html_url"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator")
    parser.add_argument("--simulate", action="store_true", help="in-memory clients, no API keys needed")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("map", help="Friday sweep: triage ready issues, start fix sessions")
    p_reduce = sub.add_parser("reduce", help="merged-PR event: start one verification session")
    p_reduce.add_argument("--event-json", type=Path, default=None)
    p_report = sub.add_parser("report", help="publish finished session outcomes to GitHub")
    p_report.add_argument("--days", type=int, default=30)
    p_metrics = sub.add_parser("metrics", help="observability report")
    p_metrics.add_argument("--days", type=int, default=30)
    p_reg = sub.add_parser("register", help="create/update the MAP and REDUCE automations")
    p_reg.add_argument("--dry-run", action="store_true")
    sub.add_parser("simulate", help="run the full loop offline against fixtures")
    args = parser.parse_args(argv)

    settings = load_settings(simulate=args.simulate or args.command == "simulate")
    logging.basicConfig(
        level=settings.log_level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    registry = load_registry()

    result: Any
    if args.command == "simulate":
        result = cmd_simulate(settings, registry)
    else:
        if args.command == "register" and not args.dry_run and settings.simulate:
            raise SystemExit("register without --dry-run needs live credentials")
        devin, gh = (
            _clients(settings)
            if not (args.command == "register" and args.dry_run)
            else (FakeDevin(), FakeGitHub())
        )
        if args.command == "map":
            result = cmd_map(settings, devin, gh, registry)
        elif args.command == "reduce":
            result = cmd_reduce(settings, devin, gh, registry, args.event_json)
        elif args.command == "report":
            result = cmd_report(settings, devin, gh, registry, args.days)
        elif args.command == "metrics":
            result = cmd_metrics(settings, devin, gh, registry, args.days)
        else:
            result = cmd_register(settings, devin, args.dry_run)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
