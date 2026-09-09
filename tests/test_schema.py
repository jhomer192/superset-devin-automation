import json
from typing import Any

import pytest

from orchestrator.schema import (
    FIX_SCHEMA,
    MAX_SCHEMA_BYTES,
    VERIFICATION_SCHEMA,
    check_schema,
    is_valid,
    validate,
)

SHA = "a" * 40
BASE = "b" * 40

VERIFY_OK: dict[str, Any] = {
    "status": "ok",
    "head_sha": SHA,
    "base_sha": BASE,
    "acceptance_met": True,
    "probe_command": "verify/run_all.sh --head a --base b --issues 5",
    "probe_exit_code": 0,
    "evidence": "issue_5/unit: head=0 base=1 -> PASS",
    "results": [
        {
            "issue": 5,
            "probe": "issue_5/unit",
            "kind": "offline_pytest",
            "head_exit_code": 0,
            "base_exit_code": 1,
            "acceptance_met": True,
            "evidence": "pytest ok",
        }
    ],
}
VERIFY_ERR = {
    "status": "error",
    "head_sha": SHA,
    "error_message": "stage boot failed: /health never answered",
}
FIX_OK = {
    "status": "pr_opened",
    "issue": 5,
    "pr_url": "https://github.com/jhomer192/superset/pull/14",
    "branch": "devin/fix-5",
    "acceptance_met": True,
    "probe_command": "probes/run.sh issue_5/unit",
    "probe_exit_code": 0,
    "base_probe_exit_code": 1,
    "evidence": "failed at base, passes at head",
}
FIX_ERR = {"status": "error", "issue": 5, "error_message": "could not clone"}


@pytest.mark.parametrize("schema", [VERIFICATION_SCHEMA, FIX_SCHEMA])
def test_schemas_are_draft7_self_contained_and_small(schema):
    check_schema(schema)
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert "$ref" not in json.dumps(schema)
    assert len(json.dumps(schema).encode()) < MAX_SCHEMA_BYTES


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (VERIFICATION_SCHEMA, VERIFY_OK),
        (VERIFICATION_SCHEMA, VERIFY_ERR),
        (FIX_SCHEMA, FIX_OK),
        (FIX_SCHEMA, FIX_ERR),
    ],
)
def test_valid_payloads(schema, payload):
    assert validate(schema, payload) == []


def test_error_payload_must_not_carry_acceptance_met():
    assert not is_valid(VERIFICATION_SCHEMA, {**VERIFY_ERR, "acceptance_met": False})
    assert not is_valid(VERIFICATION_SCHEMA, {**VERIFY_ERR, "probe_exit_code": 1})
    assert not is_valid(FIX_SCHEMA, {**FIX_ERR, "acceptance_met": True})
    assert not is_valid(FIX_SCHEMA, {**FIX_ERR, "pr_url": FIX_OK["pr_url"]})


@pytest.mark.parametrize(
    "missing", ["acceptance_met", "probe_command", "probe_exit_code", "evidence", "results"]
)
def test_ok_payload_requires_acceptance_fields(missing):
    payload = {k: v for k, v in VERIFY_OK.items() if k != missing}
    assert not is_valid(VERIFICATION_SCHEMA, payload)


@pytest.mark.parametrize("missing", ["acceptance_met", "probe_command", "probe_exit_code", "evidence"])
def test_fix_ok_requires_acceptance_fields(missing):
    payload = {k: v for k, v in FIX_OK.items() if k != missing}
    assert not is_valid(FIX_SCHEMA, payload)


def test_unknown_properties_rejected():
    assert not is_valid(VERIFICATION_SCHEMA, {**VERIFY_OK, "vibes": "good"})
    assert not is_valid(FIX_SCHEMA, {**FIX_OK, "confidence": 0.9})
    bad_result = {**VERIFY_OK, "results": [{**VERIFY_OK["results"][0], "notes": "x"}]}
    assert not is_valid(VERIFICATION_SCHEMA, bad_result)


def test_error_requires_message_and_ok_forbids_it():
    assert not is_valid(VERIFICATION_SCHEMA, {"status": "error", "head_sha": SHA})
    assert not is_valid(VERIFICATION_SCHEMA, {**VERIFY_OK, "error_message": "but also ok"})


def test_shape_constraints():
    assert not is_valid(VERIFICATION_SCHEMA, {**VERIFY_OK, "head_sha": "abc"})
    assert not is_valid(VERIFICATION_SCHEMA, {**VERIFY_OK, "probe_exit_code": 256})
    assert not is_valid(VERIFICATION_SCHEMA, {**VERIFY_OK, "status": "passed"})
    assert not is_valid(FIX_SCHEMA, {**FIX_OK, "pr_url": "https://example.com/not-a-pr"})
    assert not is_valid(FIX_SCHEMA, {k: v for k, v in FIX_OK.items() if k != "pr_url"})
