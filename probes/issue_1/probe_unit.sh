#!/usr/bin/env bash
# issue #1 / offline_pytest: check_cookie_security behaviour in the sqlite unit lane.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
run_unit_pytest "${PROBES_DIR}/issue_1/test_issue_1_cookie_security.py"
