"""Build and register the two automations (MAP and REDUCE) through the Automations API.

Both payloads are validated locally against the request schemas vendored from
https://docs.devin.ai/v3-openapi.yaml (orchestrator/v3_schemas.json) before anything is sent.

Design constraints honoured here (see AutomationCreateRequest in the spec):
* at most one start_session action, no monitor_session (deprecated for new automations)
* run_as = {"type": "organization"} on both
* no limits.max_acu_limit, no concurrency caps, no timeouts
* net_policy allows git-manager.devin.ai so spawned sessions can clone
* prompts are thin shims: clone this repo, run `python -m orchestrator <map|reduce>`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .devin_api import DevinClient

SCHEMAS_PATH = Path(__file__).with_name("v3_schemas.json")
FRIDAY_RRULE = "FREQ=WEEKLY;BYDAY=FR"
MAP_NAME = "superset-devin-automation: MAP (Friday ready-issue sweep)"
REDUCE_NAME = "superset-devin-automation: REDUCE (verify merged PR)"
GIT_MANAGER_NET_POLICY = {"allow": [{"hostname": "git-manager.devin.ai"}]}


def _shim(automation_repo: str, command: str, env_lines: str) -> str:
    return f"""@{automation_repo}

You are the thin dispatch shim for the superset-devin-automation loop. Do not reason about the
issues yourself; the orchestrator does that.

1. git clone https://github.com/{automation_repo} && cd superset-devin-automation
2. python -m pip install -e .
3. Export credentials from the session secrets:
{env_lines}
4. Run: {command}
5. Report the command's JSON output verbatim and exit. Do not open PRs, do not edit code.
"""


def map_payload(target_repo: str, automation_repo: str) -> dict[str, Any]:
    prompt = (
        _shim(
            automation_repo,
            "python -m orchestrator map",
            "   DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN (already in the environment as session secrets)",
        )
        + f"\nTarget repository for issues: @{target_repo}\n"
    )
    return {
        "name": MAP_NAME,
        "enabled": True,
        "run_as": {"type": "organization"},
        "metadata": {"component": "map", "target_repo": target_repo},
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
    target_repo: str, automation_repo: str, verify_branch: str = "master", every_n: int = 1
) -> dict[str, Any]:
    prompt = (
        _shim(
            automation_repo,
            f"VERIFY_BRANCH={verify_branch} VERIFY_EVERY_N_MERGES={every_n} "
            "python -m orchestrator reduce --event-json event.json  "
            "(first write the appended pull_request event payload to event.json, unmodified)",
            "   DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN (already in the environment as session secrets)",
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
            raise ValueError(f"forbidden ceiling {forbidden!r} present in automation payload")
    if any(a.get("type") == "monitor_session" for a in payload["actions"]):
        raise ValueError("monitor_session is deprecated for new automations")
    if sum(1 for a in payload["actions"] if a.get("type") == "start_session") > 1:
        raise ValueError("at most one start_session action")


def register(
    devin: DevinClient,
    target_repo: str,
    automation_repo: str,
    *,
    verify_branch: str = "master",
    every_n: int = 1,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Create MAP and REDUCE, or update them in place if automations with the same name exist."""
    results: list[dict[str, Any]] = []
    existing = {a.get("name"): a for a in devin.list_automations()} if not dry_run else {}
    payloads = (
        map_payload(target_repo, automation_repo),
        reduce_payload(target_repo, automation_repo, verify_branch, every_n),
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
