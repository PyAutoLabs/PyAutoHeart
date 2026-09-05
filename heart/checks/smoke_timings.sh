#!/usr/bin/env bash
# heart/checks/smoke_timings.sh — per-script CI timings (the fine-grained speed leg).
#
# For each polled repo whose required gate is the smoke suite, this lists the
# repo's artifacts via `gh api`, asks `heart.checks.smoke_timings --plan` which
# of them are the PR gate's `smoke-timings-<py>` legs, downloads and unzips
# those, and hands the extracted directories to the per-repo Python call, which
# parses the `smoke_timings/1` datasets inside and writes
# $HEART_PER_REPO_DIR/<name>.smoke_timings.json. A second, aggregate pass folds
# every sidecar plus the PREVIOUSLY PUBLISHED board.json into the global rollup
# at $HEART_STATE_DIR/smoke_timings.json (per-repo/per-leg totals, every timed
# row, run-to-run slowdowns, TIMEOUT events).
#
# Only the groups whose required gate is "Smoke Tests" are polled: a library has
# no smoke-timings artifact, and asking for one would manufacture a repo full of
# false "unavailable" rows.
#
# The previous observation is *self-carrying*, exactly as in ci_timing.sh: the
# aggregate step re-reads the board.json this repo published yesterday and
# compares each script against its own last row, keyed by run id so a re-render
# of the same run never compares it against itself. HEART_PREV_BOARD_URL
# overrides the URL (tests, staging boards); any fetch failure degrades to "no
# comparison", never to an error.
#
# Observer boundary: everything written lives under $HEART_STATE_DIR (plus
# mktemp scratch, cleaned up per repo). A repo whose listing fetch fails records
# the reason and NO legs — "we could not ask" must never render as "all quiet".

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

# The groups whose required workflow is the smoke gate — the only repos that
# publish a `smoke-timings-<py>` artifact at all.
_SMOKE_GROUPS="workspaces workspaces_test howto"

# `per_page=100` is one page of artifacts, newest first: comfortably more than
# the handful of live `smoke-timings-*` legs, and the selector takes the newest
# non-expired one per name anyway.
_SMOKE_ARTIFACTS_PATH="actions/artifacts?per_page=100"

ingest_one_repo() {
  local owner_name="$1"
  local group="$2"
  local name="${owner_name##*/}"
  local owner="${owner_name%%/*}"

  local listing downloads err ts rc
  err="$(mktemp)"
  listing="$(mktemp)"
  gh api "repos/$owner_name/$_SMOKE_ARTIFACTS_PATH" >"$listing" 2>"$err"
  rc=$?
  ts="$(date -Iseconds)"

  if [[ $rc -ne 0 || ! -s "$listing" ]]; then
    # Collapse gh's stderr to one line so it fits the sidecar and the log.
    local fetch_error
    fetch_error="$(tr '\n' ' ' <"$err" | cut -c1-200)"
    [[ -z "${fetch_error// }" ]] && fetch_error="gh api exited $rc"
    rm -f "$err" "$listing"
    PYTHONPATH="$HEART_HOME" python3 -m heart.checks.smoke_timings \
      --name "$name" --group "$group" --owner "$owner" --ts "$ts" \
      --fetch-error "$fetch_error" \
      --out "$HEART_PER_REPO_DIR/$name.smoke_timings.json"
    return 0
  fi
  rm -f "$err"

  # The selection rule (newest non-expired artifact per python leg; the weekly
  # sweep's names deliberately excluded) lives in Python, never re-derived here.
  local plan
  plan="$(PYTHONPATH="$HEART_HOME" python3 -m heart.checks.smoke_timings --plan <"$listing")"

  downloads="$(mktemp -d)"
  local id artifact_name
  while read -r id artifact_name; do
    [[ -z "$id" ]] && continue
    err="$(mktemp)"
    # KNOWN RISK: the repo-scoped GITHUB_TOKEN may be refused (403) by the /zip
    # endpoint for ANOTHER repo's artifacts. The remedy is a secret, not a code
    # change — HEART_TIMINGS_TOKEN, a fine-grained PAT with Actions: read on the
    # workspace repos (see .github/workflows/heart-health.yml). Either way one
    # leg's failure is recorded as that leg's error and must not lose the repo's
    # other leg, hence the `.error` marker and the `continue`.
    if gh api "repos/$owner_name/actions/artifacts/$id/zip" >"$downloads/$id.zip" 2>"$err" \
       && unzip -o -q "$downloads/$id.zip" -d "$downloads/$id" 2>>"$err"; then
      :
    else
      local reason
      reason="$(tr '\n' ' ' <"$err" | cut -c1-200)"
      [[ -z "${reason// }" ]] && reason="download of $artifact_name failed"
      printf '%s\n' "$reason" >"$downloads/$id.error"
    fi
    rm -f "$err" "$downloads/$id.zip"
  done <<<"$plan"

  PYTHONPATH="$HEART_HOME" python3 -m heart.checks.smoke_timings \
    --name "$name" --group "$group" --owner "$owner" --ts "$ts" \
    --listing "$listing" --downloads "$downloads" \
    --out "$HEART_PER_REPO_DIR/$name.smoke_timings.json"

  rm -rf "$downloads"
  rm -f "$listing"
}

# Print the path of a tempfile holding the previously published board.json, or
# nothing at all when it cannot be had. Never fails the check.
#
# A near-copy of ci_timing.sh's `_ci_timing_prev_board`: sourcing that file to
# reuse it would run its own top-level setup as a side effect, so the twenty
# lines are duplicated deliberately rather than the two checks being coupled.
_smoke_timings_prev_board() {
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

check_smoke_timings_all() {
  heart_state_dir
  heart_log INFO "$(c_info "smoke_timings: ingesting per-script smoke-timings artifacts across the smoke-gated repos")"
  while read -r line; do
    [[ -z "$line" ]] && continue
    local owner_name group
    owner_name="${line%% *}"
    group="${line##* }"
    case " $_SMOKE_GROUPS " in
      *" $group "*) ingest_one_repo "$owner_name" "$group" & ;;
      *) continue ;;
    esac
  done < <(load_repos_yaml)
  wait

  local prev
  prev="$(_smoke_timings_prev_board)"
  PYTHONPATH="$HEART_HOME" python3 -m heart.checks.smoke_timings \
    --aggregate --prev-board "$prev" --ts "$(date -Iseconds)" \
    --out "$HEART_STATE_DIR/smoke_timings.json"
  [[ -n "$prev" ]] && rm -f "$prev"

  heart_log OK "$(c_ok "smoke_timings: done")"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  check_smoke_timings_all
fi
