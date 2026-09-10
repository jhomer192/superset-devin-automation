"""CLI: python -m orchestrator SUBCOMMAND [--simulate]

Subcommands: cycle, testing, autopr, register, register-playbooks, simulate.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import automations, playbooks
from .autopr_job import run_autopr
from .config import Settings, load_settings
from .cycle import run_cycle
from .devin_api import DevinClient, LiveDevinClient
from .github_api import GitHubClient, LiveGitHubClient
from .registry import Registry, load_registry
from .simulate import FakeDevin, FakeGitHub, issue_event, load_event, pr_number
from .testing_job import run_testing

log = logging.getLogger("orchestrator")


def _clients(settings: Settings) -> tuple[DevinClient, GitHubClient]:
    if settings.simulate:
        return FakeDevin(), FakeGitHub.from_fixtures()
    settings.require_live()
    return (
        LiveDevinClient(settings.devin_api_base, settings.devin_api_key, settings.devin_org_id),
        LiveGitHubClient(settings.github_token),
    )


def cmd_autopr(
    settings: Settings,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    event_json: Path | None = None,
    event: dict[str, Any] | None = None,
    wait: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """With an issue event: the fix session for that regression issue. Without: the Friday sweep."""
    if event_json is not None and event is None:
        event = load_event(event_json)
    kwargs: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
    return run_autopr(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=settings.target_repo,
        automation_repo=settings.automation_repo,
        ready_label=settings.ready_label,
        playbook_id=settings.playbook_id_fix,
        event=event,
        wait=wait,
        **kwargs,
    ).as_dict()


def cmd_testing(
    settings: Settings,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    event_json: Path | None,
    event: dict[str, Any] | None = None,
    wait: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    if event_json is None and event is None and not settings.simulate:
        raise SystemExit("testing requires --event-json <path> unless --simulate")
    event = event if event is not None else load_event(event_json)
    kwargs: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
    return run_testing(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=settings.target_repo,
        automation_repo=settings.automation_repo,
        event=event,
        verify_branch=settings.verify_branch,
        every_n=settings.verify_every_n_merges,
        playbook_id=settings.playbook_id_verify,
        wait=wait,
        **kwargs,
    ).as_dict()


def cmd_cycle(
    settings: Settings,
    devin: DevinClient,
    gh: GitHubClient,
    registry: Registry,
    event_json: Path | None,
    event: dict[str, Any] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    if event_json is None and event is None and not settings.simulate:
        raise SystemExit("cycle requires --event-json <path> unless --simulate")
    event = event if event is not None else load_event(event_json)
    kwargs: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
    return run_cycle(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=settings.target_repo,
        automation_repo=settings.automation_repo,
        event=event,
        verify_branch=settings.verify_branch,
        every_n=settings.verify_every_n_merges,
        issue_window_hours=settings.cycle_issue_window_hours,
        playbook_id=settings.playbook_id_verify,
        **kwargs,
    ).as_dict()


def cmd_register(settings: Settings, devin: DevinClient, dry_run: bool) -> list[dict[str, Any]]:
    return automations.register(
        devin,
        settings.target_repo,
        settings.automation_repo,
        verify_branch=settings.verify_branch,
        every_n=settings.verify_every_n_merges,
        issue_window_hours=settings.cycle_issue_window_hours,
        playbook_id_fix=settings.playbook_id_fix,
        playbook_id_verify=settings.playbook_id_verify,
        dry_run=dry_run,
    )


def cmd_register_playbooks(devin: DevinClient, dry_run: bool) -> dict[str, Any]:
    results = playbooks.register(devin, dry_run=dry_run)
    ids = playbooks.lookup_ids(devin) if not dry_run else {}
    return {"playbooks": results, "env": ids}


def cmd_simulate(settings: Settings, registry: Registry) -> dict[str, Any]:
    """The whole chain, offline: Friday AUTOPR sweep, a fix landing, TESTING on the merges, a failed
    verification filing a regression issue, that issue's event starting AUTOPR.

    Everything printed here is produced by the same code paths the live automations run; only
    the Devin and GitHub clients are in-memory doubles, and the fake sessions "finish" while
    TESTING and AUTOPR are waiting on them.
    """
    devin, gh = FakeDevin(), FakeGitHub.from_fixtures()
    out: dict[str, Any] = {}

    event = load_event()
    pr = event["pull_request"]

    def finish_fixes(_seconds: float) -> None:
        """Stand-in for time.sleep inside AUTOPR's wait. The fix for #5 lands a PR, the other
        Friday fixes error out, and a regression fix opens a PR that closes its issue."""
        for sid, session in list(devin.sessions.items()):
            if "sda-fix" not in session["tags"] or session["structured_output"] is not None:
                continue
            issue = next(int(t[6:]) for t in session["tags"] if t.startswith("issue-"))
            if issue == 5:
                devin.advance(sid, outcome="ok", acus=3.2, pr_url=pr["html_url"])
                gh.pulls[int(pr["number"])] = pr
                gh.close_completed(5)
            elif registry.by_number(issue) is not None:
                devin.advance(sid, outcome="error", acus=0.4)
            else:
                fix_pr = gh.open_pull(960, title=f"fix: regression from #{issue}", body=f"Closes #{issue}")
                devin.advance(sid, outcome="ok", acus=4.5, pr_url=fix_pr["html_url"])

    out["friday_1_autopr_sweep"] = cmd_autopr(settings, devin, gh, registry, wait=True, sleep=finish_fixes)
    out["friday_1_autopr_rerun_retries_only_the_errored"] = cmd_autopr(settings, devin, gh, registry)

    def finish_verification(outcome: str) -> Callable[[float], None]:
        """Stand-in for time.sleep inside TESTING's wait: the pending verification finishes."""

        def _sleep(_seconds: float) -> None:
            for sid, session in devin.sessions.items():
                if "sda-verify" in session["tags"] and session["structured_output"] is None:
                    devin.advance(sid, outcome=outcome, acus=6.1)

        return _sleep

    # Cadence: with VERIFY_EVERY_N_MERGES=n, the n-1 merges before the fix are counted, not verified.
    branch = settings.verify_branch
    n = settings.verify_every_n_merges
    gh.pulls.pop(int(pr["number"]))
    gh.branch_heads[branch] = pr["base"]["sha"]
    out["merges_counted_not_verified"] = []
    for i in range(1, n):
        filler = gh.merge(900 + i, branch=branch, sha=f"{i:040x}", title=f"chore: unrelated merge {i}")
        filler_event = {**event, "number": filler["number"], "pull_request": filler}
        result = cmd_testing(settings, devin, gh, registry, None, event=filler_event)
        out["merges_counted_not_verified"].append(
            {"pr": filler["number"], "merge_index": result["merge_index"], "reason": result["skipped_reason"]}
        )
    event["pull_request"] = gh.merge(
        int(pr["number"]), branch=branch, sha=pr["merge_commit_sha"], title=pr["title"], body=pr["body"]
    )
    event["pull_request"]["html_url"] = pr["html_url"]

    out["merge_testing"] = cmd_testing(
        settings, devin, gh, registry, None, event=event, wait=True, sleep=finish_verification("ok")
    )
    out["merge_testing_replay_is_deduplicated"] = cmd_testing(
        settings, devin, gh, registry, None, event=event
    )
    verify_sid = out["merge_testing"]["session_id"]
    out["verification_structured_output"] = devin.get_session(verify_sid)["structured_output"]

    # A later merge regresses: TESTING files the labelled issue, the label event starts AUTOPR.
    # The fillers put the regressing merge on a window boundary whatever the cadence is.
    for i in range(1, n):
        filler = gh.merge(940 + i, branch=branch, sha=f"{940 + i:040x}", title=f"chore: merge {i}")
        cmd_testing(settings, devin, gh, registry, None, event={**event, "pull_request": filler})
    bad = gh.merge(950, branch=branch, sha=f"{950:040x}", title="feat: a change that regresses")
    bad_event = {**event, "number": bad["number"], "pull_request": bad}
    out["regressing_merge_testing"] = cmd_testing(
        settings, devin, gh, registry, None, event=bad_event, wait=True, sleep=finish_verification("failed")
    )
    regression_issue = out["regressing_merge_testing"]["regression_filed"]["issue"]
    labelled = issue_event(gh.get_issue(settings.target_repo, regression_issue), settings.target_repo)
    out["regression_issue_event_autopr"] = cmd_autopr(
        settings, devin, gh, registry, event=labelled, wait=True, sleep=finish_fixes
    )
    out["regression_issue_event_replay_is_deduplicated"] = cmd_autopr(
        settings, devin, gh, registry, event=labelled
    )
    human = gh.create_issue(
        settings.target_repo, "human-filed issue", "not from the loop", ["sda-regression"]
    )
    out["human_labelled_issue_starts_nothing"] = cmd_autopr(
        settings, devin, gh, registry, event=issue_event(human, settings.target_repo)
    )
    out["friday_2_autopr_sweep"] = cmd_autopr(settings, devin, gh, registry)
    out["automations_dry_run"] = [
        {"name": r["name"], "triggers": r["payload"]["triggers"]}
        for r in cmd_register(settings, devin, dry_run=True)
    ]
    # Playbooks: the loop above ran with whatever PLAYBOOK_ID_* the environment had (unset means
    # the inline fallback). Register them into the fake org twice to show title idempotence, then
    # run a fresh sweep with the ids so the @playbook: token path is exercised as well.
    ids = cmd_register_playbooks(devin, dry_run=False)["env"]
    cmd_register_playbooks(devin, dry_run=False)
    out["playbooks_registered"] = ids
    out["playbooks_reregister_is_idempotent"] = len(devin.list_playbooks()) == 2
    with_playbooks = replace(
        settings, playbook_id_fix=ids["PLAYBOOK_ID_FIX"], playbook_id_verify=ids["PLAYBOOK_ID_VERIFY"]
    )
    devin2, gh2 = FakeDevin(), FakeGitHub.from_fixtures()
    sweep_with = cmd_autopr(with_playbooks, devin2, gh2, registry)
    out["autopr_with_playbook"] = {
        "started": len(sweep_with["started"]),
        "prompts_carry_playbook_token": all(
            f"@playbook:{ids['PLAYBOOK_ID_FIX']}" in str(s["prompt"]) for s in devin2.sessions.values()
        ),
    }
    out["issue_comment_ledger"] = {
        f"#{n}": [c["body"].splitlines()[0] for c in cs] for n, cs in sorted(gh.comments.items())
    }
    out["pr_comment_ledger_pr"] = pr_number(pr["html_url"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator")
    parser.add_argument("--simulate", action="store_true", help="in-memory clients, no API keys needed")
    sub = parser.add_subparsers(dest="command", required=True)
    p_autopr = sub.add_parser(
        "autopr", help="issue event: fix session for that regression issue; no event: Friday sweep"
    )
    p_autopr.add_argument("--event-json", type=Path, default=None)
    p_autopr.add_argument(
        "--wait", action="store_true", help="block until the fix sessions finish, post each verdict"
    )
    p_testing = sub.add_parser("testing", help="merged-PR event: start one verification session")
    p_testing.add_argument("--event-json", type=Path, default=None)
    p_testing.add_argument(
        "--wait", action="store_true", help="block until the verdict, publish it, file the regression"
    )
    p_cycle = sub.add_parser(
        "cycle",
        help="merged-PR event: verify, fix every recent regression issue, wait for all, report",
    )
    p_cycle.add_argument("--event-json", type=Path, default=None)
    p_reg = sub.add_parser("register", help="create/update the CYCLE and AUTOPR automations")
    p_reg.add_argument("--dry-run", action="store_true")
    p_pb = sub.add_parser(
        "register-playbooks", help="create/update the remediation and verification playbooks"
    )
    p_pb.add_argument("--dry-run", action="store_true")
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
        registering = args.command in {"register", "register-playbooks"}
        if registering and not args.dry_run and settings.simulate:
            raise SystemExit(f"{args.command} without --dry-run needs live credentials")
        devin, gh = _clients(settings) if not (registering and args.dry_run) else (FakeDevin(), FakeGitHub())
        if args.command == "cycle":
            result = cmd_cycle(settings, devin, gh, registry, args.event_json)
        elif args.command == "autopr":
            result = cmd_autopr(settings, devin, gh, registry, args.event_json, wait=args.wait)
        elif args.command == "testing":
            result = cmd_testing(settings, devin, gh, registry, args.event_json, wait=args.wait)
        elif args.command == "register-playbooks":
            result = cmd_register_playbooks(devin, args.dry_run)
        else:
            result = cmd_register(settings, devin, args.dry_run)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
