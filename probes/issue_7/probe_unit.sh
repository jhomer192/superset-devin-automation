#!/usr/bin/env bash
# issue #7 / offline_pytest: sqlite unit lane, verdict is pytest's exit code.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
run_unit_pytest "${PROBES_DIR}/issue_7/test_issue_7_custom_tags_rison.py"
