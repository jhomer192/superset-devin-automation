#!/usr/bin/env bash
# PRD-SEC-2 / live_http: GET /api/v1/chart/ with no credentials is 401 on the running Superset.
set -euo pipefail
: "${SUPERSET_URL:?SUPERSET_URL must point at a running Superset}"
code="$(curl -sS -o /dev/null -w '%{http_code}' "${SUPERSET_URL%/}/api/v1/chart/")"
echo "GET /api/v1/chart/ (anonymous) -> ${code}"
[[ "${code}" == "401" ]]
