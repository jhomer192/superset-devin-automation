# superset-devin-automation

An event-driven remediation and regression-verification loop for
[jhomer192/superset](https://github.com/jhomer192/superset), driven by the
[Devin v3 API](https://docs.devin.ai/v3-openapi.yaml).

Two Devin Automations exist. Both are thin shims: the session they start clones this repo
and runs `python -m orchestrator ...`. Every decision — which issue gets a session, whether a
merged PR gets verified, whether the fix worked — is made by code in this repository and is
covered by `tests/`.

| Name | Trigger | What the orchestrator does |
|------|---------|----------------------------|
| **MAP** — `sda-map-friday` | `schedule:recurring`, condition `{field:"rrule", operator:"recurrence", value:"FREQ=WEEKLY;BYDAY=FR"}` | `python -m orchestrator map`: list open issues labelled `ready`, triage each one, start **at most one** fix session per eligible issue. |
| **REDUCE** — `sda-reduce-merged-pr` | `github:pull_request` with `action == "closed"`, `pull_request.merged == true`, `repository.full_name == "jhomer192/superset"` | `python -m orchestrator reduce --event-json <path>`: start **one** verification session for the merged commit that clones Superset, stands up Postgres, builds the frontend, boots the app, and runs the committed probes at HEAD *and* at BASE. |

Both automations are created with `run_as: {"type": "organization"}` and a network policy
allowing `git-manager.devin.ai`. The exact payloads are built in
`orchestrator/automations.py` and validated against the OpenAPI components vendored in
`orchestrator/v3_schemas.json` before anything is sent.

## Run it

### Offline, no keys (`simulate`)

```bash
docker compose up            # builds the image and runs `python -m orchestrator simulate`
# or, without docker:
pip install -e . && python -m orchestrator simulate
```

`simulate` swaps the Devin and GitHub clients for in-memory fakes seeded from `fixtures/`
(a snapshot of the fork's issues and a merged-PR webhook body) and walks the whole loop:

1. **Friday 1, MAP** — 9 `ready` issues scanned; #12 and #15 (dependency refreshes) are
   deflected at zero ACU with a logged reason; 7 fix sessions start.
2. **Friday 1, MAP rerun** — nothing starts: every issue already has a live session.
3. **A fix session finishes** with structured output and PR #14 merges → **REDUCE** starts one
   verification session; replaying the same webhook is deduplicated on
   `(pr_url, merge_commit_sha)`.
4. **The verification session finishes** with a schema-valid structured output whose
   `acceptance_met` was decided by probe exit codes.
5. **Friday 2, MAP** — sessions still waiting for a human keep their slot; a dead session's
   issue is retried; the merged issue is gone from the `ready` list.
6. **Metrics** are computed over the fake sessions, and the two automation payloads are
   dry-run validated.

Every structured output the fakes emit is validated against the same schemas live sessions
get (`orchestrator/schema.py`), so the simulation cannot pass with a payload a real session
would be rejected for.

### Live

```bash
cp .env.example .env         # fill DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN, VERIFY_BRANCH, VERIFY_EVERY_N_MERGES
docker compose run --rm orchestrator register --dry-run   # print validated payloads
docker compose run --rm orchestrator register             # create/update both automations
docker compose run --rm orchestrator map                  # what Friday would do, right now
docker compose run --rm orchestrator reduce --event-json /events/pr.json
docker compose run --rm orchestrator metrics --days 30
```

Inside a Devin session the same values are available as the org secret
`superset_remediation_bot` (Devin API key, service user `superset-remediation-bot`) and the
personal secret `superset_github_key` (GitHub PAT for `jhomer192`). `register` is idempotent:
it updates the two automations by name rather than creating duplicates.

Secrets come from environment variables only; `.env.example` ships with empty values and
`.env` is git-ignored. `orchestrator/config.py` is the complete list of settings.

## How a fix session is decided (MAP)

For every open issue labelled `ready`, in order (`orchestrator/map_job.py`):

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

Progress is an append-only comment log on the issue (`orchestrator/ledger.py`,
`<!-- sda:{json} -->` markers) because GitHub has no conditional write for issue bodies.

## How a merged PR is verified (REDUCE)

`orchestrator/reduce_job.py` accepts only `closed` + `merged` events for the target repo
whose `pull_request.base.ref` is `VERIFY_BRANCH`, resolves the issues the merged PRs close
(`Closes #n` keywords) plus every issue whose fix already landed (regression guards), and starts
one session keyed `(pr_url, merge_commit_sha)`. The ledger comment on the PR makes a replayed
webhook a no-op.

### Cadence: every *n*th merge into a selected branch

The automation trigger fires on every merged PR — the Automations API has no counter and no
callback action — so the counter lives in code and is derived from GitHub itself rather than
from mutable state:

```
VERIFY_BRANCH=main            # only PRs merged into this branch count (default: master)
VERIFY_EVERY_N_MERGES=5       # verify when count % 5 == 0 (default: 1 = every merge)
```

On each event, `run_reduce` lists the PRs merged into `VERIFY_BRANCH` (`GET /pulls?state=closed&base=…`,
ordered by `merged_at`) and takes this PR's 1-based position *k*. If `k % n != 0` it appends a
`merge_counted` ledger comment ("merge k, verification deferred") to the PR and exits without a
session. If `k % n == 0` it verifies the window of the last *n* merges: HEAD is this PR's
`merge_commit_sha`, BASE is the first parent of the oldest merge in the window, and the probes
cover every issue closed anywhere in the window. A replayed webhook finds the `merge_counted` or
`verification_started` record and does nothing; PRs merged into other branches are skipped
before counting. `register` bakes both values into the REDUCE prompt and metadata, so changing
them is `VERIFY_BRANCH=… VERIFY_EVERY_N_MERGES=… orchestrator register`.

The session runs `verify/run_all.sh`, which on the Devin VM:

1. clones the target repo at HEAD and at BASE;
2. starts PostgreSQL 16 and Redis in docker;
3. installs backend requirements into a venv, `npm ci && npm run build` in `superset-frontend`;
4. `superset db upgrade`, creates an admin, `superset init`, boots gunicorn, waits for `/health`;
5. runs every selected probe at HEAD and at BASE (`verify/list_probes.py` picks them from the
   registry);
6. `verify/collect.py` turns exit codes into verify/out/result.json (git-ignored), valid against
   `VERIFICATION_SCHEMA`, and exits 0 iff acceptance held.

Acceptance is mechanical: a probe for an issue the PR closes must **pass at HEAD and fail at
BASE** — a probe that already passes before the fix proves nothing and fails the run. A
probe for an already-landed fix (regression guard) must pass at HEAD.

## Probes

Each probe is a committed script under `probes/`; its exit code is the verdict. `probes/run.sh
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

`orchestrator/schema.py` defines `FIX_SCHEMA` and `VERIFICATION_SCHEMA` (JSON Schema draft-07,
self-contained, no `$ref`, well under 64 KB, `additionalProperties: false` everywhere). The
taxonomy is machine-enforced with `if/then/else`: when `status == "error"` the payload must
carry `error_message` and must **not** carry `acceptance_met`; otherwise `acceptance_met`,
`probe_command`, `probe_exit_code` and `evidence` are all required. `tests/test_schema.py`
executes both schemas against passing and failing payloads.

## Observability

`python -m orchestrator metrics --days N` (`orchestrator/metrics.py`) queries
`GET /metrics/sessions` (`time_after`/`time_before`), `GET /sessions` filtered to
`origin=automation`, `GET /sessions/insights`, and
`GET /consumption/daily/sessions/{session_id}` for each automation session, then reports:

| number | meaning | limitation |
|--------|---------|------------|
| `org_metrics.sessions_created_count`, `sessions_with_merged_prs_count`, `avg_acus_per_session` | Devin's own aggregates for the window | organization-wide, not only this loop |
| `fix_sessions`, `verify_sessions` | automation sessions tagged `sda-fix` / `sda-verify` in the window | tags are set by this orchestrator only |
| `merge_rate` | fix sessions whose `pull_requests[]` has `pr_state == merged` ÷ fix sessions | a PR merged after the window closes is counted next run |
| `acu_per_merged_pr` | **all** automation ACUs (fix + verify + errored) ÷ merged PRs | cost of outcome, not cost per PR |
| `verification_pass_rate` | verification sessions with `acceptance_met == true` ÷ verification sessions with a non-error output | sessions still running are excluded |
| `triage_deflections` | `triage_deflected` ledger comments on the target repo's issues | needs `GITHUB_TOKEN`; 0 without it |
| `liveness` | count per liveness bucket (live / awaiting_human / dead / terminal_suspended / unknown_suspended) | point-in-time |

The report also carries a `limitations` list so a reader never has to infer them.

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

* **Cadence.** Fix sessions start on Fridays; verification runs on every *n*th merge into
  `VERIFY_BRANCH` (see above). The counter is not held by the automation — it cannot be, there is
  no callback action — but computed from the branch's merged-PR history on each event.
* **Regression guards.** REDUCE runs the probes for already-landed fixes too, not only the PR's
  own issue — that is what "no regressions since last time" actually requires.
* **Issue #15** duplicates #12 (same lockfile defect, closed by PR 17). Both are deflected by
  MAP; the registry maps #15 to the same probe so the merge of PR 17 is verified.
