#!/usr/bin/env bash
# heart/checks/worktree_drift.sh — PyAutoLabs-wt/ dirs vs active.md claims.
#
# Three signals:
#   ORPHAN   dirs on disk under $PYAUTO_WT_ROOT that are NOT in active.md
#   MISSING  active.md entries whose worktree dir does NOT exist on disk
#   DIRTY    any worktree on disk that has uncommitted changes
#
# Writes $HEART_STATE_DIR/worktree_drift.json with the categorised lists.
# Colour summary:
#   no drift          → green
#   only clean orphans → yellow
#   dirty orphans / missing claimed → red

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../_common.sh"

PYAUTO_WT_ROOT="${PYAUTO_WT_ROOT:-$HOME/Code/PyAutoLabs-wt}"
ACTIVE_MD="$PYAUTO_ROOT/PyAutoPrompt/active.md"

check_worktree_drift() {
  heart_state_dir
  heart_log INFO "$(c_info "worktree_drift: scanning $PYAUTO_WT_ROOT vs $ACTIVE_MD")"

  local result_json
  result_json="$(python3 - "$PYAUTO_WT_ROOT" "$ACTIVE_MD" <<'PY'
import json
import os
import re
import subprocess
import sys
from pathlib import Path

wt_root = Path(sys.argv[1])
active_md = Path(sys.argv[2])

# 1. Worktree dirs on disk.
on_disk = []
if wt_root.is_dir():
    for entry in sorted(wt_root.iterdir()):
        if not entry.is_dir():
            continue
        # Empty stubs vs real worktrees (real worktrees contain at least
        # one symlink or subdir with .git).
        has_content = any(
            child.is_symlink() or (child.is_dir() and (child / ".git").exists())
            for child in entry.iterdir()
        )
        on_disk.append({
            "name": entry.name,
            "path": str(entry),
            "has_real_worktrees": has_content,
        })

# 2. Active.md claimed worktrees.
claimed = []
if active_md.is_file():
    text = active_md.read_text()
    current_task = None
    for line in text.splitlines():
        m = re.match(r"^## (\S+)", line)
        if m:
            current_task = m.group(1)
        elif current_task:
            wm = re.search(r"worktree:\s*(\S+)", line)
            if wm:
                # Expand ~ if present.
                path = os.path.expanduser(wm.group(1))
                claimed.append({"task": current_task, "path": path})

# 3. Categorise.
claimed_paths = {c["path"] for c in claimed}
on_disk_paths = {d["path"] for d in on_disk}

orphans = [d for d in on_disk if d["path"] not in claimed_paths]
missing = [c for c in claimed if c["path"] not in on_disk_paths]

# 4. Check each on-disk worktree for dirtiness.
dirty = []
for entry in on_disk:
    if not entry["has_real_worktrees"]:
        continue
    for child in Path(entry["path"]).iterdir():
        if child.is_dir() and (child / ".git").exists():
            try:
                res = subprocess.run(
                    ["git", "-C", str(child), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=10,
                )
                if res.stdout.strip():
                    dirty.append({
                        "worktree": entry["name"],
                        "repo": child.name,
                        "dirty_files": len(res.stdout.strip().splitlines()),
                    })
            except Exception:
                pass

print(json.dumps({
    "on_disk_count": len(on_disk),
    "claimed_count": len(claimed),
    "orphans": orphans,
    "missing": missing,
    "dirty": dirty,
}))
PY
)"

  heart_write_json "$HEART_STATE_DIR/worktree_drift.json" "$result_json"

  local orphan_n missing_n dirty_n
  orphan_n="$(echo "$result_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['orphans']))")"
  missing_n="$(echo "$result_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['missing']))")"
  dirty_n="$(echo "$result_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['dirty']))")"

  local glyph label
  if [[ "$orphan_n" -eq 0 && "$missing_n" -eq 0 && "$dirty_n" -eq 0 ]]; then
    glyph="$(glyph_ok)"; label="$(c_ok "no drift")"
  elif [[ "$dirty_n" -gt 0 || "$missing_n" -gt 0 ]]; then
    glyph="$(glyph_fail)"; label="$(c_fail "drift: ${orphan_n} orphan / ${missing_n} missing / ${dirty_n} dirty")"
  else
    glyph="$(glyph_warn)"; label="$(c_warn "drift: ${orphan_n} orphan dir(s) (clean)")"
  fi
  printf '%s %s %s\n' "$glyph" "$(c_info worktrees)" "$label"
  heart_log OK "$(c_ok "worktree_drift: done")"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  check_worktree_drift
fi
