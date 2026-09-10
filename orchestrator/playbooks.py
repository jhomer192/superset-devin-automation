"""Org playbooks for the two session kinds, registered idempotently by title.

A playbook carries the invariant workflow (``orchestrator/prompts.py`` bodies) and the
structured-output schema, so the per-session prompt is reduced to an ``@playbook:{id}`` token
plus the variables. ``playbook_id`` on a session is read-only and derived from that token.
Request shape: ``PlaybookCreateRequest`` (title, body required; macro, structured_output_schema
optional), vendored in ``v3_schemas.json``.
"""

from __future__ import annotations

import logging
from typing import Any

from .automations import assert_no_ceilings, validate_payload
from .devin_api import DevinClient
from .prompts import FIX_PLAYBOOK_BODY, VERIFY_PLAYBOOK_BODY
from .schema import FIX_SCHEMA, VERIFICATION_SCHEMA

log = logging.getLogger(__name__)

FIX_TITLE = "superset-devin-automation: remediation"
VERIFY_TITLE = "superset-devin-automation: verification"


def fix_playbook() -> dict[str, Any]:
    return {
        "title": FIX_TITLE,
        "body": FIX_PLAYBOOK_BODY,
        "macro": "!sda-fix",
        "structured_output_schema": FIX_SCHEMA,
    }


def verify_playbook() -> dict[str, Any]:
    return {
        "title": VERIFY_TITLE,
        "body": VERIFY_PLAYBOOK_BODY,
        "macro": "!sda-verify",
        "structured_output_schema": VERIFICATION_SCHEMA,
    }


def payloads() -> list[dict[str, Any]]:
    return [fix_playbook(), verify_playbook()]


def register(devin: DevinClient, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Create or update both playbooks by title; returns the API responses (or payloads on dry run)."""
    results: list[dict[str, Any]] = []
    existing = {p.get("title"): p for p in devin.list_playbooks()} if not dry_run else {}
    for payload in payloads():
        errors = validate_payload(payload, "PlaybookCreateRequest")
        if errors:
            raise ValueError(f"{payload['title']}: invalid payload:\n  " + "\n  ".join(errors))
        assert_no_ceilings(payload)
        if dry_run:
            results.append({"title": payload["title"], "dry_run": True, "payload": payload})
            continue
        current = existing.get(payload["title"])
        if current and current.get("playbook_id"):
            results.append(devin.update_playbook(str(current["playbook_id"]), payload))
        else:
            results.append(devin.create_playbook(payload))
    return results


def lookup_ids(devin: DevinClient) -> dict[str, str | None]:
    """playbook ids by title, for filling PLAYBOOK_ID_FIX / PLAYBOOK_ID_VERIFY."""
    by_title = {p.get("title"): str(p.get("playbook_id") or "") for p in devin.list_playbooks()}
    return {
        "PLAYBOOK_ID_FIX": by_title.get(FIX_TITLE) or None,
        "PLAYBOOK_ID_VERIFY": by_title.get(VERIFY_TITLE) or None,
    }
