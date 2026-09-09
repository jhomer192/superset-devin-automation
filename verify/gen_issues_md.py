"""Render ISSUES.md from probes/registry.json so the table can never drift from the registry.

python verify/gen_issues_md.py          # rewrite ISSUES.md
python verify/gen_issues_md.py --check  # exit 1 if ISSUES.md is stale (used by tests/CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "probes" / "registry.json"
TARGET = ROOT / "ISSUES.md"


def _cell(text: str | None) -> str:
    return (text or "—").replace("|", "\\|").replace("\n", " ")


def render(registry: dict[str, object]) -> str:
    repo = str(registry["repo"])
    issues = registry["issues"]
    assert isinstance(issues, list)
    lines = [
        "# Issues on jhomer192/superset",
        "",
        "Generated from `probes/registry.json` by `verify/gen_issues_md.py`; `tests/test_docs.py` fails",
        "if this file is stale. Every file:line citation points into `jhomer192/superset` at the commit",
        f'recorded in the registry (`{registry["baseline_sha"]}`). "Deciding probe" is the committed',
        "script whose exit code decides whether the remediation worked; agent judgement is never the",
        "verdict. Closed-as-not-planned issues keep their probe where one exists so a later change that",
        "happens to fix them is still detected.",
        "",
        "| # | Title | State | Category | Condition (file:line) | Deciding probe | Closing PR / not-planned reason |",  # noqa: E501
        "|---|-------|-------|----------|-----------------------|----------------|---------------------------------|",
    ]
    for issue in issues:
        assert isinstance(issue, dict)
        number = issue["number"]
        state = issue["state"]
        if issue.get("state_reason"):
            state = f"{state} ({issue['state_reason']})"
        probes = (
            "<br>".join(f"`{p['id']}` ({p['kind']}) → `{p['script']}`" for p in issue["probes"]) or "none"
        )
        if issue.get("closing_pr"):
            outcome = f"[PR]({issue['closing_pr']})"
            if issue.get("closing_pr_state"):
                outcome += f" ({issue['closing_pr_state']})"
        elif issue.get("not_planned_reason"):
            outcome = _cell(issue["not_planned_reason"])
        else:
            outcome = "open; a MAP run starts a fix session"
        lines.append(
            f"| [{number}](https://github.com/{repo}/issues/{number}) | {_cell(issue['title'])} | {state} "
            f"| {issue['category']} | {_cell(issue['condition'])} | {probes} | {outcome} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = render(json.loads(REGISTRY.read_text()))
    if args.check:
        current = TARGET.read_text() if TARGET.exists() else ""
        if current != text:
            sys.stderr.write("ISSUES.md is stale; run python verify/gen_issues_md.py\n")
            return 1
        return 0
    TARGET.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
