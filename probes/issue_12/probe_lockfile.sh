#!/usr/bin/env bash
# issue #12 / offline_static: nx >= 23.1.2 < 24 inside the lerna range, no brace-expansion in
# [4.0.0, 5.0.9), 2.x/1.x copies stay patched, and the minimatch@>=10 override either reads
# >=5.0.9 or is gone. `npm ci && npm run build` is asserted by the verification harness.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_src
"${PROBE_PYTHON}" "${PROBES_DIR}/lockfile_check.py" "${SUPERSET_SRC}" \
  "path-min:node_modules/nx:23.1.2:24.0.0" \
  "not-in:brace-expansion:4.0.0:5.0.9" \
  "not-in:brace-expansion:2.0.0:2.1.4" \
  "not-in:brace-expansion:1.0.0:1.1.18" \
  "any-of:override-floor:minimatch@>=10:brace-expansion:5.0.9|no-override:minimatch@>=10:brace-expansion"
