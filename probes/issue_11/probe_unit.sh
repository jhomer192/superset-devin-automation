#!/usr/bin/env bash
# issue #11 / offline_pytest: sqlite unit lane, verdict is pytest's exit code.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
run_unit_pytest "${PROBES_DIR}/issue_11/test_issue_11_diff_duplicate_keys.py"
