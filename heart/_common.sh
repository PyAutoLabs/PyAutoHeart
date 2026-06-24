#!/usr/bin/env bash
# heart/_common.sh — shared helpers + globals for every Heart module.
#
# Source this file. It sets:
#
#   HEART_HOME           — repo root (PyAutoHeart/)
#   HEART_STATE_DIR      — ~/.pyauto-heart/ (created on first call)
#   HEART_PER_REPO_DIR   — per-repo JSON caches
#   HEART_TIMINGS_DIR    — rolling script timing baselines
#   HEART_LOG_DIR        — daemon log files
#   HEART_PID_FILE       — daemon pidfile
#   HEART_STATE_FILE     — aggregated latest snapshot
#   PYAUTO_ROOT          — ~/Code/PyAutoLabs/ (where the repos live)
#
# And the helper functions:
#
#   heart_log <level> <msg>      — timestamped log line to stdout and HEART_LOG_DIR/heart.log
#   heart_state_dir              — ensure state dir tree exists
#   load_repos_yaml              — print the list of polled repos as `owner/name` lines
#   load_repos_by_group <group>  — print just one group's repos
#   heart_with_lock <name> <cmd> — flock-style serialisation for state writes

set -u   # nounset; tolerate unset only in explicit checks via ${VAR:-}

# Determine HEART_HOME by walking up from this file.
_heart_common_self="${BASH_SOURCE[0]}"
HEART_HOME="$(cd "$(dirname "$_heart_common_self")/.." && pwd)"
export HEART_HOME

PYAUTO_ROOT="${PYAUTO_ROOT:-$HOME/Code/PyAutoLabs}"
export PYAUTO_ROOT

HEART_STATE_DIR="${HEART_STATE_DIR:-${PULSE_STATE_DIR:-$HOME/.pyauto-heart}}"
HEART_PER_REPO_DIR="$HEART_STATE_DIR/per-repo"
HEART_TIMINGS_DIR="$HEART_STATE_DIR/timings"
HEART_LOG_DIR="$HEART_STATE_DIR/logs"
HEART_PID_FILE="$HEART_STATE_DIR/heart.pid"
HEART_STATE_FILE="$HEART_STATE_DIR/state.json"
HEART_TICK_LOG="$HEART_LOG_DIR/heart.log"
export HEART_STATE_DIR HEART_PER_REPO_DIR HEART_TIMINGS_DIR HEART_LOG_DIR
export HEART_PID_FILE HEART_STATE_FILE HEART_TICK_LOG

# Backwards-compatible environment names for existing automation.
PULSE_HOME="$HEART_HOME"
PULSE_STATE_DIR="$HEART_STATE_DIR"
PULSE_PER_REPO_DIR="$HEART_PER_REPO_DIR"
PULSE_TIMINGS_DIR="$HEART_TIMINGS_DIR"
PULSE_LOG_DIR="$HEART_LOG_DIR"
PULSE_PID_FILE="$HEART_PID_FILE"
PULSE_STATE_FILE="$HEART_STATE_FILE"
PULSE_TICK_LOG="$HEART_TICK_LOG"
export PULSE_HOME PULSE_STATE_DIR PULSE_PER_REPO_DIR PULSE_TIMINGS_DIR
export PULSE_LOG_DIR PULSE_PID_FILE PULSE_STATE_FILE PULSE_TICK_LOG

source "$HEART_HOME/heart/_color.sh"

heart_state_dir() {
  mkdir -p "$HEART_STATE_DIR" "$HEART_PER_REPO_DIR" "$HEART_TIMINGS_DIR" "$HEART_LOG_DIR"
}

# Live-mode detection. Live (clear-screen, redraw, countdown) when stdout is a
# terminal; plain (streamed, append-only) otherwise — e.g. when an agent runs
# the daemon or output is piped to a file. Override with HEART_LIVE=1 (force
# live) or HEART_LIVE=0 (force plain).
heart_is_tty() {
  case "${HEART_LIVE:-}" in
    1) return 0 ;;
    0) return 1 ;;
  esac
  [ -t 1 ]
}

# Clear screen + home cursor (only meaningful on a tty).
heart_clear_screen() {
  printf '\033[H\033[2J'
}

# Log to stdout (coloured by level) and to HEART_TICK_LOG (plain).
heart_log() {
  local level="$1"; shift
  local msg="$*"
  local ts
  ts="$(date -Iseconds)"

  local stdout_line stderr_line
  case "$level" in
    DEBUG) stdout_line="$(c_meta "[$ts] DEBUG") $msg" ;;
    INFO)  stdout_line="$(c_info "[$ts] INFO ") $msg" ;;
    OK)    stdout_line="$(c_ok   "[$ts] OK   ") $msg" ;;
    WARN)  stdout_line="$(c_warn "[$ts] WARN ") $msg" ;;
    FAIL)  stdout_line="$(c_fail "[$ts] FAIL ") $msg" ;;
    *)     stdout_line="[$ts] $level $msg" ;;
  esac

  printf '%s\n' "$stdout_line"
  heart_state_dir
  printf '[%s] %s %s\n' "$ts" "$level" "$msg" >> "$HEART_TICK_LOG"
}

# Print "owner/name group" for every polled repo, one per line.
# Uses Python because parsing YAML in bash is fragile.
load_repos_yaml() {
  local config="${1:-$HEART_HOME/config/repos.yaml}"
  python3 - "$config" <<'PY'
import sys
import yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f)
for group, entries in data.get("repos", {}).items():
    for repo in entries:
        print(f"{repo['owner']}/{repo['name']} {group}")
PY
}

# Same as load_repos_yaml but for a single group only.
load_repos_by_group() {
  local group="$1"
  local config="${2:-$HEART_HOME/config/repos.yaml}"
  load_repos_yaml "$config" | awk -v g="$group" '$2 == g {print $1}'
}

# Simple file-based lock for state cache writes. Usage:
#
#   heart_with_lock state '
#     ... commands that write $HEART_STATE_FILE ...
#   '
heart_with_lock() {
  local name="$1"; shift
  heart_state_dir
  local lock="$HEART_STATE_DIR/$name.lock"
  (
    flock -x 200
    eval "$@"
  ) 200> "$lock"
}

# Atomic JSON write: write to a tempfile, then rename.
heart_write_json() {
  local target="$1"
  local content="$2"
  local tmp
  tmp="$(mktemp "${target}.XXXXXX.tmp")"
  printf '%s\n' "$content" > "$tmp"
  mv "$tmp" "$target"
}

# Backwards-compatible function names for old sourced scripts.
pulse_state_dir() { heart_state_dir "$@"; }
pulse_is_tty() { heart_is_tty "$@"; }
pulse_clear_screen() { heart_clear_screen "$@"; }
pulse_log() { heart_log "$@"; }
pulse_with_lock() { heart_with_lock "$@"; }
pulse_write_json() { heart_write_json "$@"; }
