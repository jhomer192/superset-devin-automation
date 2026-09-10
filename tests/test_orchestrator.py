import copy
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator import automations, devin_api, metrics, playbooks, prompts
from orchestrator.__main__ import cmd_simulate, count_deflections
from orchestrator.config import load_settings
from orchestrator.github_api import closing_issue_numbers
from orchestrator.ledger import IssueLedger, LedgerEntry, find, parse_entries
from orchestrator.map_job import run_map
from orchestrator.reduce_job import NotAMergedPR, extract_merged_pr, run_reduce
from orchestrator.registry import load_registry
from orchestrator.report_job import digest_lines, run_report
from orchestrator.schema import FIX_SCHEMA
from orchestrator.sessions import Liveness, classify, holds_slot, is_finished
from orchestrator.simulate import FakeDevin, FakeGitHub, load_event
from orchestrator.triage import Decision, Reason
from orchestrator.triage import classify as triage

REPO = "jhomer192/superset"
AUTO = "jhomer192/superset-devin-automation"


@pytest.fixture
def registry():
    return load_registry()


@pytest.fixture
def world():
    return FakeDevin(), FakeGitHub.from_fixtures()


def do_map(devin, gh, registry):
    return run_map(
        devin=devin, gh=gh, registry=registry, target_repo=REPO, automation_repo=AUTO, ready_label="ready"
    )


def do_reduce(devin, gh, registry, event=None):
    """REDUCE at every_n=1 against the single-merge fixture, so one event is one verification."""
    return run_reduce(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        event=event or load_event(),
        every_n=1,
    )


def run_reduce_n(devin, gh, registry, event, *, branch, n):
    return run_reduce(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        event=event,
        verify_branch=branch,
        every_n=n,
    )


# --- registry ---------------------------------------------------------------------------------


def test_registry_covers_every_issue_and_every_probe_script_exists(registry):
    assert [i.number for i in registry.issues] == [*range(1, 13), 15]
    for n in (2, 4, 6, 8):
        spec = registry.by_number(n)
        assert spec.state == "closed" and spec.state_reason == "not_planned" and spec.not_planned_reason
    for spec in registry.issues:
        assert spec.condition and spec.category


# --- session taxonomy -------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["new", "claimed", "running", "resuming"])
def test_live_statuses_hold_slot(status):
    assert classify(status, None) is Liveness.LIVE
    assert holds_slot(status, None) and not is_finished(status, None)


@pytest.mark.parametrize("status", ["exit", "error"])
def test_dead_statuses_release_slot(status):
    assert classify(status, None) is Liveness.DEAD
    assert not holds_slot(status, None) and is_finished(status, None)


@pytest.mark.parametrize("detail", ["waiting_for_user", "waiting_for_approval", "inactivity"])
def test_suspended_awaiting_human_holds_slot(detail):
    assert classify("suspended", detail) is Liveness.AWAITING_HUMAN
    assert holds_slot("suspended", detail)


@pytest.mark.parametrize(
    "detail",
    [
        "usage_limit_exceeded",
        "out_of_credits",
        "out_of_quota",
        "no_quota_allocation",
        "payment_declined",
        "org_usage_limit_exceeded",
        "user_usage_limit_exceeded",
        "total_session_limit_exceeded",
    ],
)
def test_suspended_terminal_releases_slot(detail):
    assert classify("suspended", detail) is Liveness.TERMINAL_SUSPENDED
    assert not holds_slot("suspended", detail)


def test_unknown_suspended_detail_is_conservative():
    assert classify("suspended", "something_new") is Liveness.UNKNOWN_SUSPENDED
    assert holds_slot("suspended", "something_new")


# --- triage -----------------------------------------------------------------------------------


def test_triage_deflects_dependency_refresh_honestly(registry, world):
    _, gh = world
    t = triage(gh.get_issue(REPO, 12), registry.by_number(12))
    assert t.decision is Decision.DEFLECT and t.reason is Reason.DEPENDENCY_REFRESH
    assert "Dependabot is disabled" in t.detail
    assert "bot that will not come" in t.detail


def test_triage_requires_acceptance_criteria_and_probe(registry, world):
    _, gh = world
    issue = gh.get_issue(REPO, 5)
    assert triage(issue, registry.by_number(5)).decision is Decision.SESSION
    no_ac = {**issue, "body": "please fix it"}
    assert triage(no_ac, registry.by_number(5)).reason is Reason.NO_ACCEPTANCE_CRITERIA
    assert triage(issue, None).reason is Reason.NO_COMMITTED_PROBE


def test_every_ready_issue_is_classified_before_any_session(registry, world):
    devin, gh = world
    ready = gh.list_issues(REPO, labels="ready")
    report = do_map(devin, gh, registry)
    assert report.scanned == len(ready) == 9
    assert {d["issue"] for d in report.deflected} == {12, 15}
    assert {s["issue"] for s in report.started} == {1, 3, 5, 7, 9, 10, 11}
    assert len(devin.sessions) == 7
    for s in devin.sessions.values():
        assert "max_acu_limit" not in json.dumps(s)


# --- idempotency ------------------------------------------------------------------------------


def test_map_rerun_starts_nothing_while_sessions_hold_slots(registry, world):
    devin, gh = world
    do_map(devin, gh, registry)
    again = do_map(devin, gh, registry)
    assert again.started == [] and len(again.skipped_in_flight) == 7
    assert len(devin.sessions) == 7


def test_map_retries_after_dead_session_but_not_while_awaiting_human(registry, world):
    devin, gh = world
    first = do_map(devin, gh, registry)
    sid = {s["issue"]: s["session_id"] for s in first.started}
    devin.advance(sid[1], outcome="error", acus=0.2)
    devin.suspend(sid[3], "waiting_for_user")
    devin.suspend(sid[7], "out_of_credits")
    again = do_map(devin, gh, registry)
    assert {s["issue"] for s in again.started} == {1, 7}
    assert any(r["issue"] == 3 and "waiting_for_user" in r["reason"] for r in again.skipped_in_flight)


def test_map_skips_issue_with_open_pr_closing_it(registry, world):
    devin, gh = world
    gh.pulls[99] = {
        "number": 99,
        "state": "open",
        "html_url": f"https://github.com/{REPO}/pull/99",
        "title": "x",
        "body": "Closes #9",
    }
    report = do_map(devin, gh, registry)
    assert 9 not in {s["issue"] for s in report.started}
    assert any(r["issue"] == 9 and "pull/99" in r["reason"] for r in report.skipped_in_flight)


def test_deflection_comment_is_written_once(registry, world):
    devin, gh = world
    do_map(devin, gh, registry)
    do_map(devin, gh, registry)
    entries = IssueLedger(gh, REPO).read(12)
    assert len(find(entries, "triage_deflected")) == 1
    assert entries[0].data["acu"] == 0


def test_reduce_dedups_on_pr_url_and_merge_sha(registry, world):
    devin, gh = world
    first = do_reduce(devin, gh, registry)
    assert (
        first.session_id
        and first.closes == [5]
        and first.base_sha == "fc110d8428f35249a2092778ca0a3e26a2de0b14"
    )
    replay = do_reduce(devin, gh, registry)
    assert replay.skipped_reason and replay.session_id == first.session_id
    assert len([s for s in devin.sessions.values() if "sda-verify" in s["tags"]]) == 1

    devin.advance(first.session_id, outcome="ok", acus=5)
    done = do_reduce(devin, gh, registry)
    assert "already completed" in done.skipped_reason

    other = copy.deepcopy(load_event())
    other["pull_request"]["merge_commit_sha"] = "3" * 40
    gh.parents["3" * 40] = ["4" * 40]
    second = do_reduce(devin, gh, registry, other)
    assert second.session_id != first.session_id


def test_reduce_rejects_unmerged_and_foreign_events(registry, world):
    devin, gh = world
    ev = copy.deepcopy(load_event())
    ev["pull_request"]["merged"] = False
    with pytest.raises(NotAMergedPR):
        extract_merged_pr(ev)
    ev = copy.deepcopy(load_event())
    ev["repository"]["full_name"] = "someone/else"
    assert do_reduce(devin, gh, registry, ev).skipped_reason
    assert devin.sessions == {}


def merge_event(gh, number, *, branch, sha, body=""):
    pr = gh.merge(number, branch=branch, sha=sha, title=f"pr {number}", body=body)
    return {"action": "closed", "number": number, "pull_request": pr, "repository": {"full_name": REPO}}


def test_reduce_verifies_every_nth_merge_into_selected_branch(registry, world):
    devin, gh = world
    gh.branch_heads["main"] = "a" * 40
    bodies = {2: "Closes #3", 4: "Closes #11", 5: ""}
    reports = []
    for i in range(1, 6):
        ev = merge_event(gh, 100 + i, branch="main", sha=str(i) * 40, body=bodies.get(i, ""))
        reports.append(run_reduce_n(devin, gh, registry, ev, branch="main", n=5))
    assert [r.merge_index for r in reports] == [1, 2, 3, 4, 5]
    assert all(r.session_id is None and "of 5" in r.skipped_reason for r in reports[:4])
    fifth = reports[4]
    assert fifth.session_id and fifth.window_prs == [101, 102, 103, 104, 105]
    assert fifth.base_sha == "a" * 40 and fifth.merge_commit_sha == "5" * 40
    assert fifth.closes == [3, 11]
    assert len([s for s in devin.sessions.values() if "sda-verify" in s["tags"]]) == 1
    # every non-triggering merge left a durable count on its own PR thread
    for n in (101, 102, 103, 104):
        assert find(IssueLedger(gh, REPO).read(n), "merge_counted")
    # the 10th merge is the next trigger and its window starts after the 5th
    for i in range(6, 11):
        ev = merge_event(gh, 100 + i, branch="main", sha=chr(ord("a") + i) * 40)
        r = run_reduce_n(devin, gh, registry, ev, branch="main", n=5)
    assert r.merge_index == 10 and r.session_id and r.base_sha == "5" * 40
    assert r.window_prs == [106, 107, 108, 109, 110]


def verify_sessions(devin):
    return [s for s in devin.sessions.values() if "sda-verify" in s["tags"]]


def test_cadence_holds_across_windows_at_every_n_5(registry, world):
    devin, gh = world
    gh.branch_heads["main"] = "a" * 40
    shas = {i: f"{i:040x}" for i in range(1, 16)}
    reports = {}
    for i in range(1, 16):
        ev = merge_event(gh, 300 + i, branch="main", sha=shas[i])
        reports[i] = run_reduce_n(devin, gh, registry, ev, branch="main", n=5)
    assert [reports[i].merge_index for i in range(1, 16)] == list(range(1, 16))
    verified = sorted(i for i, r in reports.items() if r.session_id)
    assert verified == [5, 10, 15]
    for i in range(1, 16):
        if i % 5:
            assert reports[i].session_id is None and "of 5" in reports[i].skipped_reason
    assert len(verify_sessions(devin)) == 3
    # each window is the five merges ending at the trigger, BASE is the head before the window
    assert reports[5].window_prs == [301, 302, 303, 304, 305] and reports[5].base_sha == "a" * 40
    assert reports[10].window_prs == [306, 307, 308, 309, 310] and reports[10].base_sha == shas[5]
    assert reports[15].window_prs == [311, 312, 313, 314, 315] and reports[15].base_sha == shas[10]
    assert reports[15].merge_commit_sha == shas[15]


def test_cadence_verification_runs_when_the_window_closes_no_issue(registry, world):
    """The cadence is unconditional: a window with no `Closes #n` still gets the full suite."""
    devin, gh = world
    gh.branch_heads["main"] = "a" * 40
    gh.close_completed(7)  # a fix already landed; its probes are the regression suite
    for i in range(1, 6):
        ev = merge_event(gh, 400 + i, branch="main", sha=f"{i:040x}", body="chore: nothing closed")
        r = run_reduce_n(devin, gh, registry, ev, branch="main", n=5)
    assert r.merge_index == 5 and r.closes == [] and r.session_id
    assert r.regression_issues == [7]
    prompt = devin.sessions[r.session_id]["prompt"]
    assert "issue_7/unit" in prompt and '--regression "7"' in prompt
    assert len(verify_sessions(devin)) == 1


def test_cadence_window_covers_every_merge_between_base_and_head(registry, world):
    devin, gh = world
    gh.branch_heads["main"] = "a" * 40
    gh.close_completed(7)
    shas = {i: f"{i:040x}" for i in range(1, 11)}
    for i in range(1, 11):
        ev = merge_event(gh, 500 + i, branch="main", sha=shas[i])
        r = run_reduce_n(devin, gh, registry, ev, branch="main", n=5)
        if i == 5:
            first = r
    second = r
    # BASE..HEAD of each verification is exactly the first-parent chain of its window
    for report, lo in ((first, 1), (second, 6)):
        chain, sha = [], report.merge_commit_sha
        while sha != report.base_sha:
            chain.append(sha)
            sha = gh.get_commit_parents(REPO, sha)[0]
        assert chain[::-1] == [shas[i] for i in range(lo, lo + 5)]
        assert report.window_prs == [500 + i for i in range(lo, lo + 5)]
    assert second.base_sha == first.merge_commit_sha


def test_reduce_replay_does_not_recount_and_other_branches_do_not_count(registry, world):
    devin, gh = world
    gh.branch_heads["main"] = "a" * 40
    ev = merge_event(gh, 201, branch="main", sha="1" * 40)
    first = run_reduce_n(devin, gh, registry, ev, branch="main", n=5)
    assert first.merge_index == 1 and first.session_id is None
    replay = run_reduce_n(devin, gh, registry, ev, branch="main", n=5)
    assert "already counted" in replay.skipped_reason and replay.merge_index is None
    assert len(find(IssueLedger(gh, REPO).read(201), "merge_counted")) == 1

    for i in range(2, 6):
        side = merge_event(gh, 200 + i, branch="release-4.0", sha=str(i) * 40)
        r = run_reduce_n(devin, gh, registry, side, branch="main", n=5)
        assert "only 'main'" in r.skipped_reason and r.merge_index is None
    assert gh.list_merged_pulls(REPO, "main") and len(gh.list_merged_pulls(REPO, "main")) == 1
    assert devin.sessions == {}

    # the fixture PR is merged into master; with VERIFY_BRANCH=main it is ignored entirely
    r = run_reduce_n(devin, gh, registry, load_event(), branch="main", n=1)
    assert r.skipped_reason and r.session_id is None


def test_verify_settings_from_environment(monkeypatch):
    monkeypatch.delenv("VERIFY_BRANCH", raising=False)
    monkeypatch.delenv("VERIFY_EVERY_N_MERGES", raising=False)
    s = load_settings(simulate=True)
    assert s.verify_branch == "master" and s.verify_every_n_merges == 5
    monkeypatch.setenv("VERIFY_BRANCH", "main")
    monkeypatch.setenv("VERIFY_EVERY_N_MERGES", "3")
    s = load_settings(simulate=True)
    assert s.verify_branch == "main" and s.verify_every_n_merges == 3
    monkeypatch.setenv("VERIFY_EVERY_N_MERGES", "0")
    with pytest.raises(SystemExit):
        load_settings(simulate=True)


def test_report_settings_from_environment(monkeypatch):
    monkeypatch.delenv("REPORT_DIGEST_ISSUE", raising=False)
    s = load_settings(simulate=True)
    assert s.report_digest_issue is None and s.report_digest_every_hours == 24
    monkeypatch.setenv("REPORT_DIGEST_ISSUE", "42")
    monkeypatch.setenv("REPORT_DIGEST_EVERY_HOURS", "6")
    s = load_settings(simulate=True)
    assert s.report_digest_issue == 42 and s.report_digest_every_hours == 6
    monkeypatch.setenv("REPORT_DIGEST_EVERY_HOURS", "0")
    with pytest.raises(SystemExit):
        load_settings(simulate=True)


def test_reduce_automation_prompt_carries_cadence():
    payload = automations.reduce_payload(REPO, AUTO, "main", 5)
    prompt = payload["actions"][0]["prompt"]
    assert "VERIFY_BRANCH=main VERIFY_EVERY_N_MERGES=5" in prompt
    assert payload["metadata"]["verify_branch"] == "main"
    assert automations.validate_payload(payload) == []
    # the trigger itself is unchanged: it still fires on every merged PR, the counter is in code
    assert payload["triggers"] == automations.reduce_payload(REPO, AUTO)["triggers"]


# --- playbooks --------------------------------------------------------------------------------


def test_playbook_payloads_validate_against_vendored_schema():
    titles = [p["title"] for p in playbooks.payloads()]
    assert titles == [playbooks.FIX_TITLE, playbooks.VERIFY_TITLE]
    for payload in playbooks.payloads():
        assert automations.validate_payload(payload, "PlaybookCreateRequest") == []
        assert len(json.dumps(payload["structured_output_schema"])) < 64 * 1024
        assert "$ref" not in json.dumps(payload["structured_output_schema"])
        assert "max_acu" not in payload["body"] and "timeout" not in payload["body"].lower()
    assert automations.validate_payload({"title": "x"}, "PlaybookCreateRequest") != []


def test_playbook_bodies_keep_the_invariants():
    fix, verify = playbooks.fix_playbook()["body"], playbooks.verify_playbook()["body"]
    for needle in (
        "requirements/development.txt",
        "non-zero",
        "exit 0",
        "AGENTS.md",
        "Closes #",
        "Co-Auth" + "ored-By",
    ):
        assert needle in fix, needle
    for needle in (
        "verify/run_all.sh",
        "MUST FAIL at BASE",
        "result.json",
        "verbatim",
        "probes/",
        "error_message",
    ):
        assert needle in verify, needle


def test_register_playbooks_is_idempotent_by_title():
    devin = FakeDevin()
    dry = playbooks.register(devin, dry_run=True)
    assert all(r["dry_run"] for r in dry) and devin.list_playbooks() == []
    first = playbooks.register(devin)
    second = playbooks.register(devin)
    assert len(devin.list_playbooks()) == 2
    assert [r["playbook_id"] for r in first] == [r["playbook_id"] for r in second]
    ids = playbooks.lookup_ids(devin)
    assert ids["PLAYBOOK_ID_FIX"] == first[0]["playbook_id"]
    assert ids["PLAYBOOK_ID_VERIFY"] == first[1]["playbook_id"]


def test_prompts_carry_playbook_token_or_fall_back_inline(registry):
    spec = registry.by_number(5)
    issue = {"number": 5, "html_url": f"https://github.com/{REPO}/issues/5"}
    with_pb = prompts.fix_session_prompt(REPO, AUTO, issue, spec, "pb-fix")
    inline = prompts.fix_session_prompt(REPO, AUTO, issue, spec)
    assert with_pb.startswith(f"@{REPO} @playbook:pb-fix\n")
    assert "requirements/development.txt" not in with_pb and "issue_5/unit" in with_pb
    assert "@playbook:" not in inline and prompts.FIX_PLAYBOOK_BODY in inline

    probes = registry.probes_for([5])
    with_pb = prompts.verification_prompt(
        REPO, AUTO, "h" * 40, "b" * 40, "https://x/pr/1", [5], probes, "pb-v"
    )
    inline = prompts.verification_prompt(REPO, AUTO, "h" * 40, "b" * 40, "https://x/pr/1", [5], probes)
    assert "@playbook:pb-v" in with_pb and "--head " + "h" * 40 in with_pb and "npm ci" not in with_pb
    assert "@playbook:" not in inline and prompts.VERIFY_PLAYBOOK_BODY in inline


def test_map_and_reduce_run_with_and_without_playbook_ids(registry, world, caplog):
    devin, gh = world
    with caplog.at_level("WARNING"):
        report = do_map(devin, gh, registry)
    assert report.started and "PLAYBOOK_ID_FIX unset" in caplog.text
    assert all("@playbook:" not in s["prompt"] for s in devin.sessions.values())

    devin2, gh2 = FakeDevin(), FakeGitHub.from_fixtures()
    run_map(
        devin=devin2,
        gh=gh2,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        ready_label="ready",
        playbook_id="pb-fix",
    )
    assert devin2.sessions and all("@playbook:pb-fix" in s["prompt"] for s in devin2.sessions.values())
    assert all(s["structured_output_schema"] is not None for s in devin2.sessions.values())

    caplog.clear()
    with caplog.at_level("WARNING"):
        r = do_reduce(devin2, gh2, registry)
    assert r.session_id and "PLAYBOOK_ID_VERIFY unset" in caplog.text
    r2 = run_reduce(
        devin=FakeDevin(),
        gh=FakeGitHub.from_fixtures(),
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        event=load_event(),
        every_n=1,
        playbook_id="pb-v",
    )
    assert r2.session_id is not None


def test_playbook_settings_from_environment(monkeypatch):
    monkeypatch.delenv("PLAYBOOK_ID_FIX", raising=False)
    monkeypatch.setenv("PLAYBOOK_ID_VERIFY", "")
    s = load_settings(simulate=True)
    assert s.playbook_id_fix is None and s.playbook_id_verify is None
    monkeypatch.setenv("PLAYBOOK_ID_FIX", "pb-1")
    monkeypatch.setenv("PLAYBOOK_ID_VERIFY", "pb-2")
    s = load_settings(simulate=True)
    assert (s.playbook_id_fix, s.playbook_id_verify) == ("pb-1", "pb-2")


def test_automation_shims_pass_playbook_ids_through():
    m = automations.map_payload(REPO, AUTO, "pb-1")
    r = automations.reduce_payload(REPO, AUTO, "main", 5, "pb-2")
    assert "PLAYBOOK_ID_FIX=pb-1 python -m orchestrator map" in m["actions"][0]["prompt"]
    assert "PLAYBOOK_ID_VERIFY=pb-2 python -m orchestrator reduce" in r["actions"][0]["prompt"]
    assert automations.validate_payload(m) == [] and automations.validate_payload(r) == []
    assert "PLAYBOOK_ID" not in automations.map_payload(REPO, AUTO)["actions"][0]["prompt"]


def test_reduce_adds_regression_guards_for_landed_fixes(registry, world):
    devin, gh = world
    gh.close_completed(7)
    report = do_reduce(devin, gh, registry)
    assert report.regression_issues == [7]
    prompt = devin.sessions[report.session_id]["prompt"]
    assert '--regression "7"' in prompt and "issue_7/unit" in prompt and "issue_5/unit" in prompt


def test_verification_prompt_demands_base_failure_and_forbids_probe_edits(registry, world):
    devin, gh = world
    report = do_reduce(devin, gh, registry)
    prompt = devin.sessions[report.session_id]["prompt"]
    for needle in (
        "BASE",
        "must fail",
        "exit code",
        "verify/run_all.sh",
        "Postgres",
        "npm ci",
        "Do not modify",
    ):
        assert needle.lower() in prompt.lower(), needle
    assert "max_acu" not in prompt and "timeout" not in prompt.lower()


# --- ledger -----------------------------------------------------------------------------------


def test_ledger_is_append_only_and_roundtrips():
    gh = FakeGitHub()
    gh.issues[1] = {"number": 1, "state": "open", "labels": [], "body": "x", "title": "t"}
    ledger = IssueLedger(gh, REPO)
    ledger.append(1, "A", LedgerEntry("e1", data={"k": 1}), ["l1"])
    ledger.append(1, "B", LedgerEntry("e2", data={"k": 2}), ["l2"])
    comments = gh.list_issue_comments(REPO, 1)
    assert len(comments) == 2 and gh.issues[1]["body"] == "x"
    entries = parse_entries(comments)
    assert [e.event for e in entries] == ["e1", "e2"]
    assert find(entries, "e2", k=2) and not find(entries, "e2", k=3)
    assert parse_entries([{"body": "human chatter <!-- sda:not-json -->"}]) == []


def test_closing_keywords():
    text = (
        "Fixes #5, resolves https://github.com/jhomer192/superset/issues/7 "
        "and mentions #9. Closes jhomer192/superset#11"
    )
    assert closing_issue_numbers(text, REPO) == [5, 7, 11]
    assert closing_issue_numbers("closes https://github.com/other/repo/issues/3", REPO) == []


# --- metrics ----------------------------------------------------------------------------------


def test_metrics_math():
    now = datetime(2026, 1, 31, tzinfo=UTC)
    sessions: list[dict[str, Any]] = [
        {
            "session_id": "a",
            "tags": ["sda-fix"],
            "status": "exit",
            "pull_requests": [{"pr_url": "u1", "pr_state": "merged"}],
        },
        {
            "session_id": "b",
            "tags": ["sda-fix"],
            "status": "exit",
            "pull_requests": [{"pr_url": "u2", "pr_state": "open"}],
        },
        {"session_id": "c", "tags": ["sda-fix"], "status": "suspended", "status_detail": "waiting_for_user"},
        {
            "session_id": "d",
            "tags": ["sda-verify"],
            "status": "exit",
            "structured_output": {"status": "ok", "acceptance_met": True},
        },
        {
            "session_id": "e",
            "tags": ["sda-verify"],
            "status": "exit",
            "structured_output": {"status": "ok", "acceptance_met": False},
        },
        {
            "session_id": "f",
            "tags": ["sda-verify"],
            "status": "error",
            "structured_output": {"status": "error"},
        },
    ]
    consumption = {"a": 4.0, "b": 2.0, "c": 1.0, "d": 3.0, "e": 1.5, "f": 0.5}
    r = metrics.compute(
        sessions, {"sessions_created_count": 6}, consumption, deflections=3, window=(now, now)
    )
    assert r.fix_sessions == 3 and r.verify_sessions == 3
    assert r.sessions_with_merged_pr == 1 and r.merge_rate == pytest.approx(1 / 3, abs=1e-4)
    assert r.total_acus == 12.0 and r.acu_per_session == 2.0 and r.acu_per_merged_pr == 12.0
    assert r.verification_runs == 2 and r.verification_passes == 1 and r.verification_pass_rate == 0.5
    assert r.triage_deflections == 3
    assert r.liveness["awaiting_human"] == 1 and r.liveness["dead"] == 5
    assert r.org_metrics["sessions_created_count"] == 6


def test_metrics_with_no_sessions_has_no_division_by_zero():
    now = datetime.now(UTC)
    r = metrics.compute([], {}, {}, deflections=0, window=(now, now))
    assert r.merge_rate is None and r.acu_per_merged_pr is None and r.total_acus == 0


# --- automations ------------------------------------------------------------------------------


def test_automation_payloads_validate_against_openapi_and_carry_no_ceilings():
    for payload in (
        automations.map_payload(REPO, AUTO),
        automations.reduce_payload(REPO, AUTO),
        automations.report_payload(REPO, AUTO, 42),
    ):
        assert automations.validate_payload(payload) == []
        automations.assert_no_ceilings(payload)
        assert payload["run_as"] == {"type": "organization"}
        assert sum(a["type"] == "start_session" for a in payload["actions"]) == 1
        hosts = {a["hostname"] for a in payload["session_settings"]["net_policy"]["allow"]}
        assert {"git-manager.devin.ai", "api.github.com", "api.devin.ai", "pypi.org"} <= hosts
        assert f"@{AUTO}" in payload["actions"][0]["prompt"]
        assert "python -m orchestrator" in payload["actions"][0]["prompt"]


def test_map_trigger_is_friday_rrule_and_reduce_filters_merged_prs():
    m = automations.map_payload(REPO, AUTO)["triggers"][0]
    assert m["event_type"] == "schedule:recurring"
    assert m["conditions"]["any"][0]["all"] == [
        {"field": "rrule", "operator": "recurrence", "value": "FREQ=WEEKLY;BYDAY=FR"}
    ]
    r = automations.reduce_payload(REPO, AUTO)["triggers"][0]
    assert r["event_type"] == "github:pull_request"
    conds = {c["field"]: c["value"] for c in r["conditions"]["any"][0]["all"]}
    assert conds == {"action": "closed", "pull_request.merged": True, "repository.full_name": REPO}


def test_invalid_payloads_are_rejected():
    bad = automations.map_payload(REPO, AUTO)
    bad["limits"] = {"max_acu_limit": 10}
    with pytest.raises(ValueError, match="max_acu_limit"):
        automations.assert_no_ceilings(bad)
    bad = automations.map_payload(REPO, AUTO)
    bad["actions"].append({"type": "start_session", "prompt": "second"})
    with pytest.raises(ValueError, match="at most one"):
        automations.assert_no_ceilings(bad)
    bad = automations.map_payload(REPO, AUTO)
    bad["triggers"][0]["event_type"] = "cron"
    assert automations.validate_payload(bad)


def test_register_is_idempotent_by_name():
    devin = FakeDevin()
    automations.register(devin, REPO, AUTO)
    automations.register(devin, REPO, AUTO)
    assert len(devin.automations) == 3


def test_report_automation_is_hourly_and_carries_the_digest_issue():
    payload = automations.report_payload(REPO, AUTO, 42, digest_every_hours=6)
    trigger = payload["triggers"][0]
    assert trigger["event_type"] == "schedule:recurring"
    assert trigger["conditions"]["any"][0]["all"] == [
        {"field": "rrule", "operator": "recurrence", "value": "FREQ=HOURLY"}
    ]
    prompt = payload["actions"][0]["prompt"]
    assert "REPORT_DIGEST_ISSUE=42" in prompt and "REPORT_DIGEST_EVERY_HOURS=6" in prompt
    assert "REPORT_DIGEST_ISSUE" not in automations.report_payload(REPO, AUTO)["actions"][0]["prompt"]


# --- whole loop --------------------------------------------------------------------------------


def test_simulate_runs_end_to_end(registry):
    out = cmd_simulate(load_settings(simulate=True), registry)
    assert out["friday_1_map_rerun_is_idempotent"]["started"] == []
    assert out["merge_reduce_replay_is_deduplicated"]["skipped_reason"]
    assert out["verification_structured_output"]["acceptance_met"] is True
    assert out["metrics"]["triage_deflections"] == 2
    assert out["metrics"]["sessions_with_merged_pr"] == 1


def test_simulate_every_fifth_merge(registry, monkeypatch):
    monkeypatch.setenv("VERIFY_BRANCH", "master")
    monkeypatch.setenv("VERIFY_EVERY_N_MERGES", "5")
    out = cmd_simulate(load_settings(simulate=True), registry)
    counted = out["merges_counted_not_verified"]
    assert [c["merge_index"] for c in counted] == [1, 2, 3, 4]
    assert all("of 5" in c["reason"] for c in counted)
    assert out["merge_reduce"]["merge_index"] == 5 and out["merge_reduce"]["session_id"]
    assert out["merge_reduce"]["base_sha"] == "fc110d8428f35249a2092778ca0a3e26a2de0b14"
    assert out["merge_reduce"]["closes"] == [5]
    assert out["metrics"]["sessions_with_merged_pr"] == 1


def test_count_deflections(registry, world):
    devin, gh = world
    do_map(devin, gh, registry)
    assert count_deflections(gh, registry, REPO) == 2


# --- report ------------------------------------------------------------------------------------


def do_report(devin, gh, *, digest_issue=None, now=None, digest_every_hours=24):
    return run_report(
        devin=devin,
        gh=gh,
        target_repo=REPO,
        automation_repo=AUTO,
        deflections=lambda: 0,
        digest_issue=digest_issue,
        digest_every_hours=digest_every_hours,
        now=now,
    )


def test_report_publishes_each_finished_session_once(registry, world):
    devin, gh = world
    started = do_map(devin, gh, registry).started
    by_issue = {s["issue"]: s["session_id"] for s in started}
    devin.advance(by_issue[5], outcome="ok", acus=3.0, pr_url="https://github.com/jhomer192/superset/pull/14")
    devin.advance(by_issue[10], outcome="error", acus=0.5)
    devin.suspend(by_issue[11], "waiting_for_user")

    report = do_report(devin, gh)
    assert {(p["thread"], p["kind"]) for p in report.posted} == {(5, "fix"), (10, "fix")}
    assert report.unfinished == 5 and report.already_reported == 0

    bodies = {n: "\n".join(c["body"] for c in cs) for n, cs in gh.comments.items()}
    assert "acceptance met" in bodies[5] and "ACUs: 3" in bodies[5]
    assert "error: simulated failure before any probe ran" in bodies[10]
    assert 11 not in {p["thread"] for p in report.posted}

    assert do_report(devin, gh).posted == []


def test_report_posts_verification_verdict_on_the_pr(registry, world):
    devin, gh = world
    session_id = do_reduce(devin, gh, registry).session_id
    devin.advance(session_id, outcome="ok", acus=6.0)
    posted = do_report(devin, gh).posted
    assert posted == [{"thread": 14, "kind": "verify", "session_id": session_id}]
    body = gh.comments[14][-1]["body"]
    assert "Verification session finished: acceptance met" in body
    assert "base exit 1, head exit 0" in body


def test_report_flags_a_session_that_finished_without_structured_output(world):
    devin, gh = world
    session = devin.create_session({"prompt": "p", "tags": ["sda-fix", "issue-5"]})
    devin.sessions[session["session_id"]]["status"] = "exit"
    assert do_report(devin, gh).posted
    assert "no structured output" in gh.comments[5][-1]["body"]


def test_digest_is_appended_at_most_once_per_interval(world):
    devin, gh = world
    now = datetime(2026, 1, 31, 12, tzinfo=UTC)
    assert do_report(devin, gh, digest_issue=1, now=now).digest_posted
    assert not do_report(devin, gh, digest_issue=1, now=now + timedelta(hours=23)).digest_posted
    assert do_report(devin, gh, digest_issue=1, now=now + timedelta(hours=25)).digest_posted
    digests = [c for c in gh.comments[1] if "metrics digest" in c["body"]]
    assert len(digests) == 2 and "| verification passed |" in digests[0]["body"]


# --- self-healing regressions -------------------------------------------------------------------


def fail_verification(devin, gh, registry):
    """A merged PR whose verification finds a probe failing at HEAD."""
    session_id = do_reduce(devin, gh, registry).session_id
    devin.advance(session_id, outcome="failed", acus=5.0)
    return session_id


def test_failed_verification_files_an_issue_and_starts_its_fix(registry, world):
    devin, gh = world
    session_id = fail_verification(devin, gh, registry)

    filed = do_report(devin, gh).regressions_filed
    assert len(filed) == 1
    entry = filed[0]
    assert entry["pr"] == 14 and entry["probes"] == ["issue_5/unit"] and entry["depth"] == 1

    issue = gh.issues[entry["issue"]]
    assert {label["name"] for label in issue["labels"]} == {"regression", "automation"}
    assert "pull/14" in issue["body"] and "simulated pytest summary" in issue["body"]
    assert session_id in issue["body"]

    fix = devin.sessions[entry["session_id"]]
    assert set(fix["tags"]) == {"sda-fix", "sda-regression", f"issue-{entry['issue']}"}
    assert fix["structured_output_schema"]["title"] == "SupersetFixResult"
    assert f"#{entry['issue']}" in fix["prompt"] and "issue_5/unit" in fix["prompt"]
    assert "Closes #" in fix["prompt"]


def test_a_failure_is_only_filed_once(registry, world):
    devin, gh = world
    fail_verification(devin, gh, registry)
    first = do_report(devin, gh)
    assert do_report(devin, gh).regressions_filed == []
    assert len(gh.issues) == len({i["number"] for i in gh.issues.values()})
    assert find(IssueLedger(gh, REPO).read(14), "regression_filed", issue=first.regressions_filed[0]["issue"])


def fail_verification_at_every_n_5(devin, gh, registry):
    """Five merges into main; the fifth triggers a verification that fails at HEAD."""
    gh.branch_heads["main"] = "a" * 40
    gh.close_completed(5)
    for i in range(1, 6):
        ev = merge_event(gh, 200 + i, branch="main", sha=f"{i:040x}")
        r = run_reduce_n(devin, gh, registry, ev, branch="main", n=5)
    assert r.merge_index == 5 and r.window_prs == [201, 202, 203, 204, 205] and r.session_id
    devin.advance(r.session_id, outcome="failed", acus=5.0)
    return r


def test_one_failed_verification_at_every_n_5_files_one_issue_and_one_fix(registry, world):
    devin, gh = world
    reduce_report = fail_verification_at_every_n_5(devin, gh, registry)
    issues_before = set(gh.issues)

    report = do_report(devin, gh)
    # the verdict still lands on every PR in the window
    assert [p["thread"] for p in report.posted] == [201, 202, 203, 204, 205]
    # but the failure is remediated exactly once
    assert len(report.regressions_filed) == 1
    filed = report.regressions_filed[0]
    assert filed["pr"] == 205 and filed["window"] == [201, 202, 203, 204, 205]
    new_issues = set(gh.issues) - issues_before
    assert new_issues == {filed["issue"]}
    fixes = [s for s in devin.sessions.values() if "sda-regression" in s["tags"]]
    assert len(fixes) == 1 and fixes[0]["session_id"] == filed["session_id"]

    issue = gh.issues[filed["issue"]]
    assert "PR #205" in issue["title"]
    for n in range(201, 206):
        assert f"pull/{n}" in issue["body"]
    assert reduce_report.base_sha in issue["body"] and reduce_report.merge_commit_sha in issue["body"]

    # and a rerun finds the marker, whatever thread it reads
    assert do_report(devin, gh).regressions_filed == []
    ledger = IssueLedger(gh, REPO)
    assert find(ledger.read(205), "regression_filed", session_id=reduce_report.session_id)
    assert len([s for s in devin.sessions.values() if "sda-regression" in s["tags"]]) == 1
    assert set(gh.issues) - issues_before == new_issues


def test_a_regression_chain_stops_after_two_automated_attempts(registry, world):
    devin, gh = world
    fail_verification(devin, gh, registry)
    issue = do_report(devin, gh).regressions_filed[0]["issue"]

    # The fix for that issue lands and regresses again, twice.
    depths = []
    for pr_number in (101, 102):
        pr = gh.merge(
            pr_number,
            branch="master",
            sha=f"{pr_number:040x}",
            title=f"fix: attempt {pr_number}",
            body=f"Closes #{issue}",
        )
        devin.advance(
            do_reduce(devin, gh, registry, event={**load_event(), "pull_request": pr}).session_id,
            outcome="failed",
            acus=1.0,
        )
        report = do_report(devin, gh)
        depths.append(report)
        issue = report.regressions_filed[0]["issue"] if report.regressions_filed else issue

    assert depths[0].regressions_filed[0]["depth"] == 2 and depths[0].escalated == []
    assert depths[1].regressions_filed == []
    assert [(e["pr"], e["depth"]) for e in depths[1].escalated] == [(102, 3)]
    assert "a human needs to look at it" in gh.comments[102][-1]["body"]


def test_an_errored_verification_is_reported_but_not_filed_as_a_regression(registry, world):
    devin, gh = world
    devin.advance(do_reduce(devin, gh, registry).session_id, outcome="error", acus=0.2)
    report = do_report(devin, gh)
    assert report.posted and report.regressions_filed == []


def test_metrics_track_regression_issues_and_their_prs(registry, world):
    devin, gh = world
    fail_verification(devin, gh, registry)
    filed = do_report(devin, gh).regressions_filed[0]
    devin.advance(filed["session_id"], outcome="ok", acus=2.0, pr_url="https://github.com/x/y/pull/7")

    summary = metrics.collect(devin, deflections=0, days=30, acu_usd=2.5)
    assert summary.regression_issues == 1 and summary.regression_fix_prs == 1
    assert summary.regressions[0]["issue"] == filed["issue"]
    assert summary.regressions[0]["pr_url"] == "https://github.com/x/y/pull/7"
    assert summary.estimated_cost_usd == round(summary.total_acus * 2.5, 2)
    assert summary.org_total_acus is not None and summary.pr_metrics["prs_merged_count"] == 1


def test_cost_is_omitted_when_no_rate_is_configured(world):
    devin, _ = world
    summary = metrics.collect(devin, deflections=0, days=30)
    assert summary.estimated_cost_usd is None
    assert any("ACUs, not money" in limitation for limitation in summary.limitations)


# --- verify/collect ---------------------------------------------------------------------------


def test_collect_requires_base_failure_for_closed_issues_only(tmp_path):
    from verify.collect import build_results

    log = tmp_path / "l.log"
    log.write_text("x")
    row = {"probe": "p", "kind": "offline_pytest", "log": str(log)}
    rows = [
        {**row, "issue": 5, "role": "head", "exit_code": 0},
        {**row, "issue": 5, "role": "base", "exit_code": 0},
    ]
    assert build_results(rows, closed={5}, regression=set())[0]["acceptance_met"] is False
    assert build_results(rows, closed=set(), regression={5})[0]["acceptance_met"] is True
    rows[1]["exit_code"] = 1
    assert build_results(rows, closed={5}, regression=set())[0]["acceptance_met"] is True
    rows[0]["exit_code"] = 2
    assert build_results(rows, closed={5}, regression=set())[0]["acceptance_met"] is False
    assert build_results(rows[:1], closed={5}, regression=set())[0]["base_exit_code"] is None


# --- API cost of a run ---------------------------------------------------------------------------


class CountingDevin:
    """A FakeDevin that tallies its calls, so what a run costs in requests is measurable."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: Counter[str] = Counter()

    def create_session(self, body):
        self.calls["create_session"] += 1
        return self.inner.create_session(body)

    def get_session(self, session_id):
        self.calls["get_session"] += 1
        return self.inner.get_session(session_id)

    def list_sessions(self, **params):
        self.calls["list_sessions"] += 1
        return self.inner.list_sessions(**params)

    def session_metrics(self, time_after, time_before):
        self.calls["session_metrics"] += 1
        return self.inner.session_metrics(time_after, time_before)

    def pr_metrics(self, time_after, time_before):
        self.calls["pr_metrics"] += 1
        return self.inner.pr_metrics(time_after, time_before)

    def session_consumption(self, session_id):
        self.calls["session_consumption"] += 1
        return self.inner.session_consumption(session_id)

    def org_consumption(self, time_after, time_before):
        self.calls["org_consumption"] += 1
        return self.inner.org_consumption(time_after, time_before)


class CountingGitHub:
    def __init__(self, inner):
        self.inner = inner
        self.calls: Counter[str] = Counter()

    def list_issues(self, repo, labels, state="open"):
        self.calls["list_issues"] += 1
        return self.inner.list_issues(repo, labels, state)

    def get_issue(self, repo, number):
        self.calls["get_issue"] += 1
        return self.inner.get_issue(repo, number)

    def create_issue(self, repo, title, body, labels):
        self.calls["create_issue"] += 1
        return self.inner.create_issue(repo, title, body, labels)

    def list_issue_comments(self, repo, number):
        self.calls["list_issue_comments"] += 1
        return self.inner.list_issue_comments(repo, number)

    def create_issue_comment(self, repo, number, body):
        self.calls["create_issue_comment"] += 1
        return self.inner.create_issue_comment(repo, number, body)

    def list_pulls(self, repo, state="open"):
        self.calls["list_pulls"] += 1
        return self.inner.list_pulls(repo, state)

    def get_pull(self, repo, number):
        self.calls["get_pull"] += 1
        return self.inner.get_pull(repo, number)

    def list_merged_pulls(self, repo, base_branch):
        self.calls["list_merged_pulls"] += 1
        return self.inner.list_merged_pulls(repo, base_branch)

    def get_commit_parents(self, repo, sha):
        self.calls["get_commit_parents"] += 1
        return self.inner.get_commit_parents(repo, sha)


def finished_report_session(devin, *, acus):
    """What REPORT's own hourly session looks like once it has run."""
    session = devin.create_session({"prompt": "p", "tags": ["sda-report"]})
    devin.sessions[session["session_id"]].update({"status": "exit", "acus_consumed": acus})
    return session


def finished_world(registry, world):
    """The fixture world after a MAP sweep whose sessions have all finished."""
    devin, gh = world
    started = do_map(devin, gh, registry).started
    for i, session in enumerate(started):
        devin.advance(
            session["session_id"],
            outcome="ok",
            acus=float(i + 1),
            pr_url=f"https://github.com/jhomer192/superset/pull/{200 + i}",
        )
    return devin, gh


def test_a_steady_state_report_run_costs_nothing_in_consumption(registry, world):
    devin, gh = world
    devin, gh = finished_world(registry, (devin, gh))
    now = datetime(2026, 1, 31, 12, tzinfo=UTC)

    first_devin, first_gh = CountingDevin(devin), CountingGitHub(gh)
    assert do_report(first_devin, first_gh, digest_issue=1, now=now).posted
    # One consumption lookup per session reported, reused by the digest rather than repeated.
    sessions = len(devin.sessions)
    assert first_devin.calls["session_consumption"] == sessions
    assert first_devin.calls["list_sessions"] == 2  # the loop's sessions, then REPORT's own

    steady_devin, steady_gh = CountingDevin(devin), CountingGitHub(gh)
    steady = do_report(steady_devin, steady_gh, digest_issue=1, now=now + timedelta(hours=1))
    assert steady.posted == [] and not steady.digest_posted
    assert steady_devin.calls["session_consumption"] == 0
    assert steady_gh.calls["create_issue_comment"] == 0
    # Each thread is listed once per run, not once per check.
    assert steady_gh.calls["list_issue_comments"] == sessions


def test_report_reads_more_than_one_page_of_sessions(registry, world):
    devin, gh = world
    for _ in range(150):
        session = devin.create_session(
            {"prompt": "p", "tags": ["sda-fix", "issue-5"], "structured_output_schema": FIX_SCHEMA}
        )
        devin.advance(session["session_id"], outcome="ok", acus=1.0)
    noise = finished_report_session(devin, acus=0.4)

    report = do_report(devin, gh, digest_issue=1, now=datetime(2026, 1, 31, 12, tzinfo=UTC))
    assert len(report.posted) == 150
    assert noise["session_id"] not in "\n".join(c["body"] for c in gh.comments[5])


def test_report_prices_its_own_polling_separately(registry, world):
    devin, gh = world
    devin, gh = finished_world(registry, (devin, gh))
    finished_report_session(devin, acus=0.4)

    summary = metrics.collect(devin, deflections=0, days=30)
    assert summary.polling_acus == 0.4
    loop = devin.list_sessions(tags=[metrics.FIX_TAG, metrics.VERIFY_TAG])
    assert all(metrics.REPORT_TAG not in s["tags"] for s in loop)
    body = "\n".join(digest_lines(summary))
    assert "| ACUs polling (REPORT itself) | 0.4 |" in body
    assert "{" not in body and "None" not in body


def test_digest_prints_n_a_rather_than_none(world):
    devin, _ = world
    body = "\n".join(digest_lines(metrics.collect(devin, deflections=0, days=30)))
    assert "| merge rate n/a" in body or "(merge rate n/a)" in body
    assert "| ACUs per session | n/a |" in body and "None" not in body


def test_live_client_follows_the_session_cursor(monkeypatch):
    pages = [
        {
            "items": [{"session_id": f"devin-{i}"} for i in range(devin_api.MAX_PAGE)],
            "has_next_page": True,
            "end_cursor": "cursor-2",
        },
        {"items": [{"session_id": "devin-tail"}], "has_next_page": False, "end_cursor": None},
    ]
    seen = []

    def fake_request(method, url, *, headers=None, params=None, body=None):
        seen.append(dict(params or {}))
        return pages[len(seen) - 1]

    monkeypatch.setattr(devin_api, "request_json", fake_request)
    client = devin_api.LiveDevinClient("https://api.devin.ai", "key", "org-1")
    sessions = client.list_sessions(origins="automation", tags=["sda-fix", "sda-verify"])

    assert len(sessions) == devin_api.MAX_PAGE + 1
    assert seen[0]["first"] == 200 and seen[0]["tags"] == ["sda-fix", "sda-verify"]
    assert "after" not in seen[0] and seen[1]["after"] == "cursor-2"
