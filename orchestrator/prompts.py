"""Session prompts. ``@owner/repo`` tokens are what the API derives ``repos`` from (read-only)."""

from __future__ import annotations

from .registry import IssueSpec, Probe


def fix_session_prompt(
    target_repo: str, automation_repo: str, issue: dict[str, object], spec: IssueSpec
) -> str:
    probes = "\n".join(f"  - {p.id} ({p.kind}): probes/run.sh {p.id}" for p in spec.probes)
    return f"""@{target_repo}

Fix GitHub issue #{spec.number} in {target_repo}: {spec.title}

Issue URL: {issue.get("html_url", f"https://github.com/{target_repo}/issues/{spec.number}")}
Cited condition: {spec.condition}

Read the full issue body and its comments first; the acceptance criteria there are binding.

Deciding probes live in https://github.com/{automation_repo} (clone it next to the superset checkout):
{probes}

Workflow:
1. Clone {target_repo} at master. Create a branch. Install requirements/development.txt into a venv.
2. Before changing anything, run each probe above with SUPERSET_SRC pointing at your checkout and
   PROBE_PYTHON at the venv python. Every probe MUST exit non-zero at this point. Record the exit
   codes; they go in base_probe_exit_code.
3. Implement the fix following the issue's acceptance criteria. Keep the change minimal. Follow
   AGENTS.md in the superset repo (ASF headers, type hints, pre-commit on changed files).
4. Re-run each probe. Every probe MUST exit 0. Do not edit anything under the probes/ tree of
   {automation_repo}; if a probe is wrong, stop and report status "error" with the reason.
5. Open a pull request against {target_repo} master whose body contains the line
   "Closes #{spec.number}". PR title in Conventional Commits form. No AI-attribution footers
   anywhere (no Co-Authored-By, no "Generated with").

Structured output rules (enforced by schema):
- status "pr_opened": include pr_url, branch, acceptance_met=true, probe_command, probe_exit_code=0,
  base_probe_exit_code (non-zero), and evidence (the tail of the probe output at head and base).
- status "no_change_needed": only if the probes already pass at master; include the same fields.
- status "error": include error_message only; do NOT include acceptance_met or pr_url.
acceptance_met is the probe's exit code being 0, nothing else.
"""


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

Structured output rules (enforced by schema):
- status "pr_opened": include pr_url, branch, acceptance_met=true, probe_command, probe_exit_code=0,
  base_probe_exit_code (non-zero), and evidence (probe output at base and head plus the test run).
- status "no_change_needed": only when the probes already pass before any change.
- status "error": include error_message only; do NOT include acceptance_met or pr_url.
acceptance_met is the probe's exit code being 0, nothing else.
"""


def verification_prompt(
    target_repo: str,
    automation_repo: str,
    head_sha: str,
    base_sha: str,
    pr_url: str,
    issue_numbers: list[int],
    probes: list[Probe],
) -> str:
    probe_lines = "\n".join(f"  - {p.id} ({p.kind})" for p in probes) if probes else "  (none)"
    issues = ", ".join(f"#{n}" for n in issue_numbers) or "none referenced"
    return f"""@{target_repo}

Regression verification for {pr_url} (merged into master of {target_repo}).

HEAD (merged commit): {head_sha}
BASE (first parent):  {base_sha}
Issues this PR closes: {issues}
Probes to run (from probes/registry.json in {automation_repo}):
{probe_lines}

Do exactly this; the verdict comes from exit codes, not from your reading of the code:

1. git clone https://github.com/{automation_repo} automation && cd automation
2. Run:
     verify/run_all.sh --repo {target_repo} --head {head_sha} --base {base_sha} \\
       --issues "{",".join(str(n) for n in issue_numbers)}"
   This script clones {target_repo} at HEAD and at BASE, stands up a real Postgres and Redis in
   docker, installs Python requirements, runs `npm ci && npm run build` in superset-frontend,
   boots Superset against Postgres and waits for `/health`, then runs every probe at HEAD and
   again at BASE and writes verify/out/result.json. It exits 0 only if the build and boot
   succeeded and every probe for the closed issues passes at HEAD and MUST FAIL at BASE (a probe
   that already passes at BASE proves nothing and fails the verification).
3. Copy verify/out/result.json into the structured output verbatim where fields overlap:
   - status "ok" when the script produced a result file (even if acceptance_met is false).
   - acceptance_met = the script's overall verdict (exit code 0).
   - probe_command = the exact verify/run_all.sh command line you ran.
   - probe_exit_code = its exit code.
   - results = the per-probe array from the result file (issue, probe, kind, head_exit_code,
     base_exit_code, acceptance_met, evidence).
   - evidence = the last ~200 lines of the script's stdout/stderr.
   - status "error" with error_message only when the script could not run at all (no result
     file). Do NOT include acceptance_met in that case.
Do not modify anything under probes/ or verify/ in the automation checkout, and do not patch the
superset checkout; if a probe cannot run, report status "error". Do not open a PR.
"""
