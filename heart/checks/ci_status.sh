#!/usr/bin/env bash
# heart/checks/ci_status.sh — latest CI run conclusion per repo via gh.
#
# For each polled repo, fetches the most recent workflow run on main
# (any workflow) and writes a small JSON sidecar at
# $HEART_PER_REPO_DIR/<name>.ci_status.json. Colours the one-line
# summary by latest conclusion:
#
#   success    → green
#   failure    → red
#   in_progress → yellow
#   skipped/cancelled → yellow
#   <empty>    → dim (no runs at all)

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

check_one_repo_ci() {
  local owner_name="$1"
  local group="$2"
  local name="${owner_name##*/}"

  local raw status conclusion sha created_at workflow url
  raw="$(gh run list --repo "$owner_name" --limit 1 \
        --json status,conclusion,headSha,createdAt,name,url 2>/dev/null \
        || echo '[]')"

  status="$(echo "$raw" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r[0].get('status','') if r else '')")"
  conclusion="$(echo "$raw" | python3 -c "import sys,json; r=json.load(sys.stdin); print((r[0].get('conclusion') or '') if r else '')")"
  sha="$(echo "$raw" | python3 -c "import sys,json; r=json.load(sys.stdin); print((r[0].get('headSha','')[:7]) if r else '')")"
  created_at="$(echo "$raw" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r[0].get('createdAt','') if r else '')")"
  workflow="$(echo "$raw" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r[0].get('name','') if r else '')")"
  url="$(echo "$raw" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r[0].get('url','') if r else '')")"

  local ts
  ts="$(date -Iseconds)"

  heart_write_json "$HEART_PER_REPO_DIR/$name.ci_status.json" "$(python3 -c "
import json
print(json.dumps({
    'name': '$name',
    'group': '$group',
    'status': '$status',
    'conclusion': '$conclusion',
    'sha': '$sha',
    'created_at': '$created_at',
    'workflow': '$workflow',
    'url': '$url',
    'ts': '$ts',
}))
")"

  local glyph label
  if [[ -z "$status" ]]; then
    glyph="$(c_meta '·')"; label="$(c_meta '(no runs)')"
  elif [[ "$conclusion" == "success" ]]; then
    glyph="$(glyph_ok)"; label="$(c_ok success) $(c_meta "($sha)")"
  elif [[ "$conclusion" == "failure" ]]; then
    glyph="$(glyph_fail)"; label="$(c_fail FAILURE) $(c_meta "$workflow @ $sha")"
  elif [[ "$status" == "in_progress" || "$status" == "queued" ]]; then
    glyph="$(glyph_warn)"; label="$(c_warn "$status") $(c_meta "($sha)")"
  else
    glyph="$(glyph_warn)"; label="$(c_warn "$conclusion") $(c_meta "($sha)")"
  fi
  printf '%s %s %s\n' "$glyph" "$(c_info "$name")" "$label"
}

check_ci_status_all() {
  heart_state_dir
  heart_log INFO "$(c_info "ci_status: scanning $(load_repos_yaml | wc -l) repos via gh")"
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
