#!/usr/bin/env bash
# heart/checks/ci_status.sh — per-required-workflow CI conclusions on main HEAD.
#
# For each polled repo this fetches, via `gh api` (cheap metadata reads only):
#   1. the `main` HEAD commit sha   (`gh api .../commits/main`, falling back to
#      an anonymous `git ls-remote` — see `ci_head_sha` below)
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

# Seconds any single `git ls-remote` HEAD-sha fallback may take. The repos are
# scanned in parallel, so the worst case this adds to a tick is one timeout, not
# one per repo — but it is capped anyway so a stalled network can never eat the
# <30s budget.
_CI_LS_REMOTE_TIMEOUT="${HEART_LS_REMOTE_TIMEOUT:-10}"

# Which binary bounds that fallback. `timeout` is coreutils: Linux has it, and
# macOS has it only as `gtimeout`, and only when coreutils is installed. When
# neither exists the fallback is skipped entirely rather than run unbounded — a
# stalled `ls-remote` inside the tick is a worse failure than the empty sha that
# is already today's answer. Overridable for tests (set to "" to force the
# no-timeout path), same idiom as HEART_VALIDATION_FILE.
if [[ -n "${HEART_TIMEOUT_BIN+set}" ]]; then
  _CI_TIMEOUT_BIN="$HEART_TIMEOUT_BIN"
elif command -v timeout >/dev/null 2>&1; then
  _CI_TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  _CI_TIMEOUT_BIN="gtimeout"
else
  _CI_TIMEOUT_BIN=""
fi

# Resolve <owner/name>'s `main` HEAD sha, preferring `gh` and falling back to an
# anonymous `git ls-remote`.
#
# `gh` stays first: it is what the dev box uses, and it is the only one of the
# two that can read a private repo. The fallback exists for the mobile/cloud
# session, where `gh` is not installed at all — there the sha came back empty and
# readiness could not confirm an ingested release-validation report against the
# live HEADs, reporting "release validation source unconfirmed (current HEADs
# unknown)" even with every other gap cleared.
#
# `ls-remote` needs no authentication and no session repo attachment, so it works
# exactly where `gh` cannot. It is bounded because this runs inside the <30s
# tick: GIT_TERMINAL_PROMPT=0 stops a private repo blocking on a credential
# prompt, and `timeout` caps a stalled connection. Prints the empty string on
# failure — the same honest answer as before, never a fabricated sha.
ci_head_sha() {
  local owner_name="$1" sha=""

  if command -v gh >/dev/null 2>&1; then
    sha="$(gh api "repos/$owner_name/commits/main" --jq '.sha' 2>/dev/null || echo '')"
  fi

  if [[ -z "$sha" && -n "$_CI_TIMEOUT_BIN" ]]; then
    sha="$(GIT_TERMINAL_PROMPT=0 "$_CI_TIMEOUT_BIN" "$_CI_LS_REMOTE_TIMEOUT" \
             git ls-remote "https://github.com/$owner_name" refs/heads/main \
             2>/dev/null | awk 'NR==1 {print $1}')"
  fi

  printf '%s' "$sha"
}

check_one_repo_ci() {
  local owner_name="$1"
  local group="$2"
  local name="${owner_name##*/}"

  local runs head_sha ts err rc
  err="$(mktemp)"
  runs="$(gh api "repos/$owner_name/$_CI_RUNS_PATH" 2>"$err")"
  rc=$?
  head_sha="$(ci_head_sha "$owner_name")"
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
    --cloud-runs "$HEART_CLOUD_CI_DIR/$name.json" \
    --out "$HEART_PER_REPO_DIR/$name.ci_status.json"
}

check_ci_status_all() {
  heart_state_dir
  heart_log INFO "$(c_info "ci_status: scanning $(load_repos_yaml | wc -l) repos via gh (HEAD sha falls back to ls-remote; per-required-workflow on main HEAD)")"
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
