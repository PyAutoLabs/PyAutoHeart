# PyAutoHeart — Agent Guidance

This file is for AI coding agents (Claude Code, Codex, Cursor, etc.)
discovering this repository.

## What this repo is

PyAutoHeart is the **health and vital-signs authority** of the PyAuto organism.
It owns health and release-readiness checking: continuous monitoring (CI status,
dirty checkouts, branch ahead/behind, open PRs, worktree state, script-timing
regressions, version skew) plus deep on-demand/cloud checks (install
verification, URL hygiene), workspace validation, and generated-artifact/noise
classification, all green/yellow/red colour coded. `pyauto-heart readiness` is
the **authoritative** "is it safe to release?" gate.

It is **separate** from PyAutoHands / PyAutoBuild on purpose: Hands is a pure
executor (it produces PyPI releases and runs no readiness checks); Heart owns
the checking. Heart shells out to `autobuild` primitives but never imports
PyAutoBuild Python, never writes into other repos, and never triggers Hands.

See [`AGENTS.md`](AGENTS.md) for the canonical Brain/Heart/Hands boundary and
the `Brain → Heart → Hands` call chain, and `README.md` for user-facing docs.

## Hard rules

1. **Color coding everywhere**: green = passing, yellow = warning,
   red = failing. Use the `c_ok / c_warn / c_fail / c_info / c_meta`
   helpers in `heart/_color.sh` (bash) and `heart/heart_color.py`
   (Python). Honour `NO_COLOR` and `--no-color`.
2. **Never write outside `~/.pyauto-heart/`** in any check module.
   The daemon must be a pure observer; mutations belong in
   `pyauto-heart fix <topic>` which only EMITS context for a fresh
   Claude session.
3. **Polling must be cheap**. A full `tick` should complete in <30s
   total. If a check would take longer, run it less often (move to a
   v2 daily cron, not the watch loop).
4. **Lightweight test footprint**. Heart's own test suite runs on the
   standard library plus PyYAML only — no scientific/ML stack (numba,
   matplotlib, JAX, the PyAuto libraries). This keeps the suite fast and
   flake-free so it runs anywhere (CI, mobile, sandbox). It is a property of
   *Heart's* tests, not a claim about the projects Heart watches — Heart may
   perfectly well monitor non-JAX (or JAX-heavy) repos; that's their concern,
   not the suite's.
5. **State writes are atomic**. Use `heart.state.atomic_write_json` or
   the bash equivalent (`heart_write_json` in `_common.sh`). Concurrent
   ticks must not corrupt `state.json`.

## Repo structure

```
bin/pyauto-heart                 # bash dispatcher
heart/                           # all logic, shell-first
  _color.sh, _common.sh
  daemon.sh, tick.sh             # the loop + one cycle
  state.py, status.py, fix.py    # Python side
  heart_color.py
  checks/                        # one file per check class
config/repos.yaml                # polled repo registry + thresholds
tests/                           # pytest
```

## Adding a new check

1. Create `heart/checks/<name>.{sh,py}` following the existing patterns.
2. Each check writes per-repo JSON sidecars to
   `$HEART_PER_REPO_DIR/<repo>.<check_kind>.json` OR a global file at
   `$HEART_STATE_DIR/<check_name>.json`.
3. Print a single colour-coded summary line to stdout (logged to the
   daemon log by `heart_log`).
4. Add a section to `heart/status.py:render` that surfaces the result.
5. Add tests in `tests/test_<name>.py` covering classification edges.
6. Wire into `heart/tick.sh` in the appropriate position.

## Running locally

```bash
pip install -e .[dev]
pytest tests/ -v
HEART_FORCE_COLOR=1 pyauto-heart tick     # one cycle, with colour
pyauto-heart status
```

## Codex / sandboxed runs

```bash
NUMBA_CACHE_DIR=/tmp/numba_cache MPLCONFIGDIR=/tmp/matplotlib \
  pytest tests/
```

## Never rewrite history

NEVER perform these operations on any repo with a remote:

- `git init` in a directory already tracked by git
- `rm -rf .git && git init`
- Commit with subject "Initial commit", "Fresh start", "Start fresh",
  "Reset for AI workflow", or any equivalent message on a branch with
  a remote
- `git push --force` to `main`
- `git filter-repo` / `git filter-branch` on shared branches
- `git rebase -i` rewriting commits already pushed to a shared branch

If the working tree needs a clean state, the **only** correct sequence is:

    git fetch origin
    git reset --hard origin/main
    git clean -fd
