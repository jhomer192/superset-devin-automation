#!/usr/bin/env bash
# issue #9 / live_http: exercise the theme write path on a running Superset (SUPERSET_URL).
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
: "${SUPERSET_URL:?SUPERSET_URL must point at a running Superset}"
curl -fsS "${SUPERSET_URL%/}/health" >/dev/null || { echo "Superset not healthy at ${SUPERSET_URL}" >&2; exit 2; }
"${PROBE_PYTHON}" "${PROBES_DIR}/issue_9/live_theme_api.py"
