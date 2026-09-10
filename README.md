# superset-devin-automation

An event-driven remediation and regression-verification loop for
[jhomer192/superset](https://github.com/jhomer192/superset), driven by the
[Devin v3 API](https://docs.devin.ai/v3-openapi.yaml).

Two Devin Automations form one chain. All are thin shims: the session they start clones this
repo and runs `python -m orchestrator ...`. Every decision — which issue gets a session, whether a
merged PR gets verified, whether the fix worked — is made by code in this repository and is
covered by `tests/`.

```
PR merges into master ──► find-and-fix (one invocation, one daemon-style pass)
   1. TESTING: every merge (VERIFY_EVERY_N_MERGES=1; set 5 for every 5th), one verification session on a Devin VM
      (clone Superset at HEAD, Postgres + Redis, build, boot, probes),
      wait for the verdict, comment it on every PR of the window
   2. acceptance_met == false ──► regression issues labelled `sda-regression`
   3. every open `sda-regression` issue opened in the last REGRESSION_ISSUE_WINDOW_HOURS
      (default 24) that has no fix in flight ──► one fix session each, on its own VM
      in parallel: EXPLORE, one exploratory session on the same HEAD ──► reproduced, previously
      unknown defects filed as `sda-candidate` issues (see "Finding new issues")
   4. wait for all of them (zero is fine) ──► verdict, PR and ACUs on each issue
   5. one find-and-fix report on the `sda-status` issue ──► fix PRs merge ──► back to 1
```

| Name | Trigger | What the orchestrator does |
|------|---------|----------------------------|
| **find-and-fix** — `superset issue finder and fixer` | `github:pull_request` with `action == "closed"`, `pull_request.merged == true`, `repository.full_name == "jhomer192/superset"` | `python -m orchestrator find-and-fix --event-json <path>`: steps 1–5 above (`orchestrator/find_and_fix.py`, wrapping `testing_job.run_testing`, `regression.start_regression_fix` and `autopr_job.publish_fixes`). Issues without TESTING's `regression_depth` record, issues an open PR already closes, and issues whose fix session is still running are skipped and listed in the report. |

find-and-fix is the only automation; there is no schedule. `python -m orchestrator autopr --wait` (triage every open
`ready` issue, one fix session each, wait, post each verdict/PR/ACUs) remains a manual command for the
human-filed backlog.

There is no hand-off between automations and no custom webhook receiver: the merged-PR event
starts one find-and-fix invocation, and that invocation owns verification, fan-out, waiting and the
report. Each stage publishes its own outcome before it exits, so there is no sweeper and no
digest job. `register` deletes automations still registered under retired names (MAP, REDUCE,
REPORT, TESTING and both AUTOPR variants).

The automations are created with `run_as: {"type": "organization"}` and a network policy
allowing `git-manager.devin.ai`, `github.com`, `api.github.com`, `api.devin.ai` and `pypi.org`. The exact payloads are built in
`orchestrator/automations.py` and validated against the OpenAPI components vendored in
`orchestrator/v3_schemas.json` before anything is sent.

The sessions those shims start reference two org Playbooks, which carry the part of each
prompt that is the same on every run:

| Playbook | Body (`orchestrator/prompts.py`) | Attached schema | Used by |
|----------|----------------------------------|-----------------|---------|
| `superset-devin-automation: remediation` | `FIX_PLAYBOOK_BODY`: clone at master, branch, venv from `requirements/development.txt`, run every deciding probe and require non-zero at base, minimal fix following the fork's `AGENTS.md`, re-run probes and require 0, PR body with `Closes #NN`, Conventional Commits title, no AI attribution | `FIX_SCHEMA` | fix sessions started by AUTOPR's Friday sweep |
| `superset-devin-automation: verification` | `VERIFY_PLAYBOOK_BODY`: clone this repo, run `verify/run_all.sh` with the given repo/head/requirements, copy the resulting verify/out/result.json into the structured output verbatim, never modify `probes/` or `verify/`, no PR | `VERIFICATION_SCHEMA` | verification sessions started by TESTING |

The per-session prompt is then only the variables: the `@owner/repo` token, the
`@playbook:{id}` token, and the issue number/title/condition/URL and probe list (fix) or the
HEAD SHA, PR URL, PRD requirement ids and probe list (verification). `playbook_id` on a
session is read-only; the API derives it from the token, exactly as `repos` is derived from
`@owner/repo`. If `PLAYBOOK_ID_FIX` or `PLAYBOOK_ID_VERIFY` is unset, the same body is inlined
into the prompt and a warning is logged; a missing playbook never stops TESTING or AUTOPR.

The regression fix prompt (`regression_fix_prompt` in `orchestrator/prompts.py`) stays fully
inline and carries no playbook token: its workflow differs from `FIX_PLAYBOOK_BODY` (reproduce
the failure first, add a regression test, re-run the surrounding unit-test directory), so the
remediation playbook would contradict it.

## Run it

### Offline, no keys (`simulate`)

```bash
docker compose up            # builds the image and runs `python -m orchestrator simulate`
# or, without docker:
pip install -e . && python -m orchestrator simulate
```

`simulate` swaps the Devin and GitHub clients for in-memory fakes seeded from `fixtures/`
(a snapshot of the fork's issues and a merged-PR webhook body) and walks the whole loop:

1. **Friday 1, AUTOPR sweep** — 9 `ready` issues scanned; #12 and #15 (dependency refreshes)
   are deflected at zero ACU with a logged reason; 7 fix sessions start.
   AUTOPR waits for all seven: #5's lands PR #14 and its verdict, PR and ACUs are posted on #5;
   the other six error out and get their error comment.
2. **Friday 1, sweep rerun** — the errored issues are retried; #5 is closed and starts nothing.
3. **PR #14 merges** as the 5th merge →
   **TESTING** starts one verification session, waits for it (the fake finishes while TESTING
   sleeps), and posts the verdict, probe exit codes and ACUs onto all five PRs of the window;
   replaying the same webhook is deduplicated on `(pr_url, merge_commit_sha)`.
4. **A later 5th merge fails verification** → TESTING files one issue labelled `sda-regression`
   with the window, SHAs and failing probes. The fake `github:issues` event for that issue runs
   **AUTOPR**, which starts exactly one fix session, waits for it, and posts the fix PR it opened
   onto the issue; replaying the event finds that open PR and starts nothing, and an issue a human
   created with the same label starts nothing (no `regression_depth` record).
5. **Friday 2, sweep** — errored issues are retried; the merged issue is gone from the `ready`
   list.
6. The automation payloads are dry-run validated.
7. **Playbooks** are registered into the fake org twice (same two ids both times), and one more
   sweep with those ids shows every fix prompt carrying the `@playbook:` token; the earlier steps
   ran with the ids unset, exercising the inline fallback.

Every structured output the fakes emit is validated against the same schemas live sessions
get (`orchestrator/schema.py`), so the simulation cannot pass with a payload a real session
would be rejected for.

### Live

```bash
cp .env.example .env         # fill DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN, VERIFY_BRANCH, VERIFY_EVERY_N_MERGES
docker compose run --rm orchestrator register-playbooks --dry-run   # print validated playbook payloads
docker compose run --rm orchestrator register-playbooks   # create/update both playbooks; prints PLAYBOOK_ID_*
# put the printed PLAYBOOK_ID_FIX / PLAYBOOK_ID_VERIFY in .env, then:
docker compose run --rm orchestrator register --dry-run   # print validated automation payloads
docker compose run --rm orchestrator register             # create/update both automations (shims carry the ids)
docker compose run --rm orchestrator autopr               # what Friday would do, right now
docker compose run --rm orchestrator autopr --wait --event-json /events/issue.json   # one github:issues event
docker compose run --rm orchestrator testing --wait --event-json /events/pr.json
```

Inside a Devin session the same values are available as the org secret
`superset_remediation_bot` (Devin API key, service user `superset-remediation-bot`) and the
secret `superset_github` (GitHub PAT for `jhomer192`). Sessions the automations start read
those two names, so both must have org access, and the Devin GitHub app must be installed on the
target repository (or `github:pull_request` never fires) and on this one (or the shim's clone is
refused). `register` and
`register-playbooks` are idempotent: they update by automation name / playbook title
(`PUT`) rather than creating duplicates.

Secrets come from environment variables only; `.env.example` ships with empty values and
`.env` is git-ignored. `orchestrator/config.py` is the complete list of settings.

## How a fix session is decided (AUTOPR)

On a `github:issues` event (`run_autopr_for_issue` in `orchestrator/autopr_job.py`): the event
must be `labeled`/`opened` with the `sda-regression` label for the target repo; the issue's
ledger must carry the `regression_depth` entry TESTING wrote when it filed it (otherwise the
issue was labelled by hand and nothing starts); an open PR closing it or a live `issue-<n>`
session means skip; else one fix session starts from the recorded PR, HEAD and probes
(`start_regression_fix` in `orchestrator/regression.py`).

On the Friday sweep, for every open issue labelled `ready`, in order:

1. **Already in flight?** An open PR whose body closes the issue, or a Devin session tagged
   `issue-<n>` that still holds its slot, means skip. Liveness follows the v3 status enum
   exactly (`orchestrator/sessions.py`): `new`/`claimed`/`running`/`resuming` are live;
   `exit`/`error` are dead; `suspended` splits on `status_detail` — `waiting_for_user`,
   `waiting_for_approval`, `inactivity` are live and awaiting a human, the usage/credit/quota
   details are terminal, and any unknown detail is treated as still live so a Friday never
   opens a duplicate.
2. **Triage** (`orchestrator/triage.py`). Zero-ACU deflection, with the reason written to the
   issue, for: dependency refreshes (Dependabot is disabled on this fork — security updates
   off, vulnerability alerts 404 — so these are **deferred to a human**, not delegated to a
   bot), test-only requests, issues without an `### Acceptance criteria` section, and issues
   with no committed probe in `probes/registry.json`.
3. **Start one session** with `structured_output_schema = FIX_SCHEMA`,
   `structured_output_required = true`, tags `sda-fix`, `issue-<n>`, and a prompt
   (`orchestrator/prompts.py`) that names the probe the session must make pass, requires it to
   fail at BASE first, and forbids editing the probe. No `max_acu_limit`, no timeout, no turn
   cap anywhere; `tests/test_orchestrator.py` asserts their absence.

With `--wait` (the registered shim always passes it) the command then polls each session it
started every 60 s until it reaches a terminal state and posts one `session_reported` comment on
the issue: verdict (`acceptance_met`, or the `error_message`), the PR URL, ACUs from
`GET /consumption/daily/sessions/{id}` and the session URL. A session parked on a human
(`suspended`/`waiting_for_user`) is waited on, not abandoned.

Progress is an append-only comment log on the issue (`orchestrator/ledger.py`,
`<!-- sda:{json} -->` markers) because GitHub has no conditional write for issue bodies.

## How a merged PR is verified (TESTING)

`orchestrator/testing_job.py` accepts only `closed` + `merged` events for the target repo
whose `pull_request.base.ref` is `VERIFY_BRANCH`, selects the probe of every requirement in
`PRD.md` (`probes/registry.json`, `prd.requirements`), and starts one session keyed
`(pr_url, merge_commit_sha)`. Closed issues and `Closes #n` keywords play no part in selection.
The ledger comment on the PR makes a replayed webhook a no-op.

### Cadence: every *n*th merge into a selected branch

The automation trigger fires on every merged PR — the Automations API has no counter and no
callback action — so the counter lives in code and is derived from GitHub itself rather than
from mutable state:

```
VERIFY_BRANCH=main            # only PRs merged into this branch count (default: master)
VERIFY_EVERY_N_MERGES=5       # verify when count % n == 0 (default: 5)
```

On each event, `run_testing` lists the PRs merged into `VERIFY_BRANCH` (`GET /pulls?state=closed&base=…`,
ordered by `merged_at`) and takes this PR's 1-based position *k*. If `k % n != 0` it appends a
`merge_counted` ledger comment ("merge k, verification deferred") to the PR and exits without a
session. If `k % n == 0` it verifies HEAD = this PR's `merge_commit_sha`; the window of the
last *n* merges is recorded so a failure names every PR that could have caused it. A replayed webhook finds the `merge_counted` or
`verification_started` record and does nothing; PRs merged into other branches are skipped
before counting. `register` bakes both values into the TESTING prompt and metadata, so changing
them is `VERIFY_BRANCH=… VERIFY_EVERY_N_MERGES=… orchestrator register`.

The session runs `verify/run_all.sh`, which on the Devin VM:

1. clones the target repo at HEAD;
2. starts PostgreSQL 16 and Redis in docker;
3. installs backend requirements into a venv, `npm ci && npm run build` in `superset-frontend`;
4. `superset db upgrade`, creates an admin, `superset init`, boots gunicorn, waits for `/health`;
5. runs every PRD requirement's probe against that app (`verify/list_probes.py` picks them
   from the registry);
6. `verify/collect.py` turns exit codes into verify/out/result.json (git-ignored), valid against
   `VERIFICATION_SCHEMA`, and exits 0 iff acceptance held.

Acceptance is mechanical: every selected probe must exit 0 at HEAD. There is no BASE checkout
and no comparison with the state before the merge.

### The spec: PRD.md

The target repo carries `PRD.md`, a product requirements document with stable ids
(`PRD-SEC-1`, `PRD-AUTH-1`, ...). The `prd.requirements` block of `probes/registry.json` maps
each id to the probes that hold it, either an issue's probe or a standalone one under
`probes/prd/` (listed in the registry's top-level `probes`). Every verification passes all
requirement ids as `--requirements`; each of their probes must pass at HEAD, and a failure files
a regression issue whose title and table name the violated requirement. Adding coverage means
adding a requirement to `PRD.md` and a probe for it to the registry.

With `--wait` (the registered shim always passes it) the command then polls
`GET /sessions/{id}` every 60 s until the verification reaches a terminal state, with no
deadline, and publishes through `orchestrator/publish.py`: a `session_reported` comment with the
verdict, per-probe HEAD exit codes, ACUs and session URL on every PR of the window, and on
`acceptance_met == false` the regression issue (see Self-healing). Every comment is keyed by
session id in its marker, so a replayed event posts nothing twice.

## Finding new issues (EXPLORE)

Verification only checks what `probes/registry.json` already names. After each verification that
reached a verdict (pass or fail; not a build error), find-and-fix starts one exploratory session
(`orchestrator/explore.py`, prompt in `prompts.exploration_prompt`, tagged `sda-explore`) on the
same merge commit, alongside the fix sessions. That session boots Superset, walks the app as each
role in Superset's `SECURITY.md`, reads the server log, and reports candidates under
`EXPLORE_SCHEMA`. A candidate has to carry a reproduction on that checkout (request, expected,
actual, evidence) and a probe script in the `probes/` shape that the session ran and saw exit
non-zero; a security candidate also names the `SECURITY.md` matrix row and the attacker role. The
session files nothing itself.

The orchestrator files each candidate as an issue labelled `sda-candidate` plus its category, with
the reproduction, the proposed probe and promotion steps in the body and a `candidate_filed` ledger
comment carrying the candidate's `fingerprint` (a stable slug for the defect). A later exploration
that reports a fingerprint an open `sda-candidate` issue already carries adds a `candidate_seen`
comment there instead of filing; a closed candidate does not count, so the same fingerprint after
closure files again. One exploration per verified merge: a replayed event finds the
`exploration_started` record on the PR and reuses that session.

A candidate is not a regression. `fix_recent_regressions` reads `sda-regression` only, so nothing
fixes or guards a candidate until a human promotes it: save the probe under `probes/issue_<n>/`,
add the issue and probe to `probes/registry.json`, regenerate `ISSUES.md`, merge. From then on
`autopr` can start its fix session and every verification guards the probe. `EXPLORE_AFTER_VERIFY=0`
turns the tier off; like the cadence values it is baked in at `register`.

## Probes

Each probe is a committed script under `probes/`; only its exit code is read. `probes/run.sh
<probe-id>` runs one against `SUPERSET_SRC`. Kinds (`orchestrator/registry.py`):

| kind | lane | example |
|------|------|---------|
| `offline_pytest` | pytest, unit lane (sqlite) | `probes/issue_5/probe_unit.sh` |
| `offline_static` | lockfile / manifest assertions, reads `package-lock.json` directly, does not trust `npm audit` | `probes/issue_12/probe_lockfile.sh`, `probes/lockfile_check.py` |
| `log_assertion` | startup log content | `probes/issue_1/probe_startup_log.sh` |
| `integration` | Superset's real integration harness (`scripts/python_tests.sh`, `tests.integration_tests.superset_test_config`) against Postgres | `probes/issue_10/probe_integration.sh` |
| `live_http` | HTTP against the booted app | `probes/issue_9/probe_live_theme_api.sh` |

`ISSUES.md` lists every issue on the fork with its category, the condition cited to file and
line, its deciding probe, and its closing PR or not-planned reason. It is generated from
`probes/registry.json` by `verify/gen_issues_md.py`; `tests/test_docs.py` fails if it drifts.

## Structured output

`orchestrator/schema.py` defines `FIX_SCHEMA`, `VERIFICATION_SCHEMA` and `EXPLORE_SCHEMA` (JSON Schema draft-07,
self-contained, no `$ref`, well under 64 KB, `additionalProperties: false` everywhere). The
taxonomy is machine-enforced with `if/then/else`: when `status == "error"` the payload must
carry `error_message` and must **not** carry `acceptance_met`; otherwise `acceptance_met`,
`probe_command`, `probe_exit_code` and `evidence` are all required. `tests/test_schema.py`
executes both schemas against passing and failing payloads.

## API surface

Every v3 endpoint this loop calls, all under `/v3/organizations/{org_id}` (`orchestrator/devin_api.py`):

| Endpoint | Used for |
|----------|----------|
| `POST /sessions`, `GET /sessions/{id}` | start fix/verification sessions, wait for them, dedup on live sessions |
| `GET /automations`, `POST /automations`, `PATCH /automations/{id}`, `DELETE /automations/{id}` | `register` (idempotent by name; deletes the retired MAP/REDUCE registrations) |
| `GET /playbooks`, `POST /playbooks`, `PUT /playbooks/{id}` | `register-playbooks` (idempotent by title); payload is `PlaybookCreateRequest` |
| `GET /consumption/daily/sessions/{id}` | ACUs on every verdict comment |

## Observability

Each stage writes its record where the work is, in the same run that did the work, so an
engineering leader reads the PR or issue and sees what happened to it:

| stage | where | ledger entries (`<!-- sda:{json} -->`) and what they answer |
|-------|-------|-------------------------------------------------------------|
| TESTING | every PR merged into `VERIFY_BRANCH` | `merge_counted` (position k of n: did the merge count?); `verification_started` (session id, HEAD, requirements, window: what is being verified?); `session_reported` (verdict, per-probe HEAD exit codes, ACUs, session URL: did it pass, what did it cost?); `regression_filed` / `regression_escalated` (issue number, depth: what happened to a failure?) |
| AUTOPR | the regression issue / each `ready` issue | `regression_depth` (chain depth, PR, window, probes: why does this issue exist?); `session_started` with `trigger: github:issues` or `sweep` (which session, started by what?); `session_reported` (verdict, PR URL, ACUs, session URL: did the fix land, what did it cost?); `triage_deflected` (why nothing started) |

The last step of a run that waited for sessions is the report (`orchestrator/status.py`): one
comment on the `sda-status` issue in the target repository, created on first use. A TESTING
comment carries merge position, window, HEAD, verdict, per-probe exit codes, ACUs and the
regression issue if one was filed; an AUTOPR comment carries the trigger and, per fix session,
issue, verdict, session, PR URL and ACUs, then the run's total. Reading that issue top to bottom
is the run log of the loop, and a replayed run appends nothing (`run_reported` marker).

Reading across threads: the count of `session_reported` fix comments whose PR merged over the
`session_started` comments is the remediation success rate; `merge_counted` positions are the
merge throughput; `verification_started` to `session_reported` on the same PR is verification
latency; `regression_filed` without a later merged fix PR is the open regression backlog.

The command's own JSON output (`TestingReport`, `AutoprReport`) is what the automation session
prints, so the invocation list on the automation page shows the same decisions: `merge_index`,
`skipped_reason`, `verdict`, `posted_to`, `regression_filed`, `started`, `skipped_in_flight`,
`finished` (session id, status, verdict, PR URL, ACUs per fix session). Sessions are tagged
`sda-verify` / `sda-fix` / `sda-explore` / `sda-regression` / `issue-<n>`, so the sessions list filtered by tag is
the live view of what is running.

Waiting inside the run is the mechanism because automations have no completion callback — the
same constraint that makes the TESTING cadence counter derived rather than stored. A TESTING or
AUTOPR session therefore lives as long as the session it waits on.

## Self-healing

A verification whose structured output says `acceptance_met == false` has found a probe that
fails on a commit already on `master`. TESTING files that verdict as work
(`file_regression_issue` in `orchestrator/regression.py`): one issue on the target repo labelled
`sda-regression`/`regression` naming the PR whose merge triggered the verification, listing every
PR in the verified window (any of them could be the cause), the HEAD SHA, the probe
table with requirement ids and exit codes and the captured evidence, plus a `regression_depth` ledger entry with
the PR, window and probes. If an open `sda-regression` issue's `regression_depth` record already
names exactly the same probe set, the run adopts that issue (a `regression_filed` entry with
`adopted: true` on the PR and a comment on the issue) instead of filing another; a different or
larger probe set, or a closed issue, still files. The `labeled` event for `sda-regression` runs
AUTOPR, which reads that entry and starts one fix session tagged `sda-fix`/`sda-regression`. The
verdict comment still lands on every PR thread in the window; only the remediation is once per
failed verification, whatever the cadence. That session must reproduce the failure before
touching anything, make the smallest fix, add a test, and run the probes and the surrounding unit
tests before opening its PR — and when that PR merges, TESTING verifies it like any other. The
probes are the contract in both directions: the issue states they must not be edited or relaxed.

Two things keep the loop finite. A `regression_filed` marker keyed by the verification session,
found on any PR thread in the window, means the failure already has an issue, so a replayed
TESTING run does not file it twice, and a replayed issue event finds the live `issue-<n>`
session or the open fix PR and starts nothing. And each filed issue records its
`regression_depth`; a PR that closes a regression issue inherits it, so after `MAX_CHAIN_DEPTH`
failed automated attempts on the same chain the run writes `regression_escalated` on the PR and
stops instead of spending ACUs in a find-and-fix.

## Development

```bash
pip install -e .[dev]
ruff check . && ruff format --check . && mypy && pytest
python -m orchestrator simulate
python verify/gen_issues_md.py --check
```

CI (`.github/workflows/ci.yml`) runs the same plus `docker compose build` and a containerised
`simulate`.

## Deviations from the brief

* **Cadence.** Nothing runs on a clock. Verification runs on every merge into `VERIFY_BRANCH`
  by default, or every *n*th with `VERIFY_EVERY_N_MERGES=n`. The counter is not held by the automation — it cannot be, there is
  no callback action — but computed from the branch's merged-PR history on each event.
* **Spec.** `PRD.md` is the only thing verified. `ISSUES.md` documents the probes and the issues
  they came from; an issue's probe runs only if a PRD requirement maps to it.
* **Issue #15** duplicates #12 (same lockfile defect, closed by PR 17). Both are deflected by
  the AUTOPR sweep; the registry maps #15 to the same probe so the merge of PR 17 is verified.
