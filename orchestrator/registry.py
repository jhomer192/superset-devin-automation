"""Typed access to probes/registry.json: the committed issue -> probe mapping.

`IssueSpec` is also the schema of an entry: loading rejects a key the dataclass does not
declare, so a field renamed here or in the JSON cannot drift silently past `ISSUES.md`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "probes" / "registry.json"

PROBE_KINDS = ("offline_pytest", "offline_static", "log_assertion", "integration", "live_http")


@dataclass(frozen=True)
class Probe:
    id: str
    kind: str
    script: str


@dataclass(frozen=True)
class IssueSpec:
    number: int
    title: str
    category: str
    labels: tuple[str, ...]
    state: str
    condition: str
    probes: tuple[Probe, ...]
    closing_pr: str | None = None
    closing_pr_state: str | None = None
    state_reason: str | None = None
    not_planned_reason: str | None = None
    triage_note: str | None = None


@dataclass(frozen=True)
class Registry:
    repo: str
    baseline_sha: str
    issues: tuple[IssueSpec, ...]

    def by_number(self, number: int) -> IssueSpec | None:
        return next((i for i in self.issues if i.number == number), None)

    def probes_for(self, numbers: list[int]) -> list[Probe]:
        out: list[Probe] = []
        for n in numbers:
            spec = self.by_number(n)
            if spec:
                out.extend(spec.probes)
        return out


def load_registry(path: Path = REGISTRY_PATH) -> Registry:
    raw = json.loads(path.read_text())
    known = {f.name for f in fields(IssueSpec)}
    issues: list[IssueSpec] = []
    for item in raw["issues"]:
        unknown = sorted(set(item) - known)
        if unknown:
            raise ValueError(f"issue #{item['number']}: unknown registry keys {unknown}")
        probes = tuple(Probe(p["id"], p["kind"], p["script"]) for p in item.get("probes", []))
        for p in probes:
            if p.kind not in PROBE_KINDS:
                raise ValueError(f"issue #{item['number']}: unknown probe kind {p.kind!r}")
            if not (path.parent.parent / p.script).exists():
                raise FileNotFoundError(f"issue #{item['number']}: probe script {p.script} missing")
        issues.append(
            IssueSpec(
                number=int(item["number"]),
                title=item["title"],
                category=item["category"],
                labels=tuple(item.get("labels", [])),
                state=item["state"],
                condition=item["condition"],
                probes=probes,
                closing_pr=item.get("closing_pr"),
                closing_pr_state=item.get("closing_pr_state"),
                state_reason=item.get("state_reason"),
                not_planned_reason=item.get("not_planned_reason"),
                triage_note=item.get("triage_note"),
            )
        )
    return Registry(
        repo=raw["repo"],
        baseline_sha=raw["baseline_sha"],
        issues=tuple(sorted(issues, key=lambda i: i.number)),
    )
