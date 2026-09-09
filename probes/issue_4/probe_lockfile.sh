#!/usr/bin/env bash
# issue #4 / offline_static (closed as not planned): every resolved js-yaml 4.x copy is >= 4.3.2.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_src
"${PROBE_PYTHON}" "${PROBES_DIR}/lockfile_check.py" "${SUPERSET_SRC}" \
  "not-in:js-yaml:4.0.0:4.3.2"
