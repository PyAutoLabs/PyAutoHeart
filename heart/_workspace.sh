#!/usr/bin/env bash
# heart/_workspace.sh — where the workspace root is, asked in one place.
#
# The shell half of heart/_workspace.py, and the same contract: the answer
# belongs to the organism, so it is asked of PyAutoBrain/bin/_pyauto_root.sh
# whenever a Brain checkout is in reach, and only applied locally when it is
# not. Sourcing this sets, exactly as the Brain's file does:
#
#   PYAUTO_ROOT          — the directory holding the organ checkouts
#   PYAUTO_ROOT_REASON   — the rule that produced it (say so when degrading)
#   PYAUTO_ROOT_MARKER   — the marker filename (.pyauto-root)
#   PYAUTO_WT_ROOT       — where task worktrees live
#
# plus one of the Heart's own, because "where am I" and "which tree do I grade"
# are different questions:
#
#   PYAUTO_MAIN_ROOT     — the MAIN checkout: the tree health grades. Never a
#                          task bundle. A bundle's repos are git worktrees OF
#                          the canonical repos, so git names the main checkout
#                          (`rev-parse --git-common-dir`) whatever the cwd.
#   PYAUTO_MAIN_ROOT_REASON — the rule that produced it
#
# Health is a release gate, and grading a branch is an opt-in: $PYAUTO_ROOT
# wins over both. Without the split, running the Heart from the bundle you
# happen to be working in graded the bundle — the more flattering answer,
# reached by cwd alone.
#
# An explicit PYAUTO_ROOT / PYAUTO_WT_ROOT in the environment still wins: this
# replaces the DEFAULT — which used to be a path spelled out under $HOME,
# right on one developer's box and nowhere else — never the override.

if [ -z "${_HEART_WORKSPACE_SOURCED:-}" ]; then
_HEART_WORKSPACE_SOURCED=1

_heart_workspace_home="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"

# Whether the operator named a root, recorded BEFORE the resolver runs: after
# it, PYAUTO_ROOT is always set and the two cases are indistinguishable.
_heart_workspace_env_root="${PYAUTO_ROOT:-}"

# The Brain's resolver, if a checkout is in reach. Candidate order mirrors
# dashboard.theme(): $PYAUTO_BRAIN, then beside this checkout (where CI puts
# it), then vendored inside it.
_heart_workspace_shared=""
for _heart_cand in "${PYAUTO_BRAIN:-}" \
                   "$_heart_workspace_home/../PyAutoBrain" \
                   "$_heart_workspace_home/PyAutoBrain"; do
  [ -n "$_heart_cand" ] || continue
  if [ -f "$_heart_cand/bin/_pyauto_root.sh" ]; then
    _heart_workspace_shared="$_heart_cand/bin/_pyauto_root.sh"
    break
  fi
done

if [ -n "$_heart_workspace_shared" ]; then
  . "$_heart_workspace_shared"
  # A PyAutoBrain checkout older than the marker rule sets only PYAUTO_ROOT.
  # Fill the rest in rather than leave a caller reading an unset variable
  # under `set -u`; the reason says the root came from there either way.
  PYAUTO_ROOT_MARKER="${PYAUTO_ROOT_MARKER:-.pyauto-root}"
  PYAUTO_ROOT_REASON="${PYAUTO_ROOT_REASON:-PyAutoBrain resolver (no reason reported)}"
  PYAUTO_WT_ROOT="${PYAUTO_WT_ROOT:-${PYAUTO_ROOT}-wt}"
  export PYAUTO_ROOT_MARKER PYAUTO_ROOT_REASON PYAUTO_WT_ROOT
else
  # Degraded: the same order, applied here, with the reason saying so — a
  # wrong root must never read like a right one. The Heart runs with no Brain
  # in reach (a standalone install, a single-repo session), which is the same
  # case dashboard.theme() degrades for.
  PYAUTO_ROOT_MARKER=".pyauto-root"
  export PYAUTO_ROOT_MARKER
  if [ -n "${PYAUTO_ROOT:-}" ]; then
    if [ -f "$PYAUTO_ROOT/$PYAUTO_ROOT_MARKER" ]; then
      PYAUTO_ROOT_REASON="PYAUTO_ROOT (no PyAutoBrain in reach)"
    else
      PYAUTO_ROOT_REASON="PYAUTO_ROOT (unverified - no $PYAUTO_ROOT_MARKER marker) (no PyAutoBrain in reach)"
    fi
  else
    _heart_d="$(dirname "$_heart_workspace_home")"
    PYAUTO_ROOT=""
    while [ "$_heart_d" != "/" ]; do
      if [ -f "$_heart_d/$PYAUTO_ROOT_MARKER" ]; then
        PYAUTO_ROOT="$_heart_d"
        PYAUTO_ROOT_REASON="$PYAUTO_ROOT_MARKER marker (no PyAutoBrain in reach)"
        break
      fi
      _heart_d="$(dirname "$_heart_d")"
    done
    if [ -z "$PYAUTO_ROOT" ]; then
      PYAUTO_ROOT="$(dirname "$_heart_workspace_home")"
      if [ -d "$PYAUTO_ROOT/PyAutoMind" ] || [ -d "$PYAUTO_ROOT/PyAutoHeart" ] \
         || [ -d "$PYAUTO_ROOT/PyAutoHands" ] || [ -d "$PYAUTO_ROOT/PyAutoMemory" ] \
         || [ -d "$PYAUTO_ROOT/PyAutoGut" ] || [ -d "$PYAUTO_ROOT/PyAutoNerves" ] \
         || [ -d "$PYAUTO_ROOT/PyAutoCortex" ] || [ -d "$PYAUTO_ROOT/PyAutoEyes" ]; then
        PYAUTO_ROOT_REASON="beside this checkout (no PyAutoBrain in reach)"
      else
        PYAUTO_ROOT_REASON="unverified (no sibling organ beside this checkout) (no PyAutoBrain in reach)"
      fi
    fi
  fi
  export PYAUTO_ROOT PYAUTO_ROOT_REASON
  PYAUTO_WT_ROOT="${PYAUTO_WT_ROOT:-${PYAUTO_ROOT}-wt}"
  export PYAUTO_WT_ROOT
fi

# The MAIN checkout — the tree the grading checks read. $PYAUTO_ROOT first, so
# the documented "grade this branch" opt-in keeps working; then git, which
# names the main repo of any linked worktree (and, from the canonical checkout,
# answers `.git` relative to it — one code path, both layouts); then the
# workspace root as a last resort, saying so.
if [ -n "$_heart_workspace_env_root" ]; then
  PYAUTO_MAIN_ROOT="$_heart_workspace_env_root"
  PYAUTO_MAIN_ROOT_REASON="PYAUTO_ROOT"
else
  _heart_common_dir="$(git -C "$_heart_workspace_home" rev-parse --git-common-dir 2>/dev/null)" || _heart_common_dir=""
  case "$_heart_common_dir" in
    ""|/*) ;;
    *)     _heart_common_dir="$_heart_workspace_home/$_heart_common_dir" ;;
  esac
  # Find the workspace marker above the main checkout; grouped organ
  # checkouts add a directory between the repo and workspace root.
  if [ -n "$_heart_common_dir" ] && [ -e "$_heart_common_dir" ]; then
    _heart_main_checkout="$(cd "$(dirname "$_heart_common_dir")" && pwd)"
    _heart_main_parent="$_heart_main_checkout"
    while [ "$_heart_main_parent" != / ] && [ ! -f "$_heart_main_parent/$PYAUTO_ROOT_MARKER" ]; do
      _heart_main_parent="$(dirname "$_heart_main_parent")"
    done
    if [ -f "$_heart_main_parent/$PYAUTO_ROOT_MARKER" ]; then
      PYAUTO_MAIN_ROOT="$_heart_main_parent"
    else
      PYAUTO_MAIN_ROOT="$(dirname "$_heart_main_checkout")"
    fi
    PYAUTO_MAIN_ROOT_REASON="main checkout (git --git-common-dir)"
  else
    PYAUTO_MAIN_ROOT="$PYAUTO_ROOT"
    PYAUTO_MAIN_ROOT_REASON="workspace root — git could not name the main checkout ($PYAUTO_ROOT_REASON)"
  fi
fi
export PYAUTO_MAIN_ROOT PYAUTO_MAIN_ROOT_REASON

fi
