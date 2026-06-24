#!/usr/bin/env bash
# heart/tick.sh — one-shot refresh: run every check, aggregate into state.json.
#
# Composable: run directly for a force-refresh, or via the daemon loop
# in heart/daemon.sh. Each check writes its own JSON sidecar; the Python
# aggregator at the end reads them all into ~/.pyauto-heart/state.json.

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/_common.sh"

cd "$HEART_HOME"

heart_log INFO "$(c_bold "==== tick start") $(c_meta "($(date -Iseconds))") $(c_bold "====")"

# Run the four bash checks. Each is parallel-internal, so we run them
# sequentially for clean output, but they're cheap enough that this is
# fine (~5–10s total).
bash "$HEART_HOME/heart/checks/repo_state.sh"      || heart_log WARN "$(c_warn 'repo_state failed')"
bash "$HEART_HOME/heart/checks/ci_status.sh"       || heart_log WARN "$(c_warn 'ci_status failed')"
bash "$HEART_HOME/heart/checks/open_prs.sh"        || heart_log WARN "$(c_warn 'open_prs failed')"
bash "$HEART_HOME/heart/checks/worktree_drift.sh"  || heart_log WARN "$(c_warn 'worktree_drift failed')"

# Python: script timing regressions. Only runs if PyAutoBuild test_results/latest exists.
if [[ -d "$PYAUTO_ROOT/PyAutoBuild/test_results/latest" ]]; then
  PYTHONPATH="$HEART_HOME" python3 -m heart.checks.script_timing || heart_log WARN "$(c_warn 'script_timing failed')"
  PYTHONPATH="$HEART_HOME" python3 -m heart.checks.test_run     || heart_log WARN "$(c_warn 'test_run failed')"
else
  heart_log INFO "$(c_meta 'script_timing/test_run: skipped (no PyAutoBuild/test_results/latest)')"
fi

# Python: workspace-vs-library version skew (cheap file reads; no heavy imports).
PYTHONPATH="$HEART_HOME" python3 -m heart.checks.version_skew || heart_log WARN "$(c_warn 'version_skew failed')"

# Aggregate into state.json.
PYTHONPATH="$HEART_HOME" python3 -c "
from heart import state
state.aggregate()
"

# Compute the composite release-readiness verdict from the aggregated state.
PYTHONPATH="$HEART_HOME" python3 -c "
from heart import readiness
readiness.run()
" || heart_log WARN "$(c_warn 'readiness failed')"

heart_log OK "$(c_bold "==== tick complete") $(c_meta "(state at $HEART_STATE_FILE)") $(c_bold "====")"
