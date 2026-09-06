# `timings/` — the permanent CI timing record

This directory is PyAutoHeart's **append-only record of how long CI takes**. Its
observations are written by exactly one thing — the daily `heart-health.yml`
cloud job, which commits them beside the README board block, one commit a day —
and read by the timing checks as the first source for their baselines (for
`unit_timings` it is the *only* source: the board never carried per-test rows).
The one exception is [`epochs.jsonl`](#epochsjsonl--where-the-world-changed),
which a **human** writes in a PR: a boundary is a judgement about the world, not
an observation, and it decides how far back every baseline here can see.

It exists because the alternative did not last. `ci_timing` and `smoke_timings`
both carried their history in the `board.json` published to Pages by the
previous run: free, idempotent, and *the same artifact the render produces*. A
publish gap, a rewritten board or a schema change and the history is gone, and
nothing can recompute it — the Actions REST window is a couple of weeks and a
smoke-timings artifact expires in days. The board stays as the fallback; this
is the copy that lasts.

The code is [`heart/timings.py`](../heart/timings.py); its module docstring
carries the same rules for a reader who arrives from the Python side.

## The files

```
timings/README.md            # this file — doctrine, not data
timings/legacy_round_2026-09.md  # the digest of the first (legacy) round — prose, not data
timings/epochs.jsonl         # one line per epoch boundary  — a human writes it
timings/gates.jsonl          # one line per UTC date
timings/scripts/<repo>.jsonl # one line per (python leg, run id)  — smoke scripts
timings/unit/<repo>.jsonl    # one line per (python leg, run id)  — unit tests + import
```

`gates.jsonl`, `timings/scripts/` and `timings/unit/` are created by the first
run that has something to record; an empty file is not a record, so they are not
committed ahead of the data.

### `gates.jsonl` — one line per date

One JSON object per line, from `ci_timing.json`'s `gates`:

```json
{"date":"2026-09-05","ts":"2026-09-05T05:03:00+00:00",
 "gates":{"RepoA/Gate One":{"p50_s":553.0,"pr_median_s":601.0,"max_s":912.0,
                            "queue_median_s":12.0,"runs":14}}}
```

* The key is `<repo>/<workflow>`, exactly the gate key the board uses.
* `p50_s` is the gate's `median_s` — the median over **success** runs — and
  `runs` is `runs_counted`, the coverage behind it. Coverage travels beside
  time, always: a gate that got faster by running less must not read as a gate
  that got faster.
* Only gates whose `median_s` is a number are recorded. A gate with no
  completed runs in the window measured nothing, and a row of nulls would make
  the record look like it has coverage it does not have.

### `scripts/<repo>.jsonl` — one line per leg per run

One JSON object per line, from `smoke_timings.json`'s `repos` (the provenance)
joined to its `rows` (the timed entries):

```json
{"date":"2026-09-05","at":"2026-09-04T10:00:00Z","python":"3.12","run_id":7,
 "run_url":"https://ci.invalid/OwnerX/RepoA/actions/runs/7",
 "head_branch":"feat/x","head_sha":"abc123","env_profile":"smoke",
 "cache":{"jax":"hit","datasets":"miss"},
 "entries":{"imaging/x.py":[12.5,"passed",600.0]}}
```

* `entries` maps the entry path to the triple `[seconds, status, cap_s]`.
  Positional on purpose: this file grows by one line per leg per run forever,
  and repeating three key names on every entry would multiply it for nothing a
  reader cannot get from this schema. Entries are sorted by path.
* **Untimed entries are not in the record.** The rollup's `rows` carries the
  timed entries only; an entry the runner skipped has `seconds: null` and was
  never a measurement. The per-leg census on the board (`repos[].entries` vs
  `timed`) is where coverage stays visible — a `null` here would be a
  fabricated zero-second row in a dataset whose whole purpose is timing.
* `head_sha` and `env_profile` are `""` when the rollup that produced the line
  predates them, so every line in a file carries the same keys either way.
* **`cache` is the condition the measurement was taken under**, not a
  measurement of its own: `jax` and `datasets` are each `hit`, `miss` or
  `unknown`, read from the `cache_state.json` sidecar the smoke workflow
  uploads beside the timings dataset. The CI job restores a JAX compilation
  cache and the workspace's simulated `dataset/` tree between runs, so two
  otherwise identical legs can differ by the whole cost of recompiling or
  re-simulating. A line recorded before the sidecar existed reads `unknown` on
  both sides — the honest absence, never a fabricated `miss`.

  From this follows the one rule the comparison obeys:

  > **Drift is never classified across two known but different jax cache
  > states.** A cold run against a hot baseline is a recompile; a hot run
  > against a cold one is the cache landing. Neither is a change anybody made,
  > so `smoke_timings.classify_drift` returns `ok` with no ratio rather than
  > crying wolf on exactly the runs where the cache is doing its job. An
  > `unknown` on either side compares exactly as it did before the field
  > existed.

### `unit/<repo>.jsonl` — one line per leg per run

One JSON object per line, from `unit_timings.json`'s `repos` — the rollup the
libraries' own CI feeds through
[`heart/checks/unit_timings.py`](../heart/checks/unit_timings.py):

```json
{"date":"2026-09-05","at":"2026-09-04T10:00:00Z","python":"3.12","run_id":7,
 "run_url":"https://ci.invalid/OwnerX/RepoA/actions/runs/7",
 "head_branch":"feat/x","head_sha":"abc123","package":"pkg_a","import_s":3.62,
 "suite":{"tests":1500,"failures":0,"errors":0,"skipped":3,"wall_s":412.0},
 "slowest":{"tests/foo/test_bar.py::test_x":12.5}}
```

* **Only the N slowest tests are recorded**, with the suite totals beside them.
  A library suite is ~1500 tests; recorded whole, one line would carry 1500
  entries and this file would grow by a megabyte a week for rows nothing reads —
  the board shows the slowest handful and the totals. `suite` is what keeps the
  coverage visible next to them (a suite that got faster by running fewer tests
  must not read as a suite that got faster), and `slowest` maps the pytest node
  id to its seconds — the same positional-free shape the drift rule compares
  run to run. `top_n` lives in `config/repos.yaml`, never here.
* **`import_s` is the fresh-process cold import** measured on the CI runner: a
  new interpreter importing the package once, which is the cost the developer
  loop actually pays — not the warm in-process cost a test session amortises. It
  is `null`, never `0.0`, when that import failed or timed out; the import
  baseline is the **median** of the last `import_window` non-null observations,
  which is why a null is skipped rather than carried as a zero.
* A test that drops out of the slowest N simply has no baseline next run. That
  is honest: nothing was recorded about it.

## The two rules

### 1. Append-only

Lines are only ever **added**. A wrong line is superseded by a later one; it is
never edited and never deleted, and this directory is never hand-edited at all.
The record is evidence, and evidence that can be quietly rewritten is not
evidence. Every writer in `heart/timings.py` opens for append; none of them
truncates.

### 2. Dedupe on identity, never on the day

* `gates.jsonl` is keyed by **`date`** — appending is skipped when that date is
  already present. Re-running the daily job is then a no-op rather than a
  second point for the same day in every gate's sparkline.
* `scripts/<repo>.jsonl` and `unit/<repo>.jsonl` are keyed by
  **`(python, run_id)`** — appending is skipped when that leg of that run is
  already recorded.

The second key is the load-bearing one. The smoke artifacts only change when a
PR runs, so a quiet week hands the daily job the *same run* seven days running.
Keyed on the day, that writes seven copies of one measurement — the
`script_timing` "one value repeated seven times" defect, recorded once and not
to be re-derived — and a flat week reads as a week of measurements. Keyed on
the run, a quiet week records nothing, which is the truth.

## `epochs.jsonl` — where the world changed

One JSON object per line, one **boundary** per line, sorted by date:

```json
{"date":"2026-09-05","label":"legacy","note":"why this is a boundary"}
```

* `date` is `YYYY-MM-DD` and nothing else — the record sorts these as strings,
  and an unpadded `2026-9-5` would sort *after* `2026-12-01`.
* `label` is the short name the board shows (`legacy`, `fast-tests`). A line
  with no date or no label is not a boundary and is dropped on read: one could
  not be compared against, the other could not be named on a row.
* `note` is the reasoning a later reader cannot re-derive — *why* this is a
  boundary, not a restatement of the label.

**Readers compare within the current epoch** — the latest boundary dated on or
before today. Records dated before it are invisible to every baseline:
`gates_history`, `previous_script_rows`, `previous_unit_rows` and
`import_history` each take a `since` and drop the earlier lines *before* they
pick the latest observation per leg or take the median window. A number
measured before the world changed is not a baseline for a run after it — it is
a different experiment. A record line with **no** date counts as older than any
boundary: the record cannot place it after the change, and a baseline that
might predate the change is not a baseline. No boundaries at all means one
unbroken epoch, which is exactly how this record read before the file existed.

A boundary dated in the **future** is not in force yet, which is what lets a PR
append the boundary for the change it is landing without blinding every
baseline the moment it merges.

### Who writes it

A **human**, in a PR, through the verb:

```bash
PYTHONPATH="$PWD" python -m heart.timings epoch \
  --date 2026-09-05 --label legacy --note "why this is a boundary"
```

Never the daily job. `heart.timings append` writes observations and does not
touch this file — an epoch boundary is a *judgement about the world* (a
rebuild, a runner change, a cap change), and a daily job cannot observe one.
The verb is append-only like every other writer here: it refuses a duplicate
`(date, label)` and prints `epoch already recorded: …`, so re-running it is a
no-op rather than a second boundary for the same judgement.

### The standing instruction

**The phase that lands a rebuild appends the next boundary, in its own PR.**
That is the whole mechanism: the post-rebuild history starts fresh, because
from the boundary's date onwards no reader can see a single pre-rebuild number.
A rebuild that lands without its boundary leaves every baseline comparing two
different worlds and reporting the difference as a regression — the failure
mode this file exists to prevent. For the `ci-timing-fast-tests` epic the next
one is `label: fast-tests`, appended by the phase that lands the rebuild.

### The current content

One boundary:

* **`legacy` @ 2026-09-05** — the pre-rebuild reference round of the
  `ci-timing-fast-tests` epic. Phases 5–7 of that epic change `_test` script
  content, datasets, pinned likelihoods and CI caches; everything recorded up
  to and including this date measured the world as it stood *before* those
  changes, and comparisons must not cross the boundary.

## Growth

* `gates.jsonl`: ~1 line/day. A line is a few hundred bytes per tracked gate;
  a year is a few hundred lines.
* `scripts/<repo>.jsonl`: **at most** one line per python leg per day, and in
  practice far fewer — a line lands only when a new run produced a new
  artifact. Two legs per repo is the current shape, so ≤ 2 lines/day/repo, and
  a quiet repo contributes nothing at all.
* `epochs.jsonl`: a handful of lines a year at most — a boundary is written
  only when the world actually changed.
* `unit/<repo>.jsonl`: the same shape and the same cap — ≤ 2 lines/day/repo,
  and only when a new run produced a new artifact. A line is larger than a
  scripts line (the slowest N tests plus the suite totals) but bounded by
  `top_n`, which is what keeps a 1500-test suite from setting the file size.

Yearly sharding (`gates-2026.jsonl`, `scripts/2026/<repo>.jsonl`) is the
obvious next step if a file ever gets unwieldy. **Not now**: at this growth
rate the whole record is comfortably under a megabyte for years, and sharding
early would buy a path-resolution rule and a reader that has to glob, in
exchange for nothing.

## Reading it

```bash
# The one-screen answer: days recorded, observations, repos, holes — and the
# epoch every reader is currently comparing inside of.
PYTHONPATH="$PWD" python -m heart.timings show

# One gate's daily p50s.
jq -r '.date + " " + (.gates["RepoA/Gate One"].p50_s|tostring)' timings/gates.jsonl

# One script's seconds across the recorded runs of a repo.
jq -r '[.run_id, .python, .entries["imaging/x.py"][0]] | @tsv' \
   timings/scripts/RepoA.jsonl

# One library's suite wall-clock and cold import across the recorded runs.
jq -r '[.run_id, .python, .suite.wall_s, .import_s] | @tsv' \
   timings/unit/RepoA.jsonl
```

The daily job also writes the census to `$HEART_STATE_DIR/timings_record.json`,
which `state.aggregate()` folds into the snapshot as `timings_record` and the
board renders as one detail line under each of the two ⏱ timing rows.
