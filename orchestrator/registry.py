"""Typed access to probes/registry.json: the committed issue -> probe mapping."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "probes" / "registry.json"

PROBE_KINDS = ("offline_pytest", "offline_static", "log_assertion", "integration", "live_http")


@dataclass(frozen=True)
class Probe:
    id: str
    kind: str
    script: str

    @property
    def needs_running_app(self) -> bool:
        return self.kind == "live_http"

    @property
    def needs_postgres(self) -> bool:
        return self.kind in {"integration", "live_http"}


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
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Registry:
    repo: str
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
    issues: list[IssueSpec] = []
    for item in raw["issues"]:
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
    return Registry(repo=raw["repo"], issues=tuple(sorted(issues, key=lambda i: i.number)))
