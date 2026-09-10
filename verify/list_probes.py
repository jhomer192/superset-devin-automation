"""Print `issue<TAB>probe_id<TAB>kind<TAB>script` rows for the probes of the requested PRD
requirements (registry order, each probe once; issue is 0 for a probe that belongs to no issue)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.registry import load_registry  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requirements", required=True)
    args = ap.parse_args()
    registry = load_registry()
    requirements = [x.strip() for x in args.requirements.split(",") if x.strip()]
    for number, probe in registry.requirement_probes(requirements):
        sys.stdout.write(f"{number}\t{probe.id}\t{probe.kind}\t{probe.script}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
