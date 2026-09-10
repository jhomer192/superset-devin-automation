#!/usr/bin/env bash
# PRD-OPS-1 / live_http: GET /health on the running Superset (SUPERSET_URL) is 200 with body OK.
set -euo pipefail
: "${SUPERSET_URL:?SUPERSET_URL must point at a running Superset}"
body="$(curl -sS -w '\n%{http_code}' "${SUPERSET_URL%/}/health")"
code="${body##*$'\n'}"
text="${body%$'\n'*}"
echo "GET /health -> ${code} ${text}"
[[ "${code}" == "200" && "${text}" == "OK" ]]
