"""Structured-output schemas handed to Devin sessions (``structured_output_schema``).

Constraints from the v3 spec: JSON Schema Draft 7, self-contained (no external ``$ref``), <= 64 KB.
The taxonomy is machine-enforced by the schema itself, not by prose in the prompt:

* ``status == "error"`` -> ``error_message`` required, ``acceptance_met`` (and results) forbidden.
* any other status      -> ``acceptance_met``, ``probe_command``, ``probe_exit_code``, ``evidence``
  all required.
* unknown properties are rejected everywhere.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
from jsonschema import Draft7Validator

MAX_SCHEMA_BYTES = 64 * 1024

_SHA = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
_EXIT = {"type": "integer", "minimum": 0, "maximum": 255}

_PROBE_RESULT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "issue",
        "probe",
        "kind",
        "head_exit_code",
        "acceptance_met",
        "evidence",
    ],
    "properties": {
        "issue": {"type": "integer", "minimum": 0},
        "probe": {"type": "string", "minLength": 1},
        "requirements": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "kind": {
            "type": "string",
            "enum": ["offline_pytest", "offline_static", "log_assertion", "integration", "live_http"],
        },
        "head_exit_code": _EXIT,
        "acceptance_met": {"type": "boolean"},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 20000},
    },
}

VERIFICATION_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SupersetVerificationResult",
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "head_sha"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "error"]},
        "head_sha": _SHA,
        "error_message": {"type": "string", "minLength": 1, "maxLength": 20000},
        "acceptance_met": {"type": "boolean"},
        "probe_command": {"type": "string", "minLength": 1},
        "probe_exit_code": _EXIT,
        "evidence": {"type": "string", "minLength": 1, "maxLength": 60000},
        "results": {"type": "array", "items": _PROBE_RESULT},
        "superset_health_url": {"type": "string"},
    },
    "if": {"properties": {"status": {"const": "error"}}},
    "then": {
        "required": ["error_message"],
        "not": {
            "anyOf": [
                {"required": ["acceptance_met"]},
                {"required": ["probe_exit_code"]},
                {"required": ["results"]},
            ]
        },
    },
    "else": {
        "required": ["acceptance_met", "probe_command", "probe_exit_code", "evidence", "results"],
        "not": {"required": ["error_message"]},
    },
}

FIX_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SupersetFixResult",
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "issue"],
    "properties": {
        "status": {"type": "string", "enum": ["pr_opened", "no_change_needed", "error"]},
        "issue": {"type": "integer", "minimum": 1},
        "pr_url": {"type": "string", "pattern": "^https://github\\.com/[^/]+/[^/]+/pull/\\d+$"},
        "branch": {"type": "string", "minLength": 1},
        "error_message": {"type": "string", "minLength": 1, "maxLength": 20000},
        "acceptance_met": {"type": "boolean"},
        "probe_command": {"type": "string", "minLength": 1},
        "probe_exit_code": _EXIT,
        "base_probe_exit_code": _EXIT,
        "evidence": {"type": "string", "minLength": 1, "maxLength": 60000},
    },
    "allOf": [
        {
            "if": {"properties": {"status": {"const": "error"}}},
            "then": {
                "required": ["error_message"],
                "not": {"anyOf": [{"required": ["acceptance_met"]}, {"required": ["pr_url"]}]},
            },
            "else": {
                "required": ["acceptance_met", "probe_command", "probe_exit_code", "evidence"],
                "not": {"required": ["error_message"]},
            },
        },
        {
            "if": {"properties": {"status": {"const": "pr_opened"}}},
            "then": {"required": ["pr_url", "branch"]},
        },
    ],
}


_CANDIDATE = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "fingerprint",
        "title",
        "category",
        "severity",
        "location",
        "repro",
        "expected",
        "actual",
        "evidence",
        "probe_kind",
        "probe_script",
    ],
    "properties": {
        "fingerprint": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{2,79}$"},
        "title": {"type": "string", "minLength": 8, "maxLength": 160},
        "category": {"type": "string", "enum": ["security", "bug", "performance", "code-quality"]},
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "location": {"type": "string", "minLength": 1, "maxLength": 400},
        "security_matrix_row": {"type": "string", "minLength": 1, "maxLength": 200},
        "attacker_role": {"type": "string", "minLength": 1, "maxLength": 80},
        "repro": {"type": "string", "minLength": 1, "maxLength": 8000},
        "expected": {"type": "string", "minLength": 1, "maxLength": 2000},
        "actual": {"type": "string", "minLength": 1, "maxLength": 2000},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 20000},
        "probe_kind": {
            "type": "string",
            "enum": ["offline_pytest", "offline_static", "log_assertion", "integration", "live_http"],
        },
        "probe_script": {"type": "string", "minLength": 1, "maxLength": 20000},
    },
}

EXPLORE_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SupersetExplorationResult",
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "head_sha"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "error"]},
        "head_sha": _SHA,
        "error_message": {"type": "string", "minLength": 1, "maxLength": 20000},
        "booted": {"type": "boolean"},
        "areas_covered": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 50},
        "candidates": {"type": "array", "items": _CANDIDATE, "maxItems": 10},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 60000},
    },
    "if": {"properties": {"status": {"const": "error"}}},
    "then": {"required": ["error_message"], "not": {"required": ["candidates"]}},
    "else": {
        "required": ["booted", "areas_covered", "candidates", "evidence"],
        "not": {"required": ["error_message"]},
    },
}


def check_schema(schema: dict[str, Any]) -> None:
    """Fail loudly if a schema breaks the v3 constraints."""
    Draft7Validator.check_schema(schema)
    encoded = json.dumps(schema, separators=(",", ":")).encode()
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise ValueError(f"schema is {len(encoded)} bytes, limit is {MAX_SCHEMA_BYTES}")
    if "$ref" in encoded.decode():
        raise ValueError("schema must be self-contained: no $ref allowed")


def validate(schema: dict[str, Any], payload: Any) -> list[str]:
    """Return a list of validation error messages; empty means valid."""
    validator = Draft7Validator(schema)
    return sorted(
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in validator.iter_errors(payload)
    )


def is_valid(schema: dict[str, Any], payload: Any) -> bool:
    try:
        jsonschema.validate(payload, schema, cls=Draft7Validator)
    except jsonschema.ValidationError:
        return False
    return True
