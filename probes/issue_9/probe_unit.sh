#!/usr/bin/env bash
# issue #9 / offline_pytest: sqlite unit lane, verdict is pytest's exit code.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
run_unit_pytest "${PROBES_DIR}/issue_9/test_issue_9_svg_sanitizer.py"
