#!/usr/bin/env bash
# heart/_color.sh — ANSI color helpers honouring NO_COLOR and --no-color.
#
# Source this file (don't execute). After sourcing, call the named
# functions with the text to colour:
#
#     source heart/_color.sh
#     echo "$(c_green "PASS") build is green"
#     echo "$(c_red "FAIL") ci failed on PyAutoFit"
#
# Honours:
#   NO_COLOR     (any non-empty value → strip colors)
#   HEART_NO_COLOR (idem; explicit heart override)
#   HEART_FORCE_COLOR (force colors even if stdout is not a tty)
#
# If stdout is not a tty AND neither HEART_FORCE_COLOR is set, colors
# are stripped. This keeps redirection to files / pipes clean.

_heart_colors_enabled() {
  if [[ -n "${HEART_FORCE_COLOR:-${PULSE_FORCE_COLOR:-}}" ]]; then
    return 0
  fi
  if [[ -n "${NO_COLOR:-}" || -n "${HEART_NO_COLOR:-${PULSE_NO_COLOR:-}}" ]]; then
    return 1
  fi
  if [[ ! -t 1 ]]; then
    return 1
  fi
  return 0
}

c_reset()  { _heart_colors_enabled && printf '\033[0m'    || true; }
c_dim()    { _heart_colors_enabled && printf '\033[2m%s\033[0m'   "$*" || printf '%s' "$*"; }
c_bold()   { _heart_colors_enabled && printf '\033[1m%s\033[0m'   "$*" || printf '%s' "$*"; }

c_green()  { _heart_colors_enabled && printf '\033[32m%s\033[0m'  "$*" || printf '%s' "$*"; }
c_yellow() { _heart_colors_enabled && printf '\033[33m%s\033[0m'  "$*" || printf '%s' "$*"; }
c_red()    { _heart_colors_enabled && printf '\033[31m%s\033[0m'  "$*" || printf '%s' "$*"; }
c_blue()   { _heart_colors_enabled && printf '\033[34m%s\033[0m'  "$*" || printf '%s' "$*"; }
c_cyan()   { _heart_colors_enabled && printf '\033[36m%s\033[0m'  "$*" || printf '%s' "$*"; }
c_magenta(){ _heart_colors_enabled && printf '\033[35m%s\033[0m'  "$*" || printf '%s' "$*"; }

# Semantic shortcuts — match the plan's colour convention.
c_ok()     { c_green   "$*"; }   # passing / clean / nominal
c_warn()   { c_yellow  "$*"; }   # warning / stale / mild drift
c_fail()   { c_red     "$*"; }   # failing / actionable now
c_info()   { c_cyan    "$*"; }   # informational headers
c_meta()   { c_dim     "$*"; }   # secondary info (counts, timestamps)

# Status glyphs — single-character indicators for table cells.
glyph_ok()   { c_ok   "✓"; }
glyph_warn() { c_warn "!"; }
glyph_fail() { c_fail "✗"; }
glyph_info() { c_info "•"; }
