#!/usr/bin/env bash
# issue #5 / offline_pytest: sqlite unit lane, verdict is pytest's exit code.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
run_unit_pytest "${PROBES_DIR}/issue_5/test_issue_5_kql_split.py"
