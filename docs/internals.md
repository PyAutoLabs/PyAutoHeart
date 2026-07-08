# PyAutoHeart — internals

Operational detail for working **inside** this repo: the check framework, the
tick budget, how to add a check, and the hard rules. What PyAutoHeart *is* and
the Brain/Heart/Build boundary live in [`AGENTS.md`](../AGENTS.md) — read that
first; read this only when changing Heart's own code.

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

The never-rewrite-history rules live in [`AGENTS.md`](../AGENTS.md) and apply
here as everywhere.
