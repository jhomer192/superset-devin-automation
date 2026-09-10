"""Print `issue<TAB>probe_id<TAB>kind<TAB>script` rows for the requested issues (registry order)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.registry import load_registry  # noqa: E402


def parse_numbers(csv: str) -> list[int]:
    return [int(x) for x in csv.split(",") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issues", default="")
    ap.add_argument("--regression", default="")
    args = ap.parse_args()
    wanted = parse_numbers(args.issues) + parse_numbers(args.regression)
    seen: set[str] = set()
    for issue in load_registry().issues:
        if issue.number not in wanted:
            continue
        for probe in issue.probes:
            if probe.id in seen:
                continue
            seen.add(probe.id)
            sys.stdout.write(f"{issue.number}\t{probe.id}\t{probe.kind}\t{probe.script}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
