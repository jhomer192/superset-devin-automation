import copy
import json

import pytest

from orchestrator import automations, playbooks, prompts
from orchestrator.__main__ import cmd_simulate
from orchestrator.autopr_job import NotARegressionIssue, extract_regression_issue, run_autopr
from orchestrator.config import load_settings
from orchestrator.find_and_fix import run_find_and_fix
from orchestrator.github_api import closing_issue_numbers
from orchestrator.ledger import IssueLedger, find
from orchestrator.publish import publish_verification
from orchestrator.registry import load_registry
from orchestrator.sessions import Liveness, classify, holds_slot, is_finished, wait_until_finished
from orchestrator.simulate import FakeDevin, FakeGitHub, issue_event, load_event
from orchestrator.testing_job import NotAMergedPR, extract_merged_pr, run_testing
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
    return run_autopr(
        devin=devin, gh=gh, registry=registry, target_repo=REPO, automation_repo=AUTO, ready_label="ready"
    )


def do_reduce(devin, gh, registry, event=None):
    """REDUCE at every_n=1 against the single-merge fixture, so one event is one verification."""
    return run_testing(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        event=event or load_event(),
        every_n=1,
    )


def run_testing_n(devin, gh, registry, event, *, branch, n):
    return run_testing(
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
    assert first.session_id and first.requirements == registry.requirement_ids()
    replay = do_reduce(devin, gh, registry)
    assert replay.skipped_reason and replay.session_id == first.session_id
    assert len([s for s in devin.sessions.values() if "sda-verify" in s["tags"]]) == 1

    devin.advance(first.session_id, outcome="ok", acus=5)
    done = do_reduce(devin, gh, registry)
    assert "already completed" in done.skipped_reason

    other = copy.deepcopy(load_event())
    other["pull_request"]["merge_commit_sha"] = "3" * 40
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
        reports.append(run_testing_n(devin, gh, registry, ev, branch="main", n=5))
    assert [r.merge_index for r in reports] == [1, 2, 3, 4, 5]
    assert all(r.session_id is None and "of 5" in r.skipped_reason for r in reports[:4])
    fifth = reports[4]
    assert fifth.session_id and fifth.window_prs == [101, 102, 103, 104, 105]
    assert fifth.merge_commit_sha == "5" * 40
    assert len([s for s in devin.sessions.values() if "sda-verify" in s["tags"]]) == 1
    # every non-triggering merge left a durable count on its own PR thread
    for n in (101, 102, 103, 104):
        assert find(IssueLedger(gh, REPO).read(n), "merge_counted")
    # the 10th merge is the next trigger and its window starts after the 5th
    for i in range(6, 11):
        ev = merge_event(gh, 100 + i, branch="main", sha=chr(ord("a") + i) * 40)
        r = run_testing_n(devin, gh, registry, ev, branch="main", n=5)
    assert r.merge_index == 10 and r.session_id
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
        reports[i] = run_testing_n(devin, gh, registry, ev, branch="main", n=5)
    assert [reports[i].merge_index for i in range(1, 16)] == list(range(1, 16))
    verified = sorted(i for i, r in reports.items() if r.session_id)
    assert verified == [5, 10, 15]
    for i in range(1, 16):
        if i % 5:
            assert reports[i].session_id is None and "of 5" in reports[i].skipped_reason
    assert len(verify_sessions(devin)) == 3
    # each window is the five merges ending at the trigger
    assert reports[5].window_prs == [301, 302, 303, 304, 305]
    assert reports[10].window_prs == [306, 307, 308, 309, 310]
    assert reports[15].window_prs == [311, 312, 313, 314, 315]
    assert reports[15].merge_commit_sha == shas[15]


def test_verification_selects_prd_probes_only_never_closed_issues(registry, world):
    """Closed issues and `Closes #n` claims do not pick probes; PRD.md does."""
    devin, gh = world
    gh.branch_heads["main"] = "a" * 40
    gh.close_completed(6)  # issue_6 has a probe but no PRD requirement
    gh.close_completed(10)
    for i in range(1, 6):
        ev = merge_event(gh, 400 + i, branch="main", sha=f"{i:040x}", body="Closes #10")
        r = run_testing_n(devin, gh, registry, ev, branch="main", n=5)
    assert r.merge_index == 5 and r.session_id
    assert r.requirements == registry.requirement_ids()
    prompt = devin.sessions[r.session_id]["prompt"]
    for _, probe in registry.requirement_probes(registry.requirement_ids()):
        assert probe.id in prompt
    assert "issue_10/" not in prompt and "issue_6/" not in prompt
    assert "--regression" not in prompt and "--issues" not in prompt and "--base" not in prompt
    assert "regression guards" not in prompt and "BASE" not in prompt
    assert f'--head {5:040x} --requirements "{",".join(registry.requirement_ids())}"' in prompt
    started = find(IssueLedger(gh, REPO).read(405), "verification_started")[0]
    assert started.data["requirements"] == registry.requirement_ids() and "base" not in started.data
    comment = [c["body"] for c in gh.comments[405] if "PRD requirements checked at HEAD" in c["body"]][-1]
    assert "regression guards" not in comment and "closes:" not in comment
    assert len(verify_sessions(devin)) == 1


def test_reduce_replay_does_not_recount_and_other_branches_do_not_count(registry, world):
    devin, gh = world
    gh.branch_heads["main"] = "a" * 40
    ev = merge_event(gh, 201, branch="main", sha="1" * 40)
    first = run_testing_n(devin, gh, registry, ev, branch="main", n=5)
    assert first.merge_index == 1 and first.session_id is None
    replay = run_testing_n(devin, gh, registry, ev, branch="main", n=5)
    assert "already counted" in replay.skipped_reason and replay.merge_index is None
    assert len(find(IssueLedger(gh, REPO).read(201), "merge_counted")) == 1

    for i in range(2, 6):
        side = merge_event(gh, 200 + i, branch="release-4.0", sha=str(i) * 40)
        r = run_testing_n(devin, gh, registry, side, branch="main", n=5)
        assert "only 'main'" in r.skipped_reason and r.merge_index is None
    assert gh.list_merged_pulls(REPO, "main") and len(gh.list_merged_pulls(REPO, "main")) == 1
    assert devin.sessions == {}

    # the fixture PR is merged into master; with VERIFY_BRANCH=main it is ignored entirely
    r = run_testing_n(devin, gh, registry, load_event(), branch="main", n=1)
    assert r.skipped_reason and r.session_id is None


def test_verify_settings_from_environment(monkeypatch):
    monkeypatch.delenv("VERIFY_BRANCH", raising=False)
    monkeypatch.delenv("VERIFY_EVERY_N_MERGES", raising=False)
    s = load_settings(simulate=True)
    assert s.verify_branch == "master" and s.verify_every_n_merges == 1
    monkeypatch.setenv("VERIFY_BRANCH", "main")
    monkeypatch.setenv("VERIFY_EVERY_N_MERGES", "3")
    s = load_settings(simulate=True)
    assert s.verify_branch == "main" and s.verify_every_n_merges == 3
    monkeypatch.setenv("VERIFY_EVERY_N_MERGES", "0")
    with pytest.raises(SystemExit):
        load_settings(simulate=True)

    assert closing_issue_numbers("closes https://github.com/other/repo/issues/3", REPO) == []


# --- automations ------------------------------------------------------------------------------


def test_automation_payloads_validate_against_openapi_and_carry_no_ceilings():
    for payload in (
        automations.finder_payload(REPO, AUTO),
        automations.finder_payload(REPO, AUTO, every_n=5),
    ):
        assert automations.validate_payload(payload) == []
        automations.assert_no_ceilings(payload)
        assert payload["run_as"] == {"type": "organization"}
        assert sum(a["type"] == "start_session" for a in payload["actions"]) == 1
        hosts = {a["hostname"] for a in payload["session_settings"]["net_policy"]["allow"]}
        assert {"git-manager.devin.ai", "api.github.com", "api.devin.ai", "pypi.org"} <= hosts
        assert f"@{AUTO}" in payload["actions"][0]["prompt"]
        assert "python -m orchestrator" in payload["actions"][0]["prompt"]


def test_finder_triggers_on_merged_prs_only_and_verifies_every_merge_by_default():
    assert "VERIFY_EVERY_N_MERGES=1 " in automations.finder_payload(REPO, AUTO)["actions"][0]["prompt"]
    assert (
        "VERIFY_EVERY_N_MERGES=5 "
        in automations.finder_payload(REPO, AUTO, every_n=5)["actions"][0]["prompt"]
    )
    finder = automations.finder_payload(REPO, AUTO, issue_window_hours=6)
    (r,) = finder["triggers"]
    assert r["event_type"] == "github:pull_request"
    conds = {c["field"]: c["value"] for c in r["conditions"]["any"][0]["all"]}
    assert conds == {"action": "closed", "pull_request.merged": True, "repository.full_name": REPO}
    prompt = finder["actions"][0]["prompt"]
    assert "REGRESSION_ISSUE_WINDOW_HOURS=6 " in prompt and "python -m orchestrator find-and-fix" in prompt


def test_invalid_payloads_are_rejected():
    bad = automations.finder_payload(REPO, AUTO)
    bad["limits"] = {"max_acu_limit": 10}
    with pytest.raises(ValueError, match="max_acu_limit"):
        automations.assert_no_ceilings(bad)
    bad = automations.finder_payload(REPO, AUTO)
    bad["actions"].append({"type": "start_session", "prompt": "second"})
    with pytest.raises(ValueError, match="at most one"):
        automations.assert_no_ceilings(bad)
    bad = automations.finder_payload(REPO, AUTO)
    bad["triggers"][0]["event_type"] = "cron"
    assert automations.validate_payload(bad)


def test_register_is_idempotent_by_name():
    devin = FakeDevin()
    automations.register(devin, REPO, AUTO)
    automations.register(devin, REPO, AUTO)
    assert len(devin.automations) == 1
    (only,) = devin.automations.values()
    assert only["name"] == automations.FINDER_NAME and "schedule" not in json.dumps(only["triggers"])


class OrderedDevin(FakeDevin):
    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []
        self.fail_create = False

    def create_automation(self, body):
        if self.fail_create:
            raise RuntimeError("api down")
        self.order.append("create")
        return super().create_automation(body)

    def delete_automation(self, automation_id: str) -> None:
        self.order.append("delete")
        super().delete_automation(automation_id)


def with_retired_automations(devin):
    for name in automations.RETIRED_NAMES:
        devin.create_automation({"name": name})
    devin.order.clear()
    return devin


def test_register_deletes_retired_automations_only_after_the_replacements_exist():
    devin = with_retired_automations(OrderedDevin())
    results = automations.register(devin, REPO, AUTO)
    assert devin.order == ["create"] + ["delete"] * len(automations.RETIRED_NAMES)
    assert {a["name"] for a in devin.automations.values()} == {automations.FINDER_NAME}
    assert [r["name"] for r in results if r.get("deleted")] == list(automations.RETIRED_NAMES)

    failing = with_retired_automations(OrderedDevin())
    failing.fail_create = True
    with pytest.raises(RuntimeError):
        automations.register(failing, REPO, AUTO)
    assert {a["name"] for a in failing.automations.values()} == set(automations.RETIRED_NAMES)


# --- whole loop --------------------------------------------------------------------------------


def test_simulate_runs_end_to_end(registry):
    out = cmd_simulate(load_settings(simulate=True), registry)
    finished = {f["issue"]: f for f in out["friday_1_autopr_sweep"]["finished"]}
    assert finished[5]["verdict"] == "acceptance met" and finished[5]["pr_url"].endswith("/pull/14")
    assert finished[5]["acus"] == 3.2 and finished[5]["posted"]
    assert all(f["verdict"] == "error" for i, f in finished.items() if i != 5)
    assert out["issue_comment_ledger"]["#5"] == [
        "**Remediation session started**",
        "**Remediation session finished: acceptance met**",
    ]
    # errored sessions free their slot; the fixed one is closed and not retried
    retried = {s["issue"] for s in out["friday_1_autopr_rerun_retries_only_the_errored"]["started"]}
    assert retried == set(finished) - {5}
    assert out["merge_testing_replay_is_deduplicated"]["skipped_reason"]
    assert out["verification_structured_output"]["acceptance_met"] is True
    assert out["merge_testing"]["verdict"] == "acceptance met"
    assert out["merge_testing"]["posted_to"] == out["merge_testing"]["window_prs"]
    assert out["friday_1_autopr_sweep"]["deflected"] and out["issue_comment_ledger"]["#12"] == [
        "**Triage: deflected, no session started (0 ACU)**"
    ]


def test_simulate_chains_testing_to_autopr_through_the_issue_event(registry):
    out = cmd_simulate(load_settings(simulate=True), registry)
    failed = out["regressing_merge_testing"]
    assert failed["verdict"] == "acceptance NOT met"
    issue = failed["regression_filed"]["issue"]
    started = out["regression_issue_event_autopr"]["started"]
    assert [s["issue"] for s in started] == [issue]
    fix = out["regression_issue_event_autopr"]["finished"][0]
    assert fix["issue"] == issue and fix["verdict"] == "acceptance met" and fix["pr_url"]
    replay = out["regression_issue_event_replay_is_deduplicated"]
    assert replay["started"] == [] and "already closes" in replay["skipped_in_flight"][0]["reason"]
    assert out["human_labelled_issue_starts_nothing"]["started"] == []
    assert (
        "TESTING did not file it"
        in out["human_labelled_issue_starts_nothing"]["skipped_in_flight"][0]["reason"]
    )
    assert out["issue_comment_ledger"][f"#{issue}"] == [
        "**Regression lineage**",
        "**Remediation session started**",
        "**Remediation session finished: acceptance met**",
    ]


def test_simulate_every_fifth_merge(registry, monkeypatch):
    monkeypatch.setenv("VERIFY_BRANCH", "master")
    monkeypatch.setenv("VERIFY_EVERY_N_MERGES", "5")
    out = cmd_simulate(load_settings(simulate=True), registry)
    counted = out["merges_counted_not_verified"]
    assert [c["merge_index"] for c in counted] == [1, 2, 3, 4]
    assert all("of 5" in c["reason"] for c in counted)
    assert out["merge_testing"]["merge_index"] == 5 and out["merge_testing"]["session_id"]
    assert out["merge_testing"]["requirements"] == registry.requirement_ids()


# --- autopr waits for its sessions ---------------------------------------------------------------


def finish_fix_sessions(devin, outcomes):
    """A sleep stand-in: each pending fix session gets the outcome keyed by its issue."""

    def _sleep(_seconds):
        for sid, session in list(devin.sessions.items()):
            if "sda-fix" not in session["tags"] or session["structured_output"] is not None:
                continue
            issue = next(int(t[6:]) for t in session["tags"] if t.startswith("issue-"))
            outcome, kwargs = outcomes.get(issue, ("error", {"acus": 0.5}))
            devin.advance(sid, outcome=outcome, **kwargs)

    return _sleep


def do_map_wait(devin, gh, registry, outcomes):
    return run_autopr(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        ready_label="ready",
        wait=True,
        sleep=finish_fix_sessions(devin, outcomes),
    )


def test_autopr_with_wait_posts_each_fix_outcome_on_its_issue(registry, world):
    devin, gh = world
    report = do_map_wait(
        devin,
        gh,
        registry,
        {5: ("ok", {"acus": 3.0, "pr_url": "https://github.com/jhomer192/superset/pull/14"})},
    )
    assert len(report.finished) == len(report.started) == 7
    by_issue = {f["issue"]: f for f in report.finished}
    assert by_issue[5]["verdict"] == "acceptance met" and by_issue[5]["acus"] == 3.0
    assert by_issue[5]["pr_url"] == "https://github.com/jhomer192/superset/pull/14"
    assert by_issue[10]["verdict"] == "error" and by_issue[10]["status"] == "error"

    bodies = {n: "\n".join(c["body"] for c in cs) for n, cs in gh.comments.items()}
    assert "acceptance met" in bodies[5] and "ACUs: 3" in bodies[5]
    assert "error: simulated failure before any probe ran" in bodies[10]
    ledger = IssueLedger(gh, REPO)
    for issue in by_issue:
        reported = find(ledger.read(issue), "session_reported", session_id=by_issue[issue]["session_id"])
        assert len(reported) == 1 and reported[0].data["kind"] == "fix"


def test_autopr_wait_outlives_a_session_parked_on_a_human(registry, world):
    devin, gh = world
    polls = []

    def _sleep(_seconds):
        polls.append(1)
        for sid, session in list(devin.sessions.items()):
            if "sda-fix" not in session["tags"] or session["structured_output"] is not None:
                continue
            if session["status"] != "suspended":
                devin.suspend(sid, "waiting_for_user")
            elif len(polls) > 2:
                devin.advance(sid, outcome="error", acus=1.0)

    report = run_autopr(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        ready_label="ready",
        wait=True,
        sleep=_sleep,
    )
    assert len(report.finished) == len(report.started) and len(polls) > 2


def test_a_fix_session_that_finished_without_structured_output_is_still_reported(registry, world):
    devin, gh = world

    def _sleep(_seconds):
        for session in devin.sessions.values():
            if "sda-fix" in session["tags"]:
                session["status"] = "exit"

    report = run_autopr(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        ready_label="ready",
        wait=True,
        sleep=_sleep,
    )
    assert all(f["verdict"] == "no structured output" for f in report.finished)
    assert "no structured output" in gh.comments[5][-1]["body"]


# --- self-healing regressions -------------------------------------------------------------------


def finish_verification(devin, outcome, acus=5.0):
    def _sleep(_seconds):
        for sid, session in devin.sessions.items():
            if "sda-verify" in session["tags"] and session["structured_output"] is None:
                devin.advance(sid, outcome=outcome, acus=acus)

    return _sleep


def verify_and_wait(devin, gh, registry, event=None, *, outcome="failed", every_n=1, branch="master"):
    """TESTING at every_n=1 by default: one merged PR event is one verification, waited for."""
    return run_testing(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        event=event or load_event(),
        verify_branch=branch,
        every_n=every_n,
        wait=True,
        sleep=finish_verification(devin, outcome),
    )


def fail_verification(devin, gh, registry):
    """A merged PR whose verification finds a probe failing at HEAD; TESTING files the regression."""
    return verify_and_wait(devin, gh, registry)


def do_autopr_for(devin, gh, registry, issue_number, *, action="labeled"):
    event = issue_event(gh.get_issue(REPO, issue_number), REPO, action=action)
    return run_autopr(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        ready_label="ready",
        event=event,
    )


def test_failed_verification_files_a_labelled_issue_and_its_event_starts_the_fix(registry, world):
    devin, gh = world
    testing = fail_verification(devin, gh, registry)
    session_id = testing.session_id

    entry = testing.regression_filed
    assert entry["pr"] == 14 and entry["probes"] == ["issue_5/unit"] and entry["depth"] == 1
    assert "session_id" not in entry

    issue = gh.issues[entry["issue"]]
    assert {label["name"] for label in issue["labels"]} == {"sda-regression", "regression"}
    assert "pull/14" in issue["body"] and "simulated pytest summary" in issue["body"]
    assert session_id in issue["body"]
    assert not [s for s in devin.sessions.values() if "sda-regression" in s["tags"]]

    autopr = do_autopr_for(devin, gh, registry, entry["issue"])
    assert autopr.trigger == "github:issues" and len(autopr.started) == 1
    fix = devin.sessions[autopr.started[0]["session_id"]]
    assert set(fix["tags"]) == {"sda-fix", "sda-regression", f"issue-{entry['issue']}"}
    assert fix["structured_output_schema"]["title"] == "SupersetFixResult"
    assert f"#{entry['issue']}" in fix["prompt"] and "issue_5/unit" in fix["prompt"]
    assert "Closes #" in fix["prompt"]
    started = find(IssueLedger(gh, REPO).read(entry["issue"]), "session_started")
    assert started[-1].data["trigger"] == "github:issues"


def test_testing_with_wait_publishes_the_verdict_and_files_the_regression_itself(registry, world):
    devin, gh = world

    def finish(_seconds):
        for sid, s in devin.sessions.items():
            if "sda-verify" in s["tags"] and s["structured_output"] is None:
                devin.advance(sid, outcome="failed", acus=5.0)

    report = run_testing(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        event=load_event(),
        every_n=1,
        wait=True,
        sleep=finish,
    )
    assert report.verdict == "acceptance NOT met" and report.posted_to == [14]
    assert report.regression_filed and report.regression_filed["pr"] == 14
    assert "Verification session finished: acceptance NOT met" in gh.comments[14][-2]["body"]
    assert "Regression filed" in gh.comments[14][-1]["body"]


def test_autopr_ignores_issue_events_the_loop_did_not_file(registry, world):
    devin, gh = world
    human = gh.create_issue(REPO, "flaky test", "please look", ["sda-regression"])
    report = do_autopr_for(devin, gh, registry, human["number"])
    assert report.started == [] and "TESTING did not file it" in report.skipped_in_flight[0]["reason"]
    assert not devin.sessions

    unlabelled = gh.create_issue(REPO, "typo", "", ["bug"])
    with pytest.raises(NotARegressionIssue):
        extract_regression_issue(issue_event(unlabelled, REPO, action="opened", label="bug"))
    with pytest.raises(NotARegressionIssue):
        do_autopr_for(devin, gh, registry, human["number"], action="closed")

    other_repo = {**issue_event(human, REPO), "repository": {"full_name": "someone/else"}}
    report = run_autopr(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        ready_label="ready",
        event=other_repo,
    )
    assert report.started == [] and "someone/else" in report.skipped_in_flight[0]["reason"]


def test_one_regression_issue_event_starts_one_fix_session_however_often_it_replays(registry, world):
    devin, gh = world
    issue = fail_verification(devin, gh, registry).regression_filed["issue"]
    first = do_autopr_for(devin, gh, registry, issue, action="opened")
    replay = do_autopr_for(devin, gh, registry, issue)
    assert len(first.started) == 1 and replay.started == []
    assert "holds the slot" in replay.skipped_in_flight[0]["reason"]
    assert len([s for s in devin.sessions.values() if "sda-regression" in s["tags"]]) == 1


def test_a_failure_is_only_filed_once(registry, world):
    devin, gh = world
    first = fail_verification(devin, gh, registry)
    issues_after = set(gh.issues)
    replay = verify_and_wait(devin, gh, registry)
    assert replay.session_id == first.session_id and replay.verdict == first.verdict
    assert replay.regression_filed is None and set(gh.issues) == issues_after
    assert find(IssueLedger(gh, REPO).read(14), "regression_filed", issue=first.regression_filed["issue"])


def fail_verification_at_every_n_5(devin, gh, registry):
    """Five merges into main; the fifth triggers a verification that fails at HEAD."""
    gh.branch_heads["main"] = "a" * 40
    gh.close_completed(5)
    issues_before = set(gh.issues)
    for i in range(1, 6):
        ev = merge_event(gh, 200 + i, branch="main", sha=f"{i:040x}")
        r = verify_and_wait(devin, gh, registry, ev, branch="main", every_n=5)
    assert r.merge_index == 5 and r.window_prs == [201, 202, 203, 204, 205] and r.session_id
    return r, issues_before


def test_one_failed_verification_at_every_n_5_files_one_issue_and_one_fix(registry, world):
    devin, gh = world
    reduce_report, issues_before = fail_verification_at_every_n_5(devin, gh, registry)

    # the verdict lands on every PR in the window
    assert reduce_report.posted_to == [201, 202, 203, 204, 205]
    # but the failure is remediated exactly once
    filed = reduce_report.regression_filed
    assert filed["pr"] == 205 and filed["window"] == [201, 202, 203, 204, 205]
    new_issues = set(gh.issues) - issues_before - {reduce_report.status_issue}
    assert new_issues == {filed["issue"]}
    fixes = [s for s in devin.sessions.values() if "sda-regression" in s["tags"]]
    assert fixes == []
    assert len(do_autopr_for(devin, gh, registry, filed["issue"]).started) == 1

    issue = gh.issues[filed["issue"]]
    assert "PR #205" in issue["title"]
    for n in range(201, 206):
        assert f"pull/{n}" in issue["body"]
    assert reduce_report.merge_commit_sha in issue["body"]

    ledger = IssueLedger(gh, REPO)
    assert find(ledger.read(205), "regression_filed", session_id=reduce_report.session_id)
    assert len([s for s in devin.sessions.values() if "sda-regression" in s["tags"]]) == 1
    assert set(gh.issues) - issues_before - {reduce_report.status_issue} == new_issues


def test_a_regression_chain_stops_after_two_automated_attempts(registry, world):
    devin, gh = world
    issue = fail_verification(devin, gh, registry).regression_filed["issue"]

    # The fix for that issue lands and regresses again, twice.
    depths = []
    for pr_number in (101, 102):
        # GitHub closes the issue when the PR that `Closes` it merges
        gh.close_completed(issue)
        pr = gh.merge(
            pr_number,
            branch="master",
            sha=f"{pr_number:040x}",
            title=f"fix: attempt {pr_number}",
            body=f"Closes #{issue}",
        )
        report = verify_and_wait(devin, gh, registry, {**load_event(), "pull_request": pr})
        depths.append(report.regression_filed)
        if report.regression_filed and not report.regression_filed.get("escalated"):
            issue = report.regression_filed["issue"]

    assert depths[0]["depth"] == 2 and not depths[0].get("escalated")
    assert depths[1]["escalated"] and (depths[1]["pr"], depths[1]["depth"]) == (102, 3)
    assert "a human needs to look at it" in gh.comments[102][-1]["body"]

    comments_before = len(gh.comments[102])
    session = devin.get_session(depths[1]["session_id"])
    assert publish_verification(
        devin=devin,
        gh=gh,
        ledger=IssueLedger(gh, REPO),
        target_repo=REPO,
        automation_repo=AUTO,
        session=session,
        threads=[102],
        acu_cache={},
    ) == ([], None)
    assert len(gh.comments[102]) == comments_before


class LabelWatcher(FakeGitHub):
    seen_at_label_time: dict[int, list[str]] = {}

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self.seen_at_label_time[number] = [c["body"] for c in self.comments.get(number, [])]
        super().add_labels(repo, number, labels)


def test_the_trigger_label_is_added_after_the_lineage_record(registry):
    devin, gh = FakeDevin(), LabelWatcher.from_fixtures()
    seen_at_label_time = LabelWatcher.seen_at_label_time
    issue = fail_verification(devin, gh, registry).regression_filed["issue"]
    assert any("regression_depth" in body for body in seen_at_label_time[issue])
    assert {lb["name"] for lb in gh.issues[issue]["labels"]} == {"sda-regression", "regression"}


def test_a_regression_already_filed_by_a_replayed_run_is_adopted_not_duplicated(registry, world):
    devin, gh = world
    session_id = do_reduce(devin, gh, registry).session_id
    devin.advance(session_id, outcome="failed", acus=5.0)
    # a replayed run already filed the issue, but its regression_filed marker has not landed yet
    other = gh.create_issue(REPO, "regression", f"Verification session: {session_id}", ["regression"])
    issues_before = set(gh.issues)
    session = devin.get_session(session_id)
    _, filed = publish_verification(
        devin=devin,
        gh=gh,
        ledger=IssueLedger(gh, REPO),
        target_repo=REPO,
        automation_repo=AUTO,
        session=session,
        threads=[14],
        acu_cache={},
    )
    assert filed is None and set(gh.issues) == issues_before
    adopted = find(IssueLedger(gh, REPO).read(14), "regression_filed", session_id=session_id)
    assert adopted and adopted[-1].data["issue"] == other["number"]


def publish_failed_session(devin, gh, session_id, probes):
    """Publish a fresh failed verification of PR 14 whose failing probe set is `probes`."""
    base = next(s for s in devin.sessions.values() if "sda-verify" in s["tags"])
    output = copy.deepcopy(base["structured_output"])
    output["results"] = [{**output["results"][0], "probe": p} for p in probes]
    session = {**base, "session_id": session_id, "url": f"https://app.devin.ai/sessions/{session_id}"}
    session["structured_output"] = output
    return publish_verification(
        devin=devin,
        gh=gh,
        ledger=IssueLedger(gh, REPO),
        target_repo=REPO,
        automation_repo=AUTO,
        session=session,
        threads=[14],
        acu_cache={},
    )


def test_remediate_adopts_open_regression_issue_with_same_probes(registry, world):
    devin, gh = world
    issue = fail_verification(devin, gh, registry).regression_filed["issue"]
    assert find(IssueLedger(gh, REPO).read(issue), "regression_depth")[-1].data["probes"] == ["issue_5/unit"]
    issues_before = set(gh.issues)
    comments_before = len(gh.comments[issue])

    _, filed = publish_failed_session(devin, gh, "verify-again", ["issue_5/unit"])
    assert filed is None and set(gh.issues) == issues_before
    adopted = find(IssueLedger(gh, REPO).read(14), "regression_filed", session_id="verify-again")
    assert len(adopted) == 1 and adopted[0].data["issue"] == issue and adopted[0].data["adopted"] is True
    assert f"#{issue} already tracks these probes" in gh.comments[14][-1]["body"]
    assert len(gh.comments[issue]) == comments_before + 1
    assert "verify-again" in gh.comments[issue][-1]["body"]


def test_remediate_files_when_probe_set_differs(registry, world):
    devin, gh = world
    issue = fail_verification(devin, gh, registry).regression_filed["issue"]
    issues_before = set(gh.issues)

    _, filed = publish_failed_session(devin, gh, "verify-wider", ["issue_5/unit", "issue_3/http"])
    assert filed and filed["issue"] not in issues_before and filed["issue"] != issue
    assert filed["probes"] == ["issue_5/unit", "issue_3/http"]


def test_remediate_ignores_closed_regression_issues(registry, world):
    devin, gh = world
    issue = fail_verification(devin, gh, registry).regression_filed["issue"]
    gh.issues[issue]["state"] = "closed"
    issues_before = set(gh.issues)

    _, filed = publish_failed_session(devin, gh, "verify-after-close", ["issue_5/unit"])
    assert filed and filed["issue"] not in issues_before
    entries = find(IssueLedger(gh, REPO).read(14), "regression_filed", session_id="verify-after-close")
    assert len(entries) == 1 and "adopted" not in entries[0].data


def test_an_errored_verification_is_reported_but_not_filed_as_a_regression(registry, world):
    devin, gh = world
    report = verify_and_wait(devin, gh, registry, outcome="error")
    assert report.verdict == "error" and report.posted_to == [14] and report.regression_filed is None


# --- verify/collect ---------------------------------------------------------------------------


def test_collect_accepts_a_requirement_probe_iff_it_exits_zero_at_head(registry, tmp_path):
    from verify.collect import build_results

    log = tmp_path / "l.log"
    log.write_text("x")
    rows = [
        {
            "probe": "issue_5/unit",
            "issue": 5,
            "kind": "offline_pytest",
            "log": str(log),
            "exit_code": 0,
        }
    ]
    out = build_results(rows, {"PRD-SQL-1"}, registry)[0]
    assert out["acceptance_met"] is True and out["requirements"] == ["PRD-SQL-1"]
    assert "base_exit_code" not in out and "BASE" not in out["evidence"]
    rows[0]["exit_code"] = 2
    assert build_results(rows, {"PRD-SQL-1"}, registry)[0]["acceptance_met"] is False
    rows[0]["exit_code"] = 0
    assert build_results(rows, {"PRD-OPS-1"}, registry)[0]["acceptance_met"] is False


def test_testing_run_is_logged_once_on_the_status_issue(registry, world):
    devin, gh = world
    report, _ = fail_verification_at_every_n_5(devin, gh, registry)
    status = report.status_issue
    assert status is not None and {lb["name"] for lb in gh.issues[status]["labels"]} == {"sda-status"}
    runs = find(IssueLedger(gh, REPO).read(status), "run_reported", stage="testing")
    assert len(runs) == 1
    assert runs[0].data["head_sha"] == report.merge_commit_sha
    assert runs[0].data["window"] == [201, 202, 203, 204, 205]
    assert runs[0].data["regression_issue"] == report.regression_filed["issue"]
    body = gh.comments[status][-1]["body"]
    assert report.session_id in body and "-> FAIL" in body and "regression issue" in body

    # replaying the same merge event appends nothing
    ev = merge_event(gh, 205, branch="main", sha=f"{5:040x}")
    verify_and_wait(devin, gh, registry, ev, branch="main", every_n=5)
    assert len(find(IssueLedger(gh, REPO).read(status), "run_reported", stage="testing")) == 1


def test_autopr_run_is_logged_on_the_status_issue_with_pr_and_acus(registry, world):
    devin, gh = world
    report, _ = fail_verification_at_every_n_5(devin, gh, registry)
    issue = report.regression_filed["issue"]

    def finish(_seconds):
        for sid, s in devin.sessions.items():
            if f"issue-{issue}" in s["tags"] and s["structured_output"] is None:
                devin.advance(sid, outcome="ok", pr_url=f"https://github.com/{REPO}/pull/900", acus=2.5)

    autopr = run_autopr(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        ready_label="ready",
        event=issue_event(gh.get_issue(REPO, issue), REPO),
        wait=True,
        sleep=finish,
    )
    status = report.status_issue
    assert status is not None and autopr.status_issue == status
    runs = find(IssueLedger(gh, REPO).read(status), "run_reported", stage="autopr")
    assert len(runs) == 1 and runs[0].data["finished"][0]["pr_url"].endswith("/pull/900")
    body = gh.comments[status][-1]["body"]
    assert "pull/900" in body and "ACU" in body


# --- find-and-fix -------------------------------------------------------------------------------------


def test_a_session_parked_waiting_for_user_with_its_output_written_counts_as_finished():
    devin = FakeDevin()
    sid = devin.create_session({"prompt": "x", "tags": ["sda-verify"], "structured_output_schema": {}})[
        "session_id"
    ]
    devin.sessions[sid]["status"], devin.sessions[sid]["status_detail"] = "running", "waiting_for_user"
    polls = []

    def _sleep(_s):
        polls.append(1)
        devin.sessions[sid]["structured_output"] = {"acceptance_met": False}

    session = wait_until_finished(devin, sid, _sleep)
    assert len(polls) == 1 and session["structured_output"] == {"acceptance_met": False}
    assert not is_finished("running", "waiting_for_user")


def finish_everything(devin, fix_outcome="ok"):
    """Sleep stand-in for a find-and-fix: verifications fail, fix sessions land with a PR."""

    def _sleep(_seconds):
        for sid, session in list(devin.sessions.items()):
            if session["structured_output"] is not None:
                continue
            if "sda-fix" in session["tags"]:
                devin.advance(sid, outcome=fix_outcome, acus=2.0, pr_url=f"https://github.com/{REPO}/pull/99")
            else:
                devin.advance(sid, outcome="failed", acus=5.0)

    return _sleep


def do_finder(devin, gh, registry, **kwargs):
    return run_find_and_fix(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        event=load_event(),
        verify_branch="master",
        every_n=1,
        sleep=finish_everything(devin),
        **kwargs,
    )


def test_finder_verifies_fixes_every_recent_regression_waits_and_reports(registry, world):
    devin, gh = world
    out = do_finder(devin, gh, registry)
    issue = out.testing["regression_filed"]["issue"]
    assert out.testing["verdict"] == "acceptance NOT met" and out.candidates == [issue]
    (finished,) = out.fixes["finished"]
    assert finished["issue"] == issue and finished["verdict"] == "acceptance met"
    assert finished["pr_url"].endswith("/pull/99") and finished["acus"] == 2.0
    fixes = [s for s in devin.sessions.values() if "sda-fix" in s["tags"]]
    assert len(fixes) == 1 and f"issue-{issue}" in fixes[0]["tags"]
    assert out.status_issue is not None
    rollup = gh.comments[out.status_issue][-1]["body"]
    assert "find-and-fix:" in rollup and f"#{issue}" in rollup and "total ACUs: 2" in rollup
    assert find(IssueLedger(gh, REPO).read(out.status_issue), "run_reported", stage="find-and-fix")


def test_finder_fix_sessions_use_the_fix_playbook_and_record_their_trigger(registry, world):
    devin, gh = world
    out = do_finder(devin, gh, registry, playbook_id="playbook-verify", fix_playbook_id="playbook-fix")
    issue = out.testing["regression_filed"]["issue"]
    (fix,) = [s for s in devin.sessions.values() if "sda-fix" in s["tags"]]
    assert fix["prompt"].startswith(f"@{REPO} @playbook:playbook-fix\n")
    assert "On top of the playbook workflow" in fix["prompt"] and "Workflow:\n1." not in fix["prompt"]
    (verify,) = [s for s in devin.sessions.values() if "sda-fix" not in s["tags"]]
    assert "@playbook:playbook-verify" in verify["prompt"] and "playbook-fix" not in verify["prompt"]
    started = find(IssueLedger(gh, REPO).read(issue), "session_started")
    assert started[-1].data["trigger"] == "find-and-fix"


def test_regression_fix_prompt_inlines_the_workflow_only_without_a_playbook():
    def prompt(playbook_id=None):
        return prompts.regression_fix_prompt(
            target_repo=REPO,
            automation_repo=AUTO,
            issue_number=41,
            issue_url=f"https://github.com/{REPO}/issues/41",
            pr_url=f"https://github.com/{REPO}/pull/37",
            head_sha="a" * 40,
            probes=["prd/api_requires_auth"],
            playbook_id=playbook_id,
        )

    bare = prompt()
    assert bare.startswith(f"@{REPO}\n") and "Workflow:\n1." in bare and "Closes #41" in bare
    assert "On top of the playbook workflow" not in bare
    with_playbook = prompt("pb")
    assert with_playbook.startswith(f"@{REPO} @playbook:pb\n")
    assert "Workflow:\n1." not in with_playbook and "tests/unit_tests/" in with_playbook
    assert "prd/api_requires_auth" in with_playbook and "pull/37" in with_playbook


def test_finder_fixes_the_issue_it_just_filed_even_when_the_label_listing_lags(registry, world):
    devin, gh = world
    real_list = gh.list_issues
    listed_before = {n for n in gh.issues}

    def lagging_list(repo, labels, state="open"):
        return [i for i in real_list(repo, labels, state) if i["number"] in listed_before]

    gh.list_issues = lagging_list
    out = do_finder(devin, gh, registry)
    issue = out.testing["regression_filed"]["issue"]
    assert issue not in listed_before and out.candidates == [issue]
    (finished,) = out.fixes["finished"]
    assert finished["issue"] == issue


def test_finder_replay_starts_nothing_new_and_reports_once(registry, world):
    devin, gh = world
    first = do_finder(devin, gh, registry)
    issue = first.testing["regression_filed"]["issue"]
    gh.open_pull(99, title=f"fix: regression #{issue}", body=f"Closes #{issue}")
    before = len(devin.sessions), len(gh.comments[first.status_issue])
    again = do_finder(devin, gh, registry)
    assert again.fixes["started"] == [] and again.status_issue is None
    assert "already closes" in again.fixes["skipped_in_flight"][0]["reason"]
    assert (len(devin.sessions), len(gh.comments[first.status_issue])) == before


def test_finder_ignores_regression_issues_older_than_the_window(registry, world):
    devin, gh = world
    verify_and_wait(devin, gh, registry)
    for issue in gh.issues.values():
        if any(lb["name"] == "sda-regression" for lb in issue["labels"]):
            issue["created_at"] = "2000-01-01T00:00:00Z"
    out = do_finder(devin, gh, registry, issue_window_hours=1)
    assert out.candidates == [] and out.fixes["started"] == []
    assert not [s for s in devin.sessions.values() if "sda-fix" in s["tags"]]


def test_finder_on_an_uncounted_merge_only_counts(registry, world):
    devin, gh = world
    out = run_find_and_fix(
        devin=devin,
        gh=gh,
        registry=registry,
        target_repo=REPO,
        automation_repo=AUTO,
        event=load_event(),
        verify_branch="master",
        every_n=5,
        sleep=finish_everything(devin),
    )
    assert out.testing["session_id"] is None and out.testing["skipped_reason"]
    assert out.fixes["started"] == [] and out.status_issue is None and devin.sessions == {}


# --- playbooks -----------------------------------------------------------------------------------


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
    for needle in ("requirements/development.txt", "non-zero", "exit 0", "AGENTS.md", "Closes #"):
        assert needle in fix, needle
    for needle in (
        "verify/run_all.sh",
        "--requirements",
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

    reqs = ["PRD-SQL-1"]
    probes = [p for _, p in registry.requirement_probes(reqs)]
    with_pb = prompts.verification_prompt(REPO, AUTO, "h" * 40, "https://x/pr/1", reqs, probes, "pb-v")
    inline = prompts.verification_prompt(REPO, AUTO, "h" * 40, "https://x/pr/1", reqs, probes)
    assert "@playbook:pb-v" in with_pb and "--head " + "h" * 40 in with_pb and "npm ci" not in with_pb
    assert "@playbook:" not in inline and prompts.VERIFY_PLAYBOOK_BODY in inline


def test_autopr_and_testing_run_with_and_without_playbook_ids(registry, world, caplog):
    devin, gh = world
    with caplog.at_level("WARNING"):
        report = do_map(devin, gh, registry)
    assert report.started and "PLAYBOOK_ID_FIX unset" in caplog.text
    assert all("@playbook:" not in s["prompt"] for s in devin.sessions.values())

    devin2, gh2 = FakeDevin(), FakeGitHub.from_fixtures()
    run_autopr(
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
    r2 = run_testing(
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


def test_automation_shim_passes_playbook_ids_through():
    c = automations.finder_payload(REPO, AUTO, "main", 5, playbook_id_verify="pb-2", playbook_id_fix="pb-1")
    assert (
        "PLAYBOOK_ID_VERIFY=pb-2 PLAYBOOK_ID_FIX=pb-1 python -m orchestrator find-and-fix"
        in c["actions"][0]["prompt"]
    )
    assert automations.validate_payload(c) == []
    assert "PLAYBOOK_ID" not in automations.finder_payload(REPO, AUTO)["actions"][0]["prompt"]


def test_finder_fills_trimmed_event_from_the_api(registry, world):
    devin, gh = world
    event = load_event()
    trimmed = {
        "action": "closed",
        "pull_request": {k: event["pull_request"][k] for k in ("number", "merged", "base", "title")},
        "repository": event["repository"],
    }
    with pytest.raises(NotAMergedPR, match="merge_commit_sha"):
        extract_merged_pr(trimmed)
    report = do_reduce(devin, gh, registry, event=trimmed)
    assert report.session_id and report.merge_commit_sha == event["pull_request"]["merge_commit_sha"]
    assert report.pr_url == event["pull_request"]["html_url"]


# --- PRD requirements ----------------------------------------------------------------------------


def test_registry_maps_every_prd_requirement_to_known_probes(registry):
    assert registry.requirement_ids()[:1] == ["PRD-SEC-1"]
    known = registry.all_probes()
    for req in registry.requirements:
        assert req.probes, req.id
        assert all(p in known for p in req.probes), req.id
    assert known["prd/health"][0] == 0 and known["issue_1/unit"][0] == 1
    ids = [p.id for _, p in registry.requirement_probes(["PRD-SEC-1", "PRD-OPS-1"])]
    assert ids == ["issue_1/unit", "issue_1/startup_log", "prd/health"]


def test_registry_rejects_requirement_naming_an_unknown_probe(tmp_path):
    import json
    from pathlib import Path

    from orchestrator.registry import REGISTRY_PATH

    raw = json.loads(Path(REGISTRY_PATH).read_text())
    raw["prd"]["requirements"].append({"id": "PRD-X", "title": "x", "probes": ["nope/none"]})
    probes_dir = tmp_path / "probes"
    probes_dir.mkdir()
    for p in [p for i in raw["issues"] for p in i["probes"]] + raw["probes"]:
        (tmp_path / p["script"]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p["script"]).write_text("")
    bad = probes_dir / "registry.json"
    bad.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="PRD-X"):
        load_registry(bad)


def test_collect_guards_prd_requirement_probes_at_head(registry, tmp_path):
    from verify.collect import build_results

    log = tmp_path / "l.log"
    log.write_text("x")
    rows = [
        {
            "probe": "prd/health",
            "issue": 0,
            "kind": "live_http",
            "log": str(log),
            "exit_code": 0,
        },
    ]
    out = build_results(rows, {"PRD-OPS-1"}, registry)
    assert out[0]["acceptance_met"] is True and out[0]["requirements"] == ["PRD-OPS-1"]
    assert build_results(rows, set(), registry)[0]["acceptance_met"] is False
    rows[0]["exit_code"] = 1
    assert build_results(rows, {"PRD-OPS-1"}, registry)[0]["acceptance_met"] is False
    from orchestrator.schema import VERIFICATION_SCHEMA, validate

    validate(
        {
            "status": "success",
            "head_sha": "h" * 40,
            "acceptance_met": False,
            "probe_command": "x",
            "probe_exit_code": 1,
            "evidence": "e",
            "results": build_results(rows, {"PRD-OPS-1"}, registry),
        },
        VERIFICATION_SCHEMA,
    )


def test_regression_issue_names_the_violated_requirement():
    from orchestrator.regression import failures, issue_body, issue_title

    output = {
        "results": [
            {
                "probe": "prd/health",
                "issue": 0,
                "requirements": ["PRD-OPS-1"],
                "acceptance_met": False,
                "head_exit_code": 1,
                "evidence": "boom",
            },
            {
                "probe": "issue_5/unit",
                "issue": 5,
                "requirements": ["PRD-SQL-1"],
                "acceptance_met": True,
                "head_exit_code": 0,
                "evidence": "",
            },
        ]
    }
    items = failures(output)
    assert [f.probe for f in items] == ["prd/health"]
    assert issue_title(27, items) == "PRD violated after PR #27: PRD-OPS-1 (prd/health)"
    body = issue_body(
        target_repo=REPO,
        automation_repo=AUTO,
        pr_url="https://x/pr/27",
        window_prs=[27],
        head_sha="h" * 40,
        items=items,
        session_url="https://s",
    )
    assert "| prd/health | PRD-OPS-1 | 1 |" in body and "BASE" not in body


def test_verification_session_guards_every_prd_requirement(registry, world):
    devin, gh = world
    report, _ = fail_verification_at_every_n_5(devin, gh, registry)
    assert report.requirements == registry.requirement_ids()
    prompt = devin.sessions[report.session_id]["prompt"]
    assert '--requirements "PRD-SEC-1,' in prompt and "prd/health (live_http)" in prompt
