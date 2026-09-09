#!/usr/bin/env bash
# Run one probe by id (see registry.json), e.g.  probes/run.sh issue_5/unit
# Exit code is the verdict.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
id="${1:?probe id required}"
script="$(python3 - "$here/registry.json" "$id" <<'PY'
import json, sys
reg = json.load(open(sys.argv[1]))
for issue in reg["issues"]:
    for probe in issue["probes"]:
        if probe["id"] == sys.argv[2]:
            print(probe["script"]); sys.exit(0)
sys.exit(f"unknown probe id {sys.argv[2]}")
PY
)"
exec bash "${here}/../${script}"
