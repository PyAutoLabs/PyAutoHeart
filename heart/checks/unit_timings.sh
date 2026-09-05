#!/usr/bin/env bash
# heart/checks/unit_timings.sh — per-test + per-import CI timings (the unit leg).
#
# The twin of smoke_timings.sh, pointed at the LIBRARIES. For each polled
# library this lists the repo's artifacts via `gh api`, asks
# `heart.checks.unit_timings --plan` which of them are the Tests gate's
# `unit-timings-<py>` legs, downloads and unzips those, and hands the extracted
# directories to the per-repo Python call, which parses the junit XML (per-test
# durations) and the `import_time/1` dataset inside and writes
# $HEART_PER_REPO_DIR/<name>.unit_timings.json. A second, aggregate pass folds
# every sidecar into the global rollup at $HEART_STATE_DIR/unit_timings.json and
# writes the two LEGACY summaries the board's "Unit-test timing" and "Import
# timing" sections already read ($HEART_STATE_DIR/unit_test_timing.json and
# import_time.json) — the same files the dev-box checks write, so those rows
# come alive without either section changing shape.
#
# Only the `libraries` group is polled: a workspace runs no unit suite and
# publishes no `unit-timings-<py>` artifact, and asking for one would manufacture
# a repo full of false "unavailable" rows.
#
# NO PREVIOUS-BOARD FETCH, deliberately, and this is the one structural
# difference from smoke_timings.sh: the committed record
# (timings/unit/<repo>.jsonl) is the ONLY baseline here. The published board
# never carried unit rows, so there is nothing to fall back to and nothing to
# fetch — with no record yet every test row is `ok` and every import is
# `building`, which is the truth on day one.
#
# Observer boundary: everything written lives under $HEART_STATE_DIR (plus
# mktemp scratch, cleaned up per repo). A repo whose listing fetch fails records
# the reason and NO legs — "we could not ask" must never render as "all quiet".

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

# The group whose required workflow is the libraries' Tests gate — the only
# repos that publish a `unit-timings-<py>` artifact at all.
_UNIT_GROUPS="libraries"

# `per_page=100` is one page of artifacts, newest first: comfortably more than
# the handful of live `unit-timings-*` legs, and the selector takes the newest
# non-expired one per name anyway.
_UNIT_ARTIFACTS_PATH="actions/artifacts?per_page=100"

ingest_one_repo() {
  local owner_name="$1"
  local group="$2"
  local name="${owner_name##*/}"
  local owner="${owner_name%%/*}"

  local listing downloads err ts rc
  err="$(mktemp)"
  listing="$(mktemp)"
  gh api "repos/$owner_name/$_UNIT_ARTIFACTS_PATH" >"$listing" 2>"$err"
  rc=$?
  ts="$(date -Iseconds)"

  if [[ $rc -ne 0 || ! -s "$listing" ]]; then
    # Collapse gh's stderr to one line so it fits the sidecar and the log.
    local fetch_error
    fetch_error="$(tr '\n' ' ' <"$err" | cut -c1-200)"
    [[ -z "${fetch_error// }" ]] && fetch_error="gh api exited $rc"
    rm -f "$err" "$listing"
    PYTHONPATH="$HEART_HOME" python3 -m heart.checks.unit_timings \
      --name "$name" --group "$group" --owner "$owner" --ts "$ts" \
      --fetch-error "$fetch_error" \
      --out "$HEART_PER_REPO_DIR/$name.unit_timings.json"
    return 0
  fi
  rm -f "$err"

  # The selection rule (newest non-expired artifact per python leg) lives in
  # Python, never re-derived here.
  local plan
  plan="$(PYTHONPATH="$HEART_HOME" python3 -m heart.checks.unit_timings --plan <"$listing")"

  downloads="$(mktemp -d)"
  local id artifact_name
  while read -r id artifact_name; do
    [[ -z "$id" ]] && continue
    err="$(mktemp)"
    # KNOWN RISK: the repo-scoped GITHUB_TOKEN may be refused (403) by the /zip
    # endpoint for ANOTHER repo's artifacts. The remedy is a secret, not a code
    # change — HEART_TIMINGS_TOKEN, a fine-grained PAT with Actions: read on the
    # library repos (see .github/workflows/heart-health.yml). Either way one
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

  PYTHONPATH="$HEART_HOME" python3 -m heart.checks.unit_timings \
    --name "$name" --group "$group" --owner "$owner" --ts "$ts" \
    --listing "$listing" --downloads "$downloads" \
    --out "$HEART_PER_REPO_DIR/$name.unit_timings.json"

  rm -rf "$downloads"
  rm -f "$listing"
}

check_unit_timings_all() {
  heart_state_dir
  heart_log INFO "$(c_info "unit_timings: ingesting per-test unit-timings artifacts across the libraries")"
  while read -r line; do
    [[ -z "$line" ]] && continue
    local owner_name group
    owner_name="${line%% *}"
    group="${line##* }"
    case " $_UNIT_GROUPS " in
      *" $group "*) ingest_one_repo "$owner_name" "$group" & ;;
      *) continue ;;
    esac
  done < <(load_repos_yaml)
  wait

  # The committed record (timings/unit/<repo>.jsonl) is the only baseline:
  # previous test seconds per leg, and the import window whose median every
  # import ratio is taken against.
  PYTHONPATH="$HEART_HOME" python3 -m heart.checks.unit_timings \
    --aggregate --ts "$(date -Iseconds)" \
    --record-dir "$HEART_HOME/timings" \
    --out "$HEART_STATE_DIR/unit_timings.json"

  heart_log OK "$(c_ok "unit_timings: done")"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  check_unit_timings_all
fi
