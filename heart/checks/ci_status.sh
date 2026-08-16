#!/usr/bin/env bash
# heart/checks/ci_status.sh — per-required-workflow CI conclusions on main HEAD.
#
# For each polled repo this fetches, via `gh api` (cheap metadata reads only):
#   1. the `main` HEAD commit sha   (`gh api .../commits/main`)
#   2. the recent workflow runs on `main` (`gh api .../actions/runs?branch=main`)
# and pipes the runs JSON to `heart.checks.ci_status`, which picks the latest
# run of each workflow, rolls the *required* workflows for the repo's group
# (config/repos.yaml `required_workflows`) into one conclusion, writes the
# structured sidecar at $HEART_PER_REPO_DIR/<name>.ci_status.json, and prints
# the coloured one-line summary.
#
# This replaces the old `gh run list --limit 1` (newest run, ANY workflow, ANY
# branch) which could report a green url-check while smoke_tests was red. The
# heavier per-workflow detail is still just metadata — two cheap `gh` calls per
# repo, run in parallel — so the <30s tick budget holds.
#
# A repo with no runs is written with an empty conclusion (dashboard shows
# "(no runs)"). A *failed fetch* is different and is reported as such: the
# sidecar records `status="unavailable"` plus the `gh` stderr, so a broken query
# never masquerades as "CI still in progress". Either way the tick degrades
# gracefully rather than failing.
#
# The runs are read with `gh api` rather than `gh run list` on purpose.
# `gh run list`'s surface has moved between releases — `--branch` only exists
# from gh 2.9, and the `--json` field names changed (`workflowName` is newer
# than `name`) — so on an older `gh` the whole call exited non-zero, the old
# `|| echo '[]'` swallowed it, and every repo silently rendered as "CI
# in_progress". `gh api` + the REST endpoint is stable across every `gh` that
# has `gh api` at all, which is the same call already used for the HEAD sha.

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

# Recent runs on main, newest first. `per_page=30` mirrors the old --limit 30.
_CI_RUNS_PATH="actions/runs?branch=main&per_page=30&exclude_pull_requests=true"

check_one_repo_ci() {
  local owner_name="$1"
  local group="$2"
  local name="${owner_name##*/}"

  local runs head_sha ts err rc
  err="$(mktemp)"
  runs="$(gh api "repos/$owner_name/$_CI_RUNS_PATH" 2>"$err")"
  rc=$?
  head_sha="$(gh api "repos/$owner_name/commits/main" --jq '.sha' 2>/dev/null || echo '')"
  ts="$(date -Iseconds)"

  local fetch_error=""
  if [[ $rc -ne 0 || -z "$runs" ]]; then
    # Collapse gh's stderr to one line so it fits the sidecar and the log.
    fetch_error="$(tr '\n' ' ' <"$err" | cut -c1-200)"
    [[ -z "${fetch_error// }" ]] && fetch_error="gh api exited $rc"
    runs='{}'
  fi
  rm -f "$err"

  printf '%s' "$runs" | PYTHONPATH="$HEART_HOME" python3 -m heart.checks.ci_status \
    --name "$name" --group "$group" --head-sha "$head_sha" --ts "$ts" \
    --fetch-error "$fetch_error" \
    --out "$HEART_PER_REPO_DIR/$name.ci_status.json"
}

check_ci_status_all() {
  heart_state_dir
  heart_log INFO "$(c_info "ci_status: scanning $(load_repos_yaml | wc -l) repos via gh (per-required-workflow on main HEAD)")"
  while read -r line; do
    [[ -z "$line" ]] && continue
    local owner_name group
    owner_name="${line%% *}"
    group="${line##* }"
    check_one_repo_ci "$owner_name" "$group" &
  done < <(load_repos_yaml)
  wait
  heart_log OK "$(c_ok "ci_status: done")"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  check_ci_status_all
fi
