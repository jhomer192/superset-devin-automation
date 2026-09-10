"""Build and register the one automation (find-and-fix) through the Automations API.

find-and-fix is the daemon: a PR merges -> verify (every merge by default; VERIFY_EVERY_N_MERGES=5 for
every 5th), wait for the verdict, post it on every PR of the window, file an `sda-regression`
issue on failure -> start one fix session per `sda-regression` issue opened in the last N hours ->
wait for all of them (none is fine) -> append the find-and-fix report to the status issue -> the fix PRs
merge and re-enter the loop. Each stage publishes its own telemetry; there is no sweeper and no
schedule. Automations under retired names (MAP, REDUCE, REPORT, TESTING, AUTOPR) are deleted on
register.

Every payload is validated locally against the request schemas vendored from
https://docs.devin.ai/v3-openapi.yaml (orchestrator/v3_schemas.json) before anything is sent.

Design constraints honoured here (see AutomationCreateRequest in the spec):
* at most one start_session action, no monitor_session (deprecated for new automations)
* run_as = {"type": "organization"} on all of them
* no limits.max_acu_limit, no concurrency caps, no timeouts
* net_policy allows the git proxy, GitHub, the Devin API and PyPI; the shim needs nothing else
* prompts are thin shims: clone this repo, run `python -m orchestrator <find-and-fix|autopr> --wait`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .devin_api import DevinClient
from .regression import REGRESSION_LABEL

SCHEMAS_PATH = Path(__file__).with_name("v3_schemas.json")
FINDER_NAME = "superset issue finder and fixer"
RETIRED_NAMES = (
    "superset-devin-automation: AUTOPR (Friday ready-issue sweep)",
    "superset-devin-automation: CYCLE (verify every 5th merge, fix regressions, report)",
    "superset-devin-automation: AUTOPR (fix session per regression issue + Friday sweep)",
    "superset-devin-automation: TESTING (verify every 5th merge, file regressions)",
    "superset-devin-automation: MAP (Friday ready-issue sweep)",
    "superset-devin-automation: REDUCE (verify merged PR)",
    "superset-devin-automation: REPORT (publish session outcomes)",
)
NET_POLICY = {
    "allow": [
        {"hostname": h}
        for h in (
            "git-manager.devin.ai",
            "github.com",
            "api.github.com",
            "api.devin.ai",
            "pypi.org",
            "files.pythonhosted.org",
        )
    ]
}


def _shim(automation_repo: str, command: str) -> str:
    return f"""@{automation_repo}

You are the thin dispatch shim for the superset-devin-automation loop. Do not reason about the
issues yourself; the orchestrator does that.

1. Credentials: the org session secrets `superset_remediation_bot` (Devin API key) and
   `superset_github` (GitHub token) are already environment variables; the orchestrator reads them
   by those names (either case) and has the org id built in. Do not look for other names; if one of
   the two is missing, run the command anyway and report its error verbatim.
2. Clone with the token, never through the git proxy:
   git clone "https://x-access-token:${{superset_github}}@github.com/{automation_repo}"
   cd superset-devin-automation
3. python -m pip install -e .
4. Run: {command}
5. Report the command's JSON output verbatim and exit. Do not open PRs, do not edit code, and do
   not print the token.
"""


def _env_prefix(name: str, playbook_id: str | None) -> str:
    return f"{name}={playbook_id} " if playbook_id else ""


def finder_payload(
    target_repo: str,
    automation_repo: str,
    verify_branch: str = "master",
    every_n: int = 1,
    issue_window_hours: int = 24,
    playbook_id_verify: str | None = None,
    playbook_id_fix: str | None = None,
) -> dict[str, Any]:
    prompt = (
        _shim(
            automation_repo,
            f"VERIFY_BRANCH={verify_branch} VERIFY_EVERY_N_MERGES={every_n} "
            f"REGRESSION_ISSUE_WINDOW_HOURS={issue_window_hours} "
            + _env_prefix("PLAYBOOK_ID_VERIFY", playbook_id_verify)
            + _env_prefix("PLAYBOOK_ID_FIX", playbook_id_fix)
            + "python -m orchestrator find-and-fix --event-json event.json  "
            "(first write the appended pull_request event payload to event.json, unmodified)",
        )
        + f"\nTarget repository: @{target_repo}\n"
        + f"Cadence: the orchestrator only starts a verification session for every {every_n}th PR "
        f"merged into `{verify_branch}`; on other merges it records the count and exits. Otherwise it "
        "blocks until that verification finishes, posts the verdict on every PR of the window, files a "
        f"`{REGRESSION_LABEL}` issue if it failed, starts one fix session per `{REGRESSION_LABEL}` issue "
        f"opened in the last {issue_window_hours}h, waits for all of them and appends the run report to "
        "the status issue. It can run for hours. Do not interrupt it.\n"
    )
    return {
        "name": FINDER_NAME,
        "enabled": True,
        "run_as": {"type": "organization"},
        "metadata": {
            "component": "find-and-fix",
            "target_repo": target_repo,
            "verify_branch": verify_branch,
            "verify_every_n_merges": str(every_n),
            "issue_window_hours": str(issue_window_hours),
            "playbook_id_verify": playbook_id_verify or "",
            "playbook_id_fix": playbook_id_fix or "",
        },
        "triggers": [
            {
                "event_type": "github:pull_request",
                "conditions": {
                    "any": [
                        {
                            "all": [
                                {"field": "action", "operator": "eq", "value": "closed"},
                                {"field": "pull_request.merged", "operator": "eq", "value": True},
                                {"field": "repository.full_name", "operator": "eq", "value": target_repo},
                            ]
                        }
                    ]
                },
            }
        ],
        "actions": [
            {
                "type": "start_session",
                "prompt": prompt,
                "session": {"tags": ["sda-find-and-fix"]},
            }
        ],
        "session_settings": {"net_policy": NET_POLICY},
    }


def _validator(schema_name: str) -> Draft202012Validator:
    """OpenAPI components use ``#/components/schemas/X`` refs, so validate through a root
    document that carries the vendored components and points at the requested schema."""
    bundle = json.loads(SCHEMAS_PATH.read_text())
    root = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/components/schemas/{schema_name}",
        "components": {"schemas": bundle["schemas"]},
    }
    return Draft202012Validator(root)


def validate_payload(payload: dict[str, Any], schema_name: str = "AutomationCreateRequest") -> list[str]:
    validator = _validator(schema_name)
    return sorted(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(payload)
    )


def assert_no_ceilings(payload: dict[str, Any]) -> None:
    """Refuse to ship a spend ceiling, turn cap or timeout, whatever the template said."""
    flat = json.dumps(payload)
    for forbidden in ("max_acu_limit", "timeout", "max_turns", "turn_cap"):
        if forbidden in flat:
            raise ValueError(f"forbidden ceiling {forbidden!r} present in payload")
    actions = payload.get("actions", [])
    if any(a.get("type") == "monitor_session" for a in actions):
        raise ValueError("monitor_session is deprecated for new automations")
    if sum(1 for a in actions if a.get("type") == "start_session") > 1:
        raise ValueError("at most one start_session action")


def register(
    devin: DevinClient,
    target_repo: str,
    automation_repo: str,
    *,
    verify_branch: str = "master",
    every_n: int = 1,
    issue_window_hours: int = 24,
    playbook_id_fix: str | None = None,
    playbook_id_verify: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Create the automations, or update them in place if ones with the same name exist.

    The ones registered under retired names are deleted last, once every replacement is in place,
    so a failure part-way leaves the old chain running rather than nothing.
    """
    results: list[dict[str, Any]] = []
    existing = {a.get("name"): a for a in devin.list_automations()} if not dry_run else {}
    payloads = (
        finder_payload(
            target_repo,
            automation_repo,
            verify_branch,
            every_n,
            issue_window_hours,
            playbook_id_verify,
            playbook_id_fix,
        ),
    )
    for payload in payloads:
        errors = validate_payload(payload)
        if errors:
            raise ValueError(f"{payload['name']}: invalid payload:\n  " + "\n  ".join(errors))
        assert_no_ceilings(payload)
        if dry_run:
            results.append({"name": payload["name"], "dry_run": True, "payload": payload})
            continue
        current = existing.get(payload["name"])
        if current and current.get("automation_id"):
            update = {k: v for k, v in payload.items() if k != "name"}
            results.append(devin.update_automation(str(current["automation_id"]), update))
        else:
            results.append(devin.create_automation(payload))
    for name in RETIRED_NAMES:
        old = existing.get(name)
        if old and old.get("automation_id"):
            devin.delete_automation(str(old["automation_id"]))
            results.append({"name": name, "deleted": True})
    return results
