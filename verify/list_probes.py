"""Print `issue<TAB>probe_id<TAB>kind<TAB>script` rows for the requested issues (registry order)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "probes" / "registry.json"


def parse_numbers(csv: str) -> list[int]:
    return [int(x) for x in csv.split(",") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issues", default="")
    ap.add_argument("--regression", default="")
    args = ap.parse_args()
    wanted = parse_numbers(args.issues) + parse_numbers(args.regression)
    registry = json.loads(REGISTRY.read_text())
    seen: set[str] = set()
    for issue in registry["issues"]:
        if issue["number"] not in wanted:
            continue
        for probe in issue["probes"]:
            if probe["id"] in seen:
                continue
            seen.add(probe["id"])
            sys.stdout.write(f"{issue['number']}\t{probe['id']}\t{probe['kind']}\t{probe['script']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
