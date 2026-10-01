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

## Dashboard consumer contract

Readiness emits additive `repository_reasons` with gate keys, plus a `penalties`
breakdown (count, weight, cap and deducted points). The dashboard projects these
into `sections[].entries`; `affects_release` comes from readiness, not a separate
dashboard policy. Legacy verdicts keep their original score and display an
unavailable breakdown until refreshed.

Brain board consumers forward `blockers[].prompt` and `command` verbatim.
Checkout-behind prompts now request a clean canonical-main fast-forward followed
by a tick; dirty/wrong-branch observations use `fix dirty`, rather than `/bug`.
CI failures retain their existing bug route. No new blocker severity is added.
`fix_plan`, `penalties`, `vantage`, `devbox_observed` and section entries are
additive; existing `stale_plan` and `performance` consumers need no migration.

`fix_plan.prompt` is a bounded summary (under 45,000 characters), suitable for
copying into an assistant. It preserves workflow constraints and references the
full `board.json` evidence. `fix_plan.evidence` holds the uncapped source
observations that used to be embedded in that prompt; consumers needing the
complete checklist must read it together with blockers and sections. Large
slices and omitted summary lines are explicitly identified. The shared browser
clipboard ceiling is 50,000 characters; larger requests are offered as complete
text downloads for attachment, never silently truncated.

Published dev-box entries use the existing path scrub on the complete entry,
including its evidence and prompt. Entries containing private local paths stay
local. Fresh observations retain their timestamp; expired observations remain
unobserved on the cloud board.

Timing cards are a pure presentation layer in `heart/timing_display.py`.
They expose seconds, comparison coverage and source separately; missing imports
and baseline-building observations never imply a passing comparison. Suite
legs stay separate (parallel wall-clocks are never summed); the three slowest
measured tests appear first, with remaining tests and legs under disclosures.
CI charts use dated daily medians with gaps for missing observations and an
equivalent text list. `performance` schema 1, its values and `gates[].spark`
remain unchanged for Brain and hygiene consumers. Plain section details retain
all measured rows; disclosures are HTML-only.

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

## Cloud validation evidence

The daily board runs the existing smoke-result reader and the read-only
`heart.checks.cloud_validation` collector before aggregation. The latter reads
only the newest main integration run and searches at most 20 main rehearsal
runs in the configured `release_evidence.rehearsal_repo`. It requires exact
version, run ID, attempt, producer SHA and chronology agreement. Missing or
expired rehearsal artifacts leave validation incomplete. Failed producers
remain adverse even when their report claims success; no older integration
pass is substituted. No build is dispatched.

The canonical validator receives the artifacts and their original producer
time (the earlier stage start), so a new cloud runner cannot rejuvenate an
old pass. Installation checks retain their own timestamps and source/index.
Readiness still checks release fidelity, current library SHAs and evidence age.
This bounded search is daily-only and does not add work to the fast local tick.
