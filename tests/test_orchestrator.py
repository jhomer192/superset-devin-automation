import copy
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from orchestrator import automations, metrics
from orchestrator.__main__ import cmd_simulate, count_deflections
from orchestrator.config import load_settings
from orchestrator.github_api import closing_issue_numbers
from orchestrator.ledger import IssueLedger, LedgerEntry, find, parse_entries
from orchestrator.map_job import run_map
from orchestrator.reduce_job import NotAMergedPR, extract_merged_pr, run_reduce
from orchestrator.registry import load_registry
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
    return run_reduce(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        event=event or load_event(),
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
    for payload in (automations.map_payload(REPO, AUTO), automations.reduce_payload(REPO, AUTO)):
        assert automations.validate_payload(payload) == []
        automations.assert_no_ceilings(payload)
        assert payload["run_as"] == {"type": "organization"}
        assert sum(a["type"] == "start_session" for a in payload["actions"]) == 1
        assert payload["session_settings"]["net_policy"] == {"allow": [{"hostname": "git-manager.devin.ai"}]}
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
    assert len(devin.automations) == 2


# --- whole loop --------------------------------------------------------------------------------


def test_simulate_runs_end_to_end(registry):
    out = cmd_simulate(load_settings(simulate=True), registry)
    assert out["friday_1_map_rerun_is_idempotent"]["started"] == []
    assert out["merge_reduce_replay_is_deduplicated"]["skipped_reason"]
    assert out["verification_structured_output"]["acceptance_met"] is True
    assert out["metrics"]["triage_deflections"] == 2
    assert out["metrics"]["sessions_with_merged_pr"] == 1


def test_count_deflections(registry, world):
    devin, gh = world
    do_map(devin, gh, registry)
    assert count_deflections(gh, registry, REPO) == 2


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
