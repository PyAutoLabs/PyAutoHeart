#!/usr/bin/env bash
# heart/checks/no_run_census.sh — census of every workspace's skipped scripts.
#
# For each polled workspace / workspace_test / HowTo repo this fetches
# `config/build/no_run.yaml` via the contents API (one cheap call per repo) and
# pipes the RAW YAML TEXT to `heart.checks.no_run_census`, which parses it
# line-based — the marker tiers live in `#` comments a YAML load would discard,
# and a bare `- off` entry parses as a YAML boolean (a recorded crash) — and
# writes $HEART_PER_REPO_DIR/<name>.no_run_census.json. A second, aggregate pass
# folds the sidecars into $HEART_STATE_DIR/no_run_census.json.
#
# A repo with no `no_run.yaml` is passed `--missing` and recorded as
# `present: false`. That is honest data, not an error: at least one
# workspace_test repo genuinely has no such file, and inventing an empty census
# for it would read as "nothing skipped here".
#
# Only the groups that HAVE a no_run.yaml are polled (workspaces,
# workspaces_test, howto) — asking a library for one would manufacture a repo
# full of false "missing" rows.
#
# Observer boundary: everything written lives under $HEART_STATE_DIR.

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

_NO_RUN_PATH="contents/config/build/no_run.yaml"
_NO_RUN_GROUPS="workspaces workspaces_test howto"

census_one_repo() {
  local owner_name="$1"
  local group="$2"
  local name="${owner_name##*/}"

  local raw body ts err rc
  err="$(mktemp)"
  # `--jq .content` yields base64 wrapped at 60 columns; strip the newlines
  # before decoding so every base64 implementation accepts it. The decode is a
  # separate step so `rc` is *gh's* exit status, not base64's.
  raw="$(gh api "repos/$owner_name/$_NO_RUN_PATH" --jq '.content' 2>"$err")"
  rc=$?
  body=""
  if [[ $rc -eq 0 && -n "$raw" ]]; then
    body="$(printf '%s' "$raw" | tr -d '\n' | base64 -d 2>/dev/null || true)"
  fi
  ts="$(date -Iseconds)"

  if [[ $rc -ne 0 || -z "$body" ]]; then
    # A 404 is the expected "this repo has none"; anything else is a query we
    # could not make, which is worth a log line even though the census row is
    # the same honest `present: false`.
    if ! grep -qi '404\|not found' "$err"; then
      heart_log WARN "$(c_warn "no_run_census: $name fetch failed: $(tr '\n' ' ' <"$err" | cut -c1-160)")"
    fi
    rm -f "$err"
    PYTHONPATH="$HEART_HOME" python3 -m heart.checks.no_run_census \
      --name "$name" --group "$group" --ts "$ts" --missing \
      --out "$HEART_PER_REPO_DIR/$name.no_run_census.json" </dev/null
    return 0
  fi
  rm -f "$err"

  printf '%s' "$body" | PYTHONPATH="$HEART_HOME" python3 -m heart.checks.no_run_census \
    --name "$name" --group "$group" --ts "$ts" \
    --out "$HEART_PER_REPO_DIR/$name.no_run_census.json"
}

check_no_run_census_all() {
  heart_state_dir
  heart_log INFO "$(c_info "no_run_census: reading config/build/no_run.yaml across the workspace repos")"
  while read -r line; do
    [[ -z "$line" ]] && continue
    local owner_name group
    owner_name="${line%% *}"
    group="${line##* }"
    case " $_NO_RUN_GROUPS " in
      *" $group "*) census_one_repo "$owner_name" "$group" & ;;
      *) continue ;;
    esac
  done < <(load_repos_yaml)
  wait

  PYTHONPATH="$HEART_HOME" python3 -m heart.checks.no_run_census \
    --aggregate --ts "$(date -Iseconds)" \
    --out "$HEART_STATE_DIR/no_run_census.json"

  heart_log OK "$(c_ok "no_run_census: done")"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  check_no_run_census_all
fi
