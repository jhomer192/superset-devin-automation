"""Session prompts.

``@owner/repo`` and ``@playbook:{id}`` tokens are what the API derives the read-only ``repos``
and ``playbook_id`` session fields from. Each prompt has two parts: the invariant workflow
(``*_playbook_body``, published as an org playbook by ``orchestrator/playbooks.py``) and the
per-session variables. With a playbook id the prompt is the token plus the variables; without
one the body is inlined so the loop keeps working when no playbook is registered.
"""

from __future__ import annotations

from .registry import IssueSpec, Probe

FIX_PLAYBOOK_BODY = """\
You are fixing one GitHub issue in the target repository named in the prompt. The prompt gives
the issue number, title, URL, cited condition and the deciding probes; everything below is the
same on every run.

Read the full issue body and its comments first; the acceptance criteria there are binding.

The probes live in the automation repository named in the prompt; clone it next to the target
checkout. Each probe is run as `probes/run.sh <probe-id>` with SUPERSET_SRC pointing at the target
checkout and PROBE_PYTHON at the venv python.

Workflow:
1. Clone the target repository at master. Create a branch. Install requirements/development.txt
   into a venv.
2. Before changing anything, run each deciding probe. Every probe MUST exit non-zero at this
   point. Record the exit codes; they go in base_probe_exit_code.
3. Implement the fix following the issue's acceptance criteria. Keep the change minimal. Follow
   AGENTS.md in the target repository (ASF headers, type hints, pre-commit on changed files).
4. Re-run each probe. Every probe MUST exit 0. Do not edit anything under the probes/ tree of the
   automation repository; if a probe is wrong, stop and report status "error" with the reason.
5. Open a pull request against the target repository's master whose body contains the line
   "Closes #<issue number>". PR title in Conventional Commits form. No AI-attribution footers
   anywhere (no Co-Authored-By, no "Generated with").

Code rules:
- Keep the fix minimal and clean: no compatibility shims, no feature flags around the change.
- Code the fix makes unreachable is deleted in the same PR. Do not deprecate, comment out or
  leave a TODO for it.

Writing rules for everything you author (commit messages, PR body, code comments, docstrings):
- Say what changed and why, once. No summary or "Overall" paragraph restating the diff, no
  padded bullet lists.
- No negative parallelism ("not just X, it is Y"), no chiasmus or rhetorical mirroring.
- Do not repeat a rule verbatim that the codebase already states; reference it.
- Claim only what you ran. If a check ran in a narrower scope than the sentence implies, say
  which scope.

Structured output rules (enforced by schema):
- every status: include the issue number given in the prompt as `issue`.
- status "pr_opened": include pr_url, branch, acceptance_met=true, probe_command, probe_exit_code=0,
  base_probe_exit_code (non-zero), and evidence (the tail of the probe output at head and base).
- status "no_change_needed": only if the probes already pass at master; include the same fields.
- status "error": include error_message only; do NOT include acceptance_met or pr_url.
acceptance_met is the probe's exit code being 0, nothing else.
"""

VERIFY_PLAYBOOK_BODY = """\
You are verifying that the target repository still meets its PRD (PRD.md in that repository)
after a merge. The prompt gives the target and automation repositories, the HEAD commit, the PR
that triggered the run, the PRD requirement ids and the probes; everything below is the same on
every run.

Do exactly this; the verdict comes from exit codes, not from your reading of the code:

1. git clone the automation repository into ./automation and cd into it.
2. Run the verify/run_all.sh command line given in the prompt (--repo, --head, --requirements).
   The script clones the target repository at HEAD, stands up a real Postgres and Redis in
   docker, installs Python requirements, runs `npm ci && npm run build` in superset-frontend,
   boots Superset against Postgres and waits for `/health`, then runs every probe of the named
   requirements against that app and writes verify/out/result.json. It exits 0 only if the build
   and boot succeeded and every probe exited 0.
3. Copy verify/out/result.json into the structured output verbatim where fields overlap:
   - status "ok" when the script produced a result file (even if acceptance_met is false).
   - acceptance_met = the script's overall verdict (exit code 0).
   - probe_command = the exact verify/run_all.sh command line you ran.
   - probe_exit_code = its exit code.
   - results = the per-probe array from the result file (issue, probe, kind, head_exit_code,
     acceptance_met, evidence, requirements).
   - evidence = the last ~200 lines of the script's stdout/stderr.
   - status "error" with error_message only when the script could not run at all (no result
     file). Do NOT include acceptance_met in that case.
Do not modify anything under probes/ or verify/ in the automation checkout, and do not patch the
target checkout; if a probe cannot run, report status "error". Do not open a PR.

Anything you write (evidence, error_message, comments): what happened and where, once, in the
scope you actually observed. No summary paragraphs, no negative parallelism ("not just X, it is
Y"), no claims about checks you did not run.
"""


def _header(target_repo: str, playbook_id: str | None) -> str:
    return f"@{target_repo}" + (f" @playbook:{playbook_id}" if playbook_id else "")


def fix_session_prompt(
    target_repo: str,
    automation_repo: str,
    issue: dict[str, object],
    spec: IssueSpec,
    playbook_id: str | None = None,
) -> str:
    probes = "\n".join(f"  - {p.id} ({p.kind}): probes/run.sh {p.id}" for p in spec.probes)
    variables = f"""\
Fix GitHub issue #{spec.number} in {target_repo}: {spec.title}

Issue URL: {issue.get("html_url", f"https://github.com/{target_repo}/issues/{spec.number}")}
Cited condition: {spec.condition}
Automation repository (probes): https://github.com/{automation_repo}
Deciding probes:
{probes}
"""
    body = "" if playbook_id else "\n" + FIX_PLAYBOOK_BODY
    return f"{_header(target_repo, playbook_id)}\n\n{variables}{body}"


def regression_fix_prompt(
    *,
    target_repo: str,
    automation_repo: str,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    head_sha: str,
    probes: list[str],
) -> str:
    probe_lines = "\n".join(f"  - {p}" for p in probes)
    return f"""@{target_repo}

Fix GitHub issue #{issue_number} in {target_repo}: a probe fails on master after {pr_url} merged.

Issue URL: {issue_url}
Failing commit: {head_sha}
Failing probes (deciding; they live in https://github.com/{automation_repo}):
{probe_lines}

Read the issue body first; its acceptance criteria are binding.

Workflow:
1. Clone {target_repo} at master and https://github.com/{automation_repo} next to it. Create a
   branch. Install requirements/development.txt into a venv. Never push to master.
2. Reproduce first: run each failing probe with SUPERSET_SRC pointing at your checkout and
   PROBE_PYTHON at the venv python. Every one MUST exit non-zero before you change anything;
   record those exit codes for base_probe_exit_code. If they all pass, the regression is already
   gone: report status "no_change_needed" with that evidence and open no PR.
3. Implement the smallest fix that addresses the cause, not the symptom. Follow AGENTS.md in the
   superset repo (ASF headers, type hints, pre-commit on changed files).
4. Add or extend a unit test under superset/tests/unit_tests/ that fails without your fix.
5. Re-run every failing probe (all MUST exit 0), the test file you touched, and the surrounding
   unit-test directory so you know the fix broke nothing else. Do not edit anything under the
   probes/ tree of {automation_repo}; if a probe is wrong, stop and report status "error".
6. Only once step 5 is green, open a pull request against {target_repo} master whose body
   contains the line "Closes #{issue_number}", the probe exit codes before and after, and the
   test command output. PR title in Conventional Commits form. No AI-attribution footers.
7. Code the fix makes unreachable is deleted in the same PR, never deprecated or left behind a
   flag. Writing rules for the commit message, PR body and any comments: state what changed and why
   once, with no summary paragraph or padded lists; no negative parallelism ("not just X, it is
   Y") or rhetorical mirroring; do not restate rules the codebase already carries; claim only
   what you ran, and say the scope if it was narrower than the sentence implies.

Structured output rules (enforced by schema):
- every status: include issue={issue_number}.
- status "pr_opened": include pr_url, branch, acceptance_met=true, probe_command, probe_exit_code=0,
  base_probe_exit_code (non-zero), and evidence (probe output at base and head plus the test run).
- status "no_change_needed": only when the probes already pass before any change; include the same
  fields.
- status "error": include error_message only; do NOT include acceptance_met or pr_url.
acceptance_met is the probe's exit code being 0, nothing else.
"""


def verification_command(target_repo: str, head_sha: str, requirements: list[str]) -> str:
    return (
        f'verify/run_all.sh --repo {target_repo} --head {head_sha} --requirements "{",".join(requirements)}"'
    )


def verification_prompt(
    target_repo: str,
    automation_repo: str,
    head_sha: str,
    pr_url: str,
    requirements: list[str],
    probes: list[Probe],
    playbook_id: str | None = None,
) -> str:
    probe_lines = "\n".join(f"  - {p.id} ({p.kind})" for p in probes) if probes else "  (none)"
    variables = f"""\
PRD verification for {pr_url} (merged into {target_repo}).

HEAD (merged commit): {head_sha}
PRD requirements (PRD.md in {target_repo}): {", ".join(requirements) or "none"}
Automation repository: https://github.com/{automation_repo}
Command: {verification_command(target_repo, head_sha, requirements)}
Probes to run (from probes/registry.json in the automation repository):
{probe_lines}
"""
    body = "" if playbook_id else "\n" + VERIFY_PLAYBOOK_BODY
    return f"{_header(target_repo, playbook_id)}\n\n{variables}{body}"
