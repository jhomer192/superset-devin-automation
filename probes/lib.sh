#!/usr/bin/env bash
# Shared helpers for probes. Every probe is a script; its exit code is the verdict.
#   0   -> acceptance criteria met at this checkout
#   !=0 -> not met (or the probe could not run; see stderr)
#
# Required environment:
#   SUPERSET_SRC   absolute path to a jhomer192/superset checkout at the commit under test
# Optional:
#   PROBE_PYTHON   python interpreter with superset's requirements installed (default: python)

set -euo pipefail

PROBES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE_PYTHON="${PROBE_PYTHON:-python}"

require_src() {
  if [[ -z "${SUPERSET_SRC:-}" || ! -d "${SUPERSET_SRC}/superset" ]]; then
    echo "SUPERSET_SRC must point at a superset checkout" >&2
    exit 2
  fi
}

# Copy a committed pytest module into the checkout's unit-test tree and run it in the
# sqlite unit lane (tests/unit_tests/conftest.py builds the sqlite app fixture).
run_unit_pytest() {
  local test_file="$1"
  require_src
  local dest_dir="${SUPERSET_SRC}/tests/unit_tests/sda_probes"
  mkdir -p "${dest_dir}"
  touch "${dest_dir}/__init__.py"
  cp "${test_file}" "${dest_dir}/"
  (
    cd "${SUPERSET_SRC}"
    export SUPERSET_TESTENV=true
    "${PROBE_PYTHON}" -m pytest -q -p no:cacheprovider \
      "tests/unit_tests/sda_probes/$(basename "${test_file}")" "${@:2}"
  )
}

# Copy a committed pytest module into the integration tree and run it through the same
# entrypoint CI uses (scripts/python_tests.sh: superset db upgrade, superset init,
# superset load-test-users, then pytest). Needs SUPERSET__SQLALCHEMY_DATABASE_URI pointing at
# a real Postgres and REDIS_HOST/REDIS_PORT reachable.
run_integration_pytest() {
  local test_file="$1"
  require_src
  local dest="${SUPERSET_SRC}/tests/integration_tests/utils/$(basename "${test_file}")"
  cp "${test_file}" "${dest}"
  (
    cd "${SUPERSET_SRC}"
    export SUPERSET_CONFIG="${SUPERSET_CONFIG:-tests.integration_tests.superset_test_config}"
    export PYTHONPATH="${SUPERSET_SRC}${PYTHONPATH:+:${PYTHONPATH}}"
    bash scripts/python_tests.sh "tests/integration_tests/utils/$(basename "${test_file}")"
  )
}
