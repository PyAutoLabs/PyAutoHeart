#!/usr/bin/env bash
# heart/checks/worktree_drift.sh — thin shim over heart/checks/worktree_drift.py.
#
# The scan/categorise/report logic lives in the python module, where it is
# unit-testable (tests/test_worktree_drift.py); this wrapper only preserves the
# sourced-function contract tick.sh and the capabilities surface rely on.

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

check_worktree_drift() {
  heart_log INFO "$(c_info "worktree_drift: scanning via heart/checks/worktree_drift.py")"
  PYTHONPATH="$HEART_HOME${PYTHONPATH:+:$PYTHONPATH}" python3 -m heart.checks.worktree_drift
  heart_log OK "$(c_ok "worktree_drift: done")"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  check_worktree_drift
fi
