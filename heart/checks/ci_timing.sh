#!/usr/bin/env bash
# heart/checks/ci_timing.sh — CI wall-clock per tracked workflow (the speed leg).
#
# For each polled repo this fetches the recent workflow runs via `gh api` (cheap
# metadata only) and pipes them to `heart.checks.ci_timing`, which times each
# tracked gate — the group's `required_workflows` plus any
# `performance.extra_workflows` entry naming the repo — and writes
# $HEART_PER_REPO_DIR/<name>.ci_timing.json. A second, aggregate pass folds every
# sidecar plus the PREVIOUSLY PUBLISHED board.json into the global rollup at
# $HEART_STATE_DIR/ci_timing.json (per-gate p50/max, drift vs the gate's own
# history, the rolled-forward history, hang/kill events).
#
# Unlike ci_status.sh the query carries NO `branch=main` filter and NO
# `exclude_pull_requests`: the contributor-facing number is exactly what someone
# waits on before a merge, so `pull_request` runs are the point of the query.
# `per_page=50` gives a week or two of runs on the busy gates without paging.
#
# The history is *self-carrying*: instead of committing baselines, the aggregate
# step re-reads the board.json this repo published yesterday, drops any entry for
# today, appends today's p50 per gate, and caps the list. Free, idempotent per
# date, no state to corrupt — and a publish gap costs a sparkline, never a
# render. HEART_PREV_BOARD_URL overrides the URL (tests, staging boards); any
# fetch failure degrades to "no history", never to an error.
#
# Observer boundary: everything written lives under $HEART_STATE_DIR. A repo
# whose runs fetch fails records the reason and NO workflows — "we could not
# ask" must never render as "all quiet".

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

# Recent runs, newest first, every branch and every event.
_CI_TIMING_RUNS_PATH="actions/runs?per_page=50"

time_one_repo_ci() {
  local owner_name="$1"
  local group="$2"
  local name="${owner_name##*/}"
  local owner="${owner_name%%/*}"

  local runs ts err rc
  err="$(mktemp)"
  runs="$(gh api "repos/$owner_name/$_CI_TIMING_RUNS_PATH" 2>"$err")"
  rc=$?
  ts="$(date -Iseconds)"

  local fetch_error=""
  if [[ $rc -ne 0 || -z "$runs" ]]; then
    # Collapse gh's stderr to one line so it fits the sidecar and the log.
    fetch_error="$(tr '\n' ' ' <"$err" | cut -c1-200)"
    [[ -z "${fetch_error// }" ]] && fetch_error="gh api exited $rc"
    runs='{}'
  fi
  rm -f "$err"

  printf '%s' "$runs" | PYTHONPATH="$HEART_HOME" python3 -m heart.checks.ci_timing \
    --name "$name" --group "$group" --owner "$owner" --ts "$ts" \
    --fetch-error "$fetch_error" \
    --out "$HEART_PER_REPO_DIR/$name.ci_timing.json"
}

# Print the path of a tempfile holding the previously published board.json, or
# nothing at all when it cannot be had. Never fails the check.
_ci_timing_prev_board() {
  local url tmp
  url="${HEART_PREV_BOARD_URL:-}"
  if [[ -z "$url" ]]; then
    url="$(PYTHONPATH="$HEART_HOME" python3 -c \
      "from heart.dashboard import PAGES_URL; print(PAGES_URL.rstrip('/') + '/board.json')" \
      2>/dev/null || true)"
  fi
  [[ -z "$url" ]] && return 0
  tmp="$(mktemp)"
  if curl -fsS --max-time 15 "$url" -o "$tmp" 2>/dev/null; then
    printf '%s' "$tmp"
  else
    rm -f "$tmp"
  fi
}

check_ci_timing_all() {
  heart_state_dir
  heart_log INFO "$(c_info "ci_timing: timing $(load_repos_yaml | wc -l) repos' gates via gh (wall-clock, PR runs included)")"
  while read -r line; do
    [[ -z "$line" ]] && continue
    local owner_name group
    owner_name="${line%% *}"
    group="${line##* }"
    time_one_repo_ci "$owner_name" "$group" &
  done < <(load_repos_yaml)
  wait

  local prev
  prev="$(_ci_timing_prev_board)"
  # The committed record (timings/gates.jsonl) is the source of truth for the
  # history; the previously published board is only the fallback for when it
  # is not there yet (first run, fresh clone, a Pages-only consumer).
  PYTHONPATH="$HEART_HOME" python3 -m heart.checks.ci_timing \
    --aggregate --prev-board "$prev" --ts "$(date -Iseconds)" \
    --record-dir "$HEART_HOME/timings" \
    --out "$HEART_STATE_DIR/ci_timing.json"
  [[ -n "$prev" ]] && rm -f "$prev"

  heart_log OK "$(c_ok "ci_timing: done")"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  check_ci_timing_all
fi
