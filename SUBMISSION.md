# Submission

## Repositories

| | Link |
|---|---|
| Automation (this repo, Docker) | https://github.com/jhomer192/superset-devin-automation |
| Apache Superset fork | https://github.com/jhomer192/superset |
| Spec the verifier checks | https://github.com/jhomer192/superset/blob/master/PRD.md |
| Run log (every verification and fix run, with verdicts, session links and ACUs) | https://github.com/jhomer192/superset/issues/25 |
| Loom walkthrough | https://www.loom.com/share/7927552814f148d4a27f82e35c766117 |

`README.md` covers how to run or simulate the workflow (`docker compose up` runs the full loop offline with no keys).

## Issues filed and remediated by Devin

Filed by hand on the fork; each was fixed by a Devin session that opened the linked PR. The probe column is the check the verifier runs against every merge to keep the fix in place, and the PRD id is the requirement it maps to.

| Issue | Class | Fix PR | Probe / PRD id |
|---|---|---|---|
| [#1](https://github.com/jhomer192/superset/issues/1) `SameSite=None requires Secure=True` cookie invariant not enforced at startup | security | [#13](https://github.com/jhomer192/superset/pull/13) | `issue_1/unit`, `issue_1/startup_log` / PRD-SEC-1 |
| [#9](https://github.com/jhomer192/superset/issues/9) `sanitize_svg_content` regex denylist is non-idempotent and reassembles the `javascript:` payload it removes | security | [#18](https://github.com/jhomer192/superset/pull/18), [#42](https://github.com/jhomer192/superset/pull/42) | `issue_9/unit` / PRD-SEC-3 |
| [#15](https://github.com/jhomer192/superset/issues/15) nx pins brace-expansion 5.0.8 (GHSA-rgw5-rvv9-x895); minimatch override has the wrong parent | dependency | [#17](https://github.com/jhomer192/superset/pull/17) | `issue_12/lockfile` / PRD-DEP-1 |
| [#3](https://github.com/jhomer192/superset/issues/3) streaming export chunk size hard-coded in two places | code quality | [#16](https://github.com/jhomer192/superset/pull/16) | `issue_3/unit` / PRD-SQL-2 |
| [#5](https://github.com/jhomer192/superset/issues/5) KQL splitter drops all text before a triple-backtick string | correctness | [#14](https://github.com/jhomer192/superset/pull/14) | `issue_5/unit` / PRD-SQL-1 |
| [#7](https://github.com/jhomer192/superset/issues/7) custom-tags substring rewrite of the rison `q` corrupts filter values | correctness | [#42](https://github.com/jhomer192/superset/pull/42) | `issue_7/unit` / PRD-CHART-2 |
| [#11](https://github.com/jhomer192/superset/issues/11) `versioning/diff.py` drops and fabricates chart-history records when two adhoc filters share a column | correctness | [#42](https://github.com/jhomer192/superset/pull/42) | `issue_11/unit` / PRD-CHART-1 |
| [#10](https://github.com/jhomer192/superset/issues/10) machine-auth priming page never closed; leaked page can clobber the injected session cookie | correctness | [#19](https://github.com/jhomer192/superset/pull/19) | `issue_10/integration` (no PRD requirement) |

Closed as not planned after triage: [#2](https://github.com/jhomer192/superset/issues/2), [#4](https://github.com/jhomer192/superset/issues/4), [#6](https://github.com/jhomer192/superset/issues/6), [#8](https://github.com/jhomer192/superset/issues/8), [#12](https://github.com/jhomer192/superset/issues/12) (duplicate of #15).

## Regressions the automation caught on its own

Each row is a merged PR, the PRD violation the verification session found on the booted app, the issue the orchestrator filed, and the fix PR a remediation session opened.

| Merged PR | Violation | Issue filed | Fix PR | Sessions |
|---|---|---|---|---|
| [#23](https://github.com/jhomer192/superset/pull/23) "redundant buffer flush" refactor | `issue_5/unit` (PRD-SQL-1) | [#24](https://github.com/jhomer192/superset/issues/24) | [#26](https://github.com/jhomer192/superset/pull/26) | [fix](https://app.devin.ai/sessions/eba07b774a3a43cf8aa7992aaed96a12) |
| [#27](https://github.com/jhomer192/superset/pull/27) `samesite == "none"` comparison | `issue_1/unit` (PRD-SEC-1) | [#28](https://github.com/jhomer192/superset/issues/28) | [#30](https://github.com/jhomer192/superset/pull/30) | [fix](https://app.devin.ai/sessions/246578446fc3457a85d61b3b9587a5c2) |
| [#33](https://github.com/jhomer192/superset/pull/33) PRD added; three requirements not yet met on master | PRD-CHART-1, PRD-CHART-2, PRD-SEC-3 | [#38](https://github.com/jhomer192/superset/issues/38) | [#42](https://github.com/jhomer192/superset/pull/42) | [verify](https://app.devin.ai/sessions/ed0698e249d048df9a122501f1d37e5d), [fix](https://app.devin.ai/sessions/fcc271be30f54d91938cb56c00a81e26) |
| [#37](https://github.com/jhomer192/superset/pull/37) `@protect()` dropped from `GET /api/v1/chart/` | PRD-SEC-2 (+ the three above) | [#41](https://github.com/jhomer192/superset/issues/41) | [#42](https://github.com/jhomer192/superset/pull/42) | [fix](https://app.devin.ai/sessions/61de5e84251a4562ad1a0edb5565ea8e) |

PRs #23, #27 and #37 were merged deliberately to exercise the failure path; #27 and #37 pass code review and the unit suite that CI would run.
