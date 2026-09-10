"""Build and register the three automations (MAP, REDUCE, REPORT) through the Automations API.

Every payload is validated locally against the request schemas vendored from
https://docs.devin.ai/v3-openapi.yaml (orchestrator/v3_schemas.json) before anything is sent.

Design constraints honoured here (see AutomationCreateRequest in the spec):
* at most one start_session action, no monitor_session (deprecated for new automations)
* run_as = {"type": "organization"} on all of them
* no limits.max_acu_limit, no concurrency caps, no timeouts
* net_policy allows git-manager.devin.ai so spawned sessions can clone
* prompts are thin shims: clone this repo, run `python -m orchestrator <map|reduce|report>`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .devin_api import DevinClient

SCHEMAS_PATH = Path(__file__).with_name("v3_schemas.json")
FRIDAY_RRULE = "FREQ=WEEKLY;BYDAY=FR"
HOURLY_RRULE = "FREQ=HOURLY"
MAP_NAME = "superset-devin-automation: MAP (Friday ready-issue sweep)"
REDUCE_NAME = "superset-devin-automation: REDUCE (verify merged PR)"
REPORT_NAME = "superset-devin-automation: REPORT (publish session outcomes)"
GIT_MANAGER_NET_POLICY = {"allow": [{"hostname": "git-manager.devin.ai"}]}


def _shim(automation_repo: str, command: str) -> str:
    return f"""@{automation_repo}

You are the thin dispatch shim for the superset-devin-automation loop. Do not reason about the
issues yourself; the orchestrator does that.

1. git clone https://github.com/{automation_repo} && cd superset-devin-automation
2. python -m pip install -e .
3. Credentials: the org session secrets `superset_remediation_bot` (Devin API key) and
   `superset_github_key` (GitHub token) are already environment variables; the orchestrator reads
   them by those names (either case) and has the org id built in. Do not look for other names; if
   one of the two is missing, run the command anyway and report its error verbatim.
4. Run: {command}
5. Report the command's JSON output verbatim and exit. Do not open PRs, do not edit code.
"""


def _env_prefix(name: str, playbook_id: str | None) -> str:
    return f"{name}={playbook_id} " if playbook_id else ""


def map_payload(target_repo: str, automation_repo: str, playbook_id_fix: str | None = None) -> dict[str, Any]:
    prompt = (
        _shim(
            automation_repo,
            _env_prefix("PLAYBOOK_ID_FIX", playbook_id_fix) + "python -m orchestrator map",
        )
        + f"\nTarget repository for issues: @{target_repo}\n"
    )
    return {
        "name": MAP_NAME,
        "enabled": True,
        "run_as": {"type": "organization"},
        "metadata": {
            "component": "map",
            "target_repo": target_repo,
            "playbook_id_fix": playbook_id_fix or "",
        },
        "triggers": [
            {
                "event_type": "schedule:recurring",
                "conditions": {
                    "any": [{"all": [{"field": "rrule", "operator": "recurrence", "value": FRIDAY_RRULE}]}]
                },
            }
        ],
        "actions": [
            {
                "type": "start_session",
                "prompt": prompt,
                "session": {"tags": ["sda-map"]},
            }
        ],
        "session_settings": {"net_policy": GIT_MANAGER_NET_POLICY},
    }


def reduce_payload(
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
            + "python -m orchestrator reduce --event-json event.json  "
            "(first write the appended pull_request event payload to event.json, unmodified)",
        )
        + f"\nTarget repository: @{target_repo}\n"
        + f"Cadence: the orchestrator only starts a verification session for every {every_n}th PR "
        f"merged into `{verify_branch}`; on other merges it records the count and exits.\n"
    )
    return {
        "name": REDUCE_NAME,
        "enabled": True,
        "run_as": {"type": "organization"},
        "metadata": {
            "component": "reduce",
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
                "session": {"tags": ["sda-reduce"]},
            }
        ],
        "session_settings": {"net_policy": GIT_MANAGER_NET_POLICY},
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
        + "The command publishes the outcome of every finished fix/verification session to the "
        "issue or PR it belongs to; sessions already reported are skipped. A verification that "
        "failed also gets a regression issue and a fix session started for it.\n"
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
        "session_settings": {"net_policy": GIT_MANAGER_NET_POLICY},
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
    """Create the automations, or update them in place if ones with the same name exist."""
    results: list[dict[str, Any]] = []
    existing = {a.get("name"): a for a in devin.list_automations()} if not dry_run else {}
    payloads = (
        map_payload(target_repo, automation_repo, playbook_id_fix),
        reduce_payload(target_repo, automation_repo, verify_branch, every_n, playbook_id_verify),
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
    return results
