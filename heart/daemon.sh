#!/usr/bin/env bash
# heart/daemon.sh — foreground watch loop. Calls tick.sh on a schedule.
#
# Usage: bash heart/daemon.sh [interval_seconds]
#
# Default interval: 300s (5 min). Ctrl+C exits cleanly.

set -u
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/_common.sh"

interval="${1:-${HEART_INTERVAL:-300}}"

heart_state_dir

# Pidfile guard.
if [[ -f "$HEART_PID_FILE" ]]; then
  existing="$(cat "$HEART_PID_FILE" 2>/dev/null)"
  if [[ -n "$existing" ]] && kill -0 "$existing" 2>/dev/null; then
    heart_log FAIL "$(c_fail "pyauto-heart already running with pid $existing — refusing to start")"
    heart_log INFO "$(c_meta "stop it with: pyauto-heart stop")"
    exit 1
  else
    heart_log WARN "$(c_warn "stale pidfile $HEART_PID_FILE (pid $existing not running) — overwriting")"
  fi
fi
echo $$ > "$HEART_PID_FILE"

cleanup() {
  rm -f "$HEART_PID_FILE"
  heart_log INFO "$(c_info "daemon stopped")"
}
trap cleanup EXIT INT TERM

heart_log OK "$(c_bold "PyAutoHeart daemon started")  $(c_meta "(pid $$, interval ${interval}s)")"

# A live countdown that redraws a single line each second (tty only). Falls
# back to a plain sleep when not live.
heart_countdown() {
  local secs="$1"
  if heart_is_tty; then
    local remaining
    for (( remaining = secs; remaining > 0; remaining-- )); do
      printf '\r%s' "$(c_meta "next tick in ${remaining}s …   ")"
      sleep 1
    done
    printf '\r%*s\r' 40 ''   # clear the countdown line
  else
    heart_log INFO "$(c_meta "sleeping ${interval}s until next tick…")"
    sleep "$secs"
  fi
}

while true; do
  if heart_is_tty; then
    heart_clear_screen
    printf '%s\n\n' "$(c_bold "PyAutoHeart · live")  $(c_meta "· $(date -Iseconds) · every ${interval}s · Ctrl-C to stop")"
  fi

  # Run one refresh. tick.sh streams its per-check / per-repo progress so the
  # work is visible as it happens.
  bash "$HEART_HOME/heart/tick.sh"

  # In live mode, render the dashboard after the tick (forced colour even
  # though the countdown writes to the same tty). In plain mode the streamed
  # tick output is the record; rendering again would just duplicate it.
  if heart_is_tty; then
    printf '\n'
    HEART_FORCE_COLOR=1 PYTHONPATH="$HEART_HOME" python3 -m heart.status || true
  fi

  heart_countdown "$interval"
done
