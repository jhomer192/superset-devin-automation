#!/usr/bin/env bash
# issue #10 / integration: real Postgres seeded by `superset db upgrade && superset init &&
# superset load-test-users` (scripts/python_tests.sh, the same entrypoint CI uses), then pytest.
# Required env: SUPERSET__SQLALCHEMY_DATABASE_URI (postgres), REDIS_HOST, REDIS_PORT.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_src
: "${SUPERSET__SQLALCHEMY_DATABASE_URI:?must point at a running Postgres, not sqlite}"
case "${SUPERSET__SQLALCHEMY_DATABASE_URI}" in
  postgresql*) ;;
  *) echo "issue #10 probe must run against Postgres, got ${SUPERSET__SQLALCHEMY_DATABASE_URI%%:*}" >&2; exit 2 ;;
esac
run_integration_pytest "${PROBES_DIR}/issue_10/sda_probe_machine_auth_tests.py"
