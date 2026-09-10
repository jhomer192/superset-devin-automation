#!/usr/bin/env bash
# PRD-AUTH-1 / live_http: database login returns an access_token that authorises GET /api/v1/me/.
set -euo pipefail
: "${SUPERSET_URL:?SUPERSET_URL must point at a running Superset}"
user="${SUPERSET_ADMIN_USER:-admin}"
pass="${SUPERSET_ADMIN_PASSWORD:-admin}"
resp="$(curl -sS -w '\n%{http_code}' -H 'Content-Type: application/json' \
  -d "{\"username\":\"${user}\",\"password\":\"${pass}\",\"provider\":\"db\"}" \
  "${SUPERSET_URL%/}/api/v1/security/login")"
code="${resp##*$'\n'}"
json="${resp%$'\n'*}"
echo "POST /api/v1/security/login -> ${code}"
[[ "${code}" == "200" ]]
token="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("access_token",""))' "${json}")"
[[ -n "${token}" ]] || { echo "no access_token in response" >&2; exit 1; }
me="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${token}" "${SUPERSET_URL%/}/api/v1/me/")"
echo "GET /api/v1/me/ (bearer) -> ${me}"
[[ "${me}" == "200" ]]
