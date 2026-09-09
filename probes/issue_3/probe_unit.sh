#!/usr/bin/env bash
# issue #3 / offline_pytest: sqlite unit lane, verdict is pytest's exit code.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
run_unit_pytest "${PROBES_DIR}/issue_3/test_issue_3_chunk_size.py"
