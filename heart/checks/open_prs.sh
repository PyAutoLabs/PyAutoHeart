#!/usr/bin/env bash
# heart/checks/open_prs.sh — open PR count + titles per repo via gh.
#
# Writes $HEART_PER_REPO_DIR/<name>.open_prs.json with one entry per
# repo. Per-line summary colours by count and PR age:
#
#   0 open                  → green
#   1+ open, all < 3 days   → cyan (just informational)
#   1+ open, any > 7 days   → yellow (stale review)
#   1+ open, any > 30 days  → red   (very stale)

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

check_one_repo_prs() {
  local owner_name="$1"
  local group="$2"
  local name="${owner_name##*/}"

  local raw count
  raw="$(gh pr list --repo "$owner_name" --state open \
        --json number,title,author,createdAt,updatedAt 2>/dev/null \
        || echo '[]')"
  count="$(echo "$raw" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")"

  # Compute max age in days for the colour decision.
  local max_age_days
  max_age_days="$(echo "$raw" | python3 -c "
import sys, json, datetime
prs = json.load(sys.stdin)
if not prs: print(0); raise SystemExit
now = datetime.datetime.now(datetime.timezone.utc)
ages = []
for pr in prs:
    ts = pr.get('createdAt', '')
    try:
        # gh returns ISO format with 'Z' suffix
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        ages.append((now - dt).days)
    except Exception:
        ages.append(0)
print(max(ages) if ages else 0)
")"

  local ts
  ts="$(date -Iseconds)"

  heart_write_json "$HEART_PER_REPO_DIR/$name.open_prs.json" "$(python3 -c "
import json
raw = '''$(echo "$raw" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)))")'''
print(json.dumps({
    'name': '$name',
    'group': '$group',
    'open_count': $count,
    'max_age_days': $max_age_days,
    'ts': '$ts',
    'prs': json.loads(raw),
}))
")"

  local glyph label
  if [[ "$count" -eq 0 ]]; then
    glyph="$(glyph_ok)"; label="$(c_ok "0 open")"
  elif [[ "$max_age_days" -ge 30 ]]; then
    glyph="$(glyph_fail)"; label="$(c_fail "$count open") $(c_meta "(oldest ${max_age_days}d)")"
  elif [[ "$max_age_days" -ge 7 ]]; then
    glyph="$(glyph_warn)"; label="$(c_warn "$count open") $(c_meta "(oldest ${max_age_days}d)")"
  else
    glyph="$(glyph_info)"; label="$(c_info "$count open") $(c_meta "(oldest ${max_age_days}d)")"
  fi
  printf '%s %s %s\n' "$glyph" "$(c_info "$name")" "$label"
}

check_open_prs_all() {
  heart_state_dir
  heart_log INFO "$(c_info "open_prs: scanning $(load_repos_yaml | wc -l) repos via gh")"
  while read -r line; do
    [[ -z "$line" ]] && continue
    local owner_name group
    owner_name="${line%% *}"
    group="${line##* }"
    check_one_repo_prs "$owner_name" "$group" &
  done < <(load_repos_yaml)
  wait
  heart_log OK "$(c_ok "open_prs: done")"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  check_open_prs_all
fi
