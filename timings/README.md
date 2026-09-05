# `timings/` — the permanent CI timing record

This directory is PyAutoHeart's **append-only record of how long CI takes**. It
is written by exactly one thing — the daily `heart-health.yml` cloud job, which
commits it beside the README board block, one commit a day — and read by the
two timing checks as the first source for their baselines.

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
timings/gates.jsonl          # one line per UTC date
timings/scripts/<repo>.jsonl # one line per (python leg, run id)
```

`gates.jsonl` and `timings/scripts/` are created by the first run that has
something to record; an empty file is not a record, so they are not committed
ahead of the data.

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
* `scripts/<repo>.jsonl` is keyed by **`(python, run_id)`** — appending is
  skipped when that leg of that run is already recorded.

The second key is the load-bearing one. The smoke artifacts only change when a
PR runs, so a quiet week hands the daily job the *same run* seven days running.
Keyed on the day, that writes seven copies of one measurement — the
`script_timing` "one value repeated seven times" defect, recorded once and not
to be re-derived — and a flat week reads as a week of measurements. Keyed on
the run, a quiet week records nothing, which is the truth.

## Reserved: `epochs.jsonl`

`timings/epochs.jsonl` is **reserved** for a future labelled epoch boundary:

```json
{"date":"2026-09-05","label":"runners moved to 8 cores","note":"..."}
```

A reader could then tell a step change in the world from a regression in the
code. Nothing writes it, and the file deliberately does not exist yet —
`heart/timings.py` names the path (`EPOCHS_FILE`) so the reservation lives in
the code as well as here. An epoch is a human's judgement about the world, not
something a daily job can observe.

## Growth

* `gates.jsonl`: ~1 line/day. A line is a few hundred bytes per tracked gate;
  a year is a few hundred lines.
* `scripts/<repo>.jsonl`: **at most** one line per python leg per day, and in
  practice far fewer — a line lands only when a new run produced a new
  artifact. Two legs per repo is the current shape, so ≤ 2 lines/day/repo, and
  a quiet repo contributes nothing at all.

Yearly sharding (`gates-2026.jsonl`, `scripts/2026/<repo>.jsonl`) is the
obvious next step if a file ever gets unwieldy. **Not now**: at this growth
rate the whole record is comfortably under a megabyte for years, and sharding
early would buy a path-resolution rule and a reader that has to glob, in
exchange for nothing.

## Reading it

```bash
# The one-screen answer: days recorded, observations, repos, holes.
PYTHONPATH="$PWD" python -m heart.timings show

# One gate's daily p50s.
jq -r '.date + " " + (.gates["RepoA/Gate One"].p50_s|tostring)' timings/gates.jsonl

# One script's seconds across the recorded runs of a repo.
jq -r '[.run_id, .python, .entries["imaging/x.py"][0]] | @tsv' \
   timings/scripts/RepoA.jsonl
```

The daily job also writes the census to `$HEART_STATE_DIR/timings_record.json`,
which `state.aggregate()` folds into the snapshot as `timings_record` and the
board renders as one detail line under each of the two ⏱ timing rows.
