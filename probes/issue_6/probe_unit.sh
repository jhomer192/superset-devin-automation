#!/usr/bin/env bash
# issue #6 / offline_pytest (closed as not planned; kept for a possible reopen).
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
run_unit_pytest "${PROBES_DIR}/issue_6/test_issue_6_end_of_quarter.py"
