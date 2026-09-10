"""Build and register the three automations (TESTING, AUTOPR, REPORT) through the Automations API.

The chain: a PR merges -> TESTING verifies every 5th merge, waits for the verdict and files an
`sda-regression` issue on failure -> that issue's `github:issues` event fires AUTOPR, which starts
the fix session -> its PR merges and re-enters TESTING. REPORT publishes fix-session verdicts and
the metrics digest. Automations with the pre-rename names (MAP, REDUCE) are deleted on register.

Every payload is validated locally against the request schemas vendored from
https://docs.devin.ai/v3-openapi.yaml (orchestrator/v3_schemas.json) before anything is sent.

Design constraints honoured here (see AutomationCreateRequest in the spec):
* at most one start_session action, no monitor_session (deprecated for new automations)
* run_as = {"type": "organization"} on all of them
* no limits.max_acu_limit, no concurrency caps, no timeouts
* net_policy allows the git proxy, GitHub, the Devin API and PyPI; the shim needs nothing else
* prompts are thin shims: clone this repo, run `python -m orchestrator <autopr|testing|report>`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .devin_api import DevinClient
from .regression import REGRESSION_LABEL

SCHEMAS_PATH = Path(__file__).with_name("v3_schemas.json")
FRIDAY_RRULE = "FREQ=WEEKLY;BYDAY=FR"
HOURLY_RRULE = "FREQ=HOURLY"
AUTOPR_NAME = "superset-devin-automation: AUTOPR (fix session per regression issue + Friday sweep)"
TESTING_NAME = "superset-devin-automation: TESTING (verify every 5th merge, file regressions)"
REPORT_NAME = "superset-devin-automation: REPORT (publish session outcomes)"
RETIRED_NAMES = (
    "superset-devin-automation: MAP (Friday ready-issue sweep)",
    "superset-devin-automation: REDUCE (verify merged PR)",
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


def autopr_payload(
    target_repo: str, automation_repo: str, playbook_id_fix: str | None = None
) -> dict[str, Any]:
    prompt = (
        _shim(
            automation_repo,
            _env_prefix("PLAYBOOK_ID_FIX", playbook_id_fix)
            + "python -m orchestrator autopr --event-json event.json  "
            "(if the triggering event is a github:issues payload, first write it to event.json unmodified; "
            "if the trigger is the schedule, run without --event-json)",
        )
        + f"\nTarget repository for issues: @{target_repo}\n"
        + f"An issue event means TESTING filed a `{REGRESSION_LABEL}` issue: the command starts the one fix "
        "session for that issue. The Friday schedule triages every open `ready` issue instead.\n"
    )
    return {
        "name": AUTOPR_NAME,
        "enabled": True,
        "run_as": {"type": "organization"},
        "metadata": {
            "component": "autopr",
            "target_repo": target_repo,
            "playbook_id_fix": playbook_id_fix or "",
        },
        "triggers": [
            {
                "event_type": "github:issues",
                # GitHub sends one `labeled` event per label on an issue created with labels, so
                # this single condition covers both create-with-label and label-later.
                "conditions": {
                    "any": [
                        {
                            "all": [
                                {"field": "action", "operator": "eq", "value": "labeled"},
                                {"field": "label.name", "operator": "eq", "value": REGRESSION_LABEL},
                                {"field": "repository.full_name", "operator": "eq", "value": target_repo},
                            ]
                        }
                    ]
                },
            },
            {
                "event_type": "schedule:recurring",
                "conditions": {
                    "any": [{"all": [{"field": "rrule", "operator": "recurrence", "value": FRIDAY_RRULE}]}]
                },
            },
        ],
        "actions": [
            {
                "type": "start_session",
                "prompt": prompt,
                "session": {"tags": ["sda-autopr"]},
            }
        ],
        "session_settings": {"net_policy": NET_POLICY},
    }


def testing_payload(
    target_repo: str,
    automation_repo: str,
    verify_branch: str = "master",
    every_n: int = 5,
    playbook_id_verify: str | None = None,
) -> dict[str, Any]:
    prompt = (
        _shim(
            automation_repo,
            f"VERIFY_BRANCH={verify_branch} VERIFY_EVERY_N_MERGES={every_n} "
            + _env_prefix("PLAYBOOK_ID_VERIFY", playbook_id_verify)
            + "python -m orchestrator testing --wait --event-json event.json  "
            "(first write the appended pull_request event payload to event.json, unmodified)",
        )
        + f"\nTarget repository: @{target_repo}\n"
        + f"Cadence: the orchestrator only starts a verification session for every {every_n}th PR "
        f"merged into `{verify_branch}`; on other merges it records the count and exits. With --wait it "
        "blocks until that verification finishes, however long that takes, then posts the verdict on every "
        f"PR of the window and files a `{REGRESSION_LABEL}` issue if it failed. Do not interrupt it.\n"
    )
    return {
        "name": TESTING_NAME,
        "enabled": True,
        "run_as": {"type": "organization"},
        "metadata": {
            "component": "testing",
            "target_repo": target_repo,
            "verify_branch": verify_branch,
            "verify_every_n_merges": str(every_n),
            "playbook_id_verify": playbook_id_verify or "",
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
                "session": {"tags": ["sda-testing"]},
            }
        ],
        "session_settings": {"net_policy": NET_POLICY},
    }


def report_payload(
    target_repo: str,
    automation_repo: str,
    digest_issue: int | None = None,
    digest_every_hours: int = 24,
) -> dict[str, Any]:
    digest_env = f"REPORT_DIGEST_ISSUE={digest_issue} " if digest_issue is not None else ""
    prompt = (
        _shim(
            automation_repo,
            f"{digest_env}REPORT_DIGEST_EVERY_HOURS={digest_every_hours} python -m orchestrator report",
        )
        + f"\nTarget repository: @{target_repo}\n"
        + "The command publishes the outcome of every finished fix session to its issue, sweeps up "
        "verification sessions whose TESTING run did not publish them, and appends the metrics digest; "
        "sessions already reported are skipped.\n"
    )
    return {
        "name": REPORT_NAME,
        "enabled": True,
        "run_as": {"type": "organization"},
        "metadata": {
            "component": "report",
            "target_repo": target_repo,
            "digest_issue": str(digest_issue) if digest_issue is not None else "",
            "digest_every_hours": str(digest_every_hours),
        },
        "triggers": [
            {
                "event_type": "schedule:recurring",
                "conditions": {
                    "any": [{"all": [{"field": "rrule", "operator": "recurrence", "value": HOURLY_RRULE}]}]
                },
            }
        ],
        "actions": [
            {
                "type": "start_session",
                "prompt": prompt,
                "session": {"tags": ["sda-report"]},
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
    every_n: int = 5,
    playbook_id_fix: str | None = None,
    playbook_id_verify: str | None = None,
    digest_issue: int | None = None,
    digest_every_hours: int = 24,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Create the automations, or update them in place if ones with the same name exist.

    The ones registered under retired names are deleted last, once every replacement is in place,
    so a failure part-way leaves the old chain running rather than nothing.
    """
    results: list[dict[str, Any]] = []
    existing = {a.get("name"): a for a in devin.list_automations()} if not dry_run else {}
    payloads = (
        testing_payload(target_repo, automation_repo, verify_branch, every_n, playbook_id_verify),
        autopr_payload(target_repo, automation_repo, playbook_id_fix),
        report_payload(target_repo, automation_repo, digest_issue, digest_every_hours),
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
