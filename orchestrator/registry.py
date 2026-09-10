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
class Requirement:
    """One PRD requirement: a stable id from the target repo's PRD.md and the probes that hold it."""

    id: str
    title: str
    probes: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    repo: str
    baseline_sha: str
    issues: tuple[IssueSpec, ...]
    prd_path: str = "PRD.md"
    requirements: tuple[Requirement, ...] = ()
    standalone_probes: tuple[Probe, ...] = ()

    def by_number(self, number: int) -> IssueSpec | None:
        return next((i for i in self.issues if i.number == number), None)

    def probes_for(self, numbers: list[int]) -> list[Probe]:
        out: list[Probe] = []
        for n in numbers:
            spec = self.by_number(n)
            if spec:
                out.extend(spec.probes)
        return out

    def all_probes(self) -> dict[str, tuple[int, Probe]]:
        """Every probe by id with the issue it belongs to (0 for a PRD-only probe)."""
        out: dict[str, tuple[int, Probe]] = {}
        for issue in self.issues:
            for p in issue.probes:
                out.setdefault(p.id, (issue.number, p))
        for p in self.standalone_probes:
            out.setdefault(p.id, (0, p))
        return out

    def requirement_ids(self) -> list[str]:
        return [r.id for r in self.requirements]

    def requirements_of(self, probe_id: str) -> list[str]:
        return [r.id for r in self.requirements if probe_id in r.probes]

    def requirement_probes(self, ids: list[str]) -> list[tuple[int, Probe]]:
        """Probes for the named requirements in registry order, each once."""
        known = self.all_probes()
        wanted = set(ids)
        out: list[tuple[int, Probe]] = []
        seen: set[str] = set()
        for req in self.requirements:
            if req.id not in wanted:
                continue
            for pid in req.probes:
                if pid not in seen:
                    seen.add(pid)
                    out.append(known[pid])
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
    standalone = tuple(Probe(p["id"], p["kind"], p["script"]) for p in raw.get("probes", []))
    for p in standalone:
        if p.kind not in PROBE_KINDS:
            raise ValueError(f"probe {p.id}: unknown probe kind {p.kind!r}")
        if not (path.parent.parent / p.script).exists():
            raise FileNotFoundError(f"probe {p.id}: script {p.script} missing")
    prd = raw.get("prd") or {}
    requirements = tuple(
        Requirement(str(r["id"]), str(r["title"]), tuple(str(p) for p in r.get("probes", [])))
        for r in prd.get("requirements", [])
    )
    registry = Registry(
        repo=raw["repo"],
        baseline_sha=raw["baseline_sha"],
        issues=tuple(sorted(issues, key=lambda i: i.number)),
        prd_path=str(prd.get("path", "PRD.md")),
        requirements=requirements,
        standalone_probes=standalone,
    )
    probe_ids = registry.all_probes()
    for req in requirements:
        for pid in req.probes:
            if pid not in probe_ids:
                raise ValueError(f"requirement {req.id}: unknown probe id {pid!r}")
    return registry
