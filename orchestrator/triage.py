"""Triage tier: decide, before spending any ACU, whether an issue deserves a Devin session.

Anything a mechanical tool would land (lockfile refreshes, coverage trivia) is deflected with a
logged reason. Dependabot is disabled on jhomer192/superset (security updates off, vulnerability
alerts 404, zero bot PRs), so dependency work is *deferred to a human*, never "delegated to a bot".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .registry import IssueSpec


class Decision(StrEnum):
    SESSION = "session"
    DEFLECT = "deflect"


class Reason(StrEnum):
    ELIGIBLE = "eligible"
    DEPENDENCY_REFRESH = "dependency_refresh"
    TEST_ONLY = "test_only"
    NO_ACCEPTANCE_CRITERIA = "no_acceptance_criteria"
    NO_COMMITTED_PROBE = "no_committed_probe"


@dataclass(frozen=True)
class Triage:
    issue_number: int
    decision: Decision
    reason: Reason
    detail: str


_DEPENDENCY_MARKERS = (
    re.compile(r"package-lock\.json", re.I),
    re.compile(r"--package-lock-only", re.I),
    re.compile(r"\bGHSA-[\w-]+", re.I),
    re.compile(r"\bnpm (update|audit|ci)\b", re.I),
    re.compile(r"\brequirements/[\w.]+\.txt\b"),
)
_TEST_ONLY_MARKERS = (
    re.compile(r"no production change", re.I),
    re.compile(r"\btest-only\b", re.I),
    re.compile(r"\buntested\b.*\bmapping\b", re.I),
)
_ACCEPTANCE_RE = re.compile(r"^#{2,4}\s*acceptance criteria", re.I | re.M)

DEPENDABOT_NOTE = (
    "Dependabot is disabled on this fork (security updates off, vulnerability alerts return 404, "
    "no bot PRs have ever been opened), so this is deferred to a human refresh rather than "
    "delegated to a bot that will not come."
)


def _labels(issue: dict[str, Any]) -> set[str]:
    return {str(lbl["name"] if isinstance(lbl, dict) else lbl).lower() for lbl in issue.get("labels") or []}


def classify(issue: dict[str, Any], spec: IssueSpec | None) -> Triage:
    number = int(issue["number"])
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    labels = _labels(issue)
    text = f"{title}\n{body}"

    if (
        "dependency" in labels
        or (spec is not None and spec.category == "dependency")
        or sum(1 for m in _DEPENDENCY_MARKERS if m.search(text)) >= 2
    ):
        probe = ", ".join(p.id for p in spec.probes) if spec and spec.probes else "none"
        return Triage(
            number,
            Decision.DEFLECT,
            Reason.DEPENDENCY_REFRESH,
            f"Mechanical dependency refresh; zero ACU spent. {DEPENDABOT_NOTE} "
            f"Verification probe on landing: {probe}.",
        )

    if any(m.search(text) for m in _TEST_ONLY_MARKERS) and "correctness" not in labels:
        return Triage(
            number,
            Decision.DEFLECT,
            Reason.TEST_ONLY,
            "Test-only request with no behavioural defect; coverage tooling reports this for free.",
        )

    if not _ACCEPTANCE_RE.search(body):
        return Triage(
            number,
            Decision.DEFLECT,
            Reason.NO_ACCEPTANCE_CRITERIA,
            "Issue has no '### Acceptance criteria' section; nothing a probe can decide.",
        )

    if spec is None or not spec.probes:
        return Triage(
            number,
            Decision.DEFLECT,
            Reason.NO_COMMITTED_PROBE,
            "No committed probe in probes/registry.json for this issue; add one before a "
            "session can be verified by exit code rather than agent judgment.",
        )

    return Triage(
        number,
        Decision.SESSION,
        Reason.ELIGIBLE,
        f"Eligible; deciding probes: {', '.join(p.id for p in spec.probes)}.",
    )
