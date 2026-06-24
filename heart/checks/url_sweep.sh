#!/usr/bin/env bash
# heart/checks/url_sweep.sh — offline URL-hygiene sweep across the PyAuto repos.
#
# Runs heart/checks/url_check.sh (the offline forbidden-pattern regex guard) over
# every repo that used to carry its own .github/workflows/url_check.yml, and
# aggregates the results into $HEART_STATE_DIR/url_check.json:
#
#   {ts, total_findings, repos:[{repo, present, clean, findings}]}
#
# This is MONITORING ONLY — url hygiene does not gate releases, so the result is
# surfaced in `pyauto-heart status` but does NOT feed `readiness`. It runs
# on-demand (`pyauto-heart url_sweep`) and from the central url-check.yml cloud
# workflow, NOT in the <30s tick loop.
#
# Colour summary: green = all clean, yellow = forbidden patterns found.

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

CHECK_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

# The repos that previously ran url_check.yml on every PR. Centralised here.
URL_CHECK_REPOS=(
  HowToFit HowToGalaxy HowToLens
  PyAutoConf PyAutoFit PyAutoArray PyAutoGalaxy PyAutoLens
  autofit_workspace autogalaxy_workspace autolens_workspace
  euclid_strong_lens_modeling_pipeline
)

check_url_sweep() {
  heart_state_dir
  heart_log INFO "$(c_info "url_sweep: scanning ${#URL_CHECK_REPOS[@]} repos for forbidden URL patterns")"

  # Collect "repo|present|findings" rows.
  local rows=""
  local repo dir out rc findings
  for repo in "${URL_CHECK_REPOS[@]}"; do
    dir="$PYAUTO_ROOT/$repo"
    if [[ ! -d "$dir" ]]; then
      rows+="${repo}|0|0"$'\n'
      continue
    fi
    # url_check.sh exits 1 and prints matches when forbidden patterns exist.
    out="$(bash "$CHECK_DIR/url_check.sh" "$dir" 2>/dev/null)" && rc=0 || rc=1
    if [[ "$rc" -eq 0 ]]; then
      findings=0
    else
      # Count "FORBIDDEN:" banner lines (one per offending pattern).
      findings="$(printf '%s\n' "$out" | grep -c '^FORBIDDEN:' || true)"
    fi
    rows+="${repo}|1|${findings}"$'\n'
  done

  local result_json
  result_json="$(printf '%s' "$rows" | python3 -c '
import datetime, json, sys
repos, total = [], 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    repo, present, findings = line.split("|")
    findings = int(findings)
    total += findings
    repos.append({
        "repo": repo,
        "present": present == "1",
        "clean": present == "1" and findings == 0,
        "findings": findings,
    })
print(json.dumps({
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "total_findings": total,
    "repos": repos,
}, indent=2))
')"

  heart_write_json "$HEART_STATE_DIR/url_check.json" "$result_json"

  local total
  total="$(printf '%s' "$result_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total_findings"])')"
  if [[ "$total" -eq 0 ]]; then
    echo "$(glyph_ok) $(c_info 'url_sweep') $(c_ok "${#URL_CHECK_REPOS[@]} repos clean")"
  else
    local dirty
    dirty="$(printf '%s' "$result_json" | python3 -c 'import json,sys; print(sum(1 for r in json.load(sys.stdin)["repos"] if r["findings"]>0))')"
    echo "$(glyph_warn) $(c_info 'url_sweep') $(c_warn "$total forbidden URL pattern(s) in $dirty repo(s)")"
  fi
}

check_url_sweep "$@"
