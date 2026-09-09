#!/usr/bin/env bash
# issue #1 / log_assertion: boot the real Flask app factory under two configs and assert on the
# startup log content, not on the code.
#
#   A) PREFERRED_URL_SCHEME=https, SESSION_COOKIE_SECURE=False  -> must start (exit 0) and emit a
#      WARNING whose message contains the literal SESSION_COOKIE_SECURE
#   B) SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SECURE=False, non-debug, non-test
#      -> must refuse to start with exit code 1 and name both keys in the log
#
# Fails at base: A boots silently, B boots successfully.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_src

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

cat >"${work}/cfg_https_insecure.py" <<'EOF'
SECRET_KEY = "sda-probe-" + "x" * 64
SQLALCHEMY_DATABASE_URI = "sqlite://"
PREFERRED_URL_SCHEME = "https"
SESSION_COOKIE_SECURE = False
TALISMAN_ENABLED = False
EOF

cat >"${work}/cfg_samesite_none.py" <<'EOF'
SECRET_KEY = "sda-probe-" + "x" * 64
SQLALCHEMY_DATABASE_URI = "sqlite://"
SESSION_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SECURE = False
TALISMAN_ENABLED = False
EOF

boot() {
  # prints the combined log to stdout, returns the app factory's exit code
  (
    cd "${SUPERSET_SRC}"
    unset SUPERSET_TESTENV
    export FLASK_DEBUG=0
    export SUPERSET_CONFIG_PATH="$1"
    "${PROBE_PYTHON}" - <<'PY' 2>&1
import logging, sys
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s", stream=sys.stdout)
from superset.app import create_app
create_app()
print("SDA_BOOTED")
PY
  )
}

set +e
log_a="$(boot "${work}/cfg_https_insecure.py")"; rc_a=$?
log_b="$(boot "${work}/cfg_samesite_none.py")"; rc_b=$?
set -e

echo "--- case A (https + Secure=False) exit=${rc_a}"
echo "${log_a}" | grep -E 'WARNING.*SESSION_COOKIE_SECURE' || true
echo "--- case B (SameSite=None + Secure=False) exit=${rc_b}"
echo "${log_b}" | grep -E 'SESSION_COOKIE_(SAMESITE|SECURE)' || true

fail=0
if [[ ${rc_a} -ne 0 ]] || ! grep -q "SDA_BOOTED" <<<"${log_a}"; then
  echo "FAIL A: app did not start" >&2; fail=1
fi
if ! grep -Eq 'WARNING.*SESSION_COOKIE_SECURE' <<<"${log_a}"; then
  echo "FAIL A: no WARNING record naming SESSION_COOKIE_SECURE" >&2; fail=1
fi
if [[ ${rc_b} -ne 1 ]]; then
  echo "FAIL B: expected exit 1, got ${rc_b}" >&2; fail=1
fi
if ! grep -q 'SESSION_COOKIE_SAMESITE' <<<"${log_b}" || ! grep -q 'SESSION_COOKIE_SECURE' <<<"${log_b}"; then
  echo "FAIL B: log does not name both cookie keys" >&2; fail=1
fi
exit ${fail}
