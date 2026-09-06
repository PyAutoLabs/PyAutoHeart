# Legacy timing round — where CI time went before the rebuild (2026-09-05)

Phase 4 of the `ci-timing-fast-tests` epic (the PyAutoMind ledger
`draft/feature/pyautoheart/ci_timing_fast_tests_epic.md`; PyAutoHeart#208). It lives
beside the record it describes, in `timings/`, and is linked from the ledger.
This is the **LEGACY reference**, not the start of the long-term history:
phases 5–7 change `_test` script content, simulated datasets, pinned
likelihoods and CI caches, so nothing measured after them may be compared
against these numbers. The boundary is recorded in the Heart's permanent
record as `timings/epochs.jsonl` → `legacy @ 2026-09-05`; the phase that lands
the rebuild appends the next one.

**Source.** The first live round of the finished board: `heart-health.yml`
dispatched by hand on 2026-09-05 (run after PyAutoHeart#207 merged), which
seeded `PyAutoHeart/timings/gates.jsonl` (26 gates) and
`timings/scripts/<repo>.jsonl` (22 legs — 11 repos × Python 3.12/3.13, 494
script rows, **0 legs unavailable**; the cross-repo artifact 403 risk did not
materialise). Per-script seconds are the runner's own `[PASS] — <n>s`
measurement inside the gate job; gate wall-clock is `updated_at −
run_started_at` medians over success runs, PR runs included. Board:
<https://pyautolabs.github.io/PyAutoHeart/>.

**Not yet in this round.** Unit-test durations and import times: the
`unit-timings-<py>` artifact (PyAutoHeart#207) is emitted by the next
library CI run after 2026-09-05 23:02 UTC and ingested on the following daily
run; imports render `building` until three runs are recorded. The unit/import
section below is therefore a placeholder to be filled by the phase-9 census,
and the Python-leg totals are the only library-suite figure this round has.

## 1. Gate wall-clock (what a contributor waits on)

| Gate | p50 | PR p50 | max | runs |
|---|---|---|---|---|
| autogalaxy_workspace_test Smoke Tests | 11.9 m | 11.8 m | 3.7 h (a stall, see §5) | 36 |
| autolens_workspace_test Smoke Tests | 9.2 m | 8.4 m | 19.5 m | 34 |
| autolens_workspace Smoke Tests | 7.8 m | 7.9 m | 14.6 m | 16 |
| HowToLens Smoke Tests | 6.3 m | 6.9 m | 8.6 m | 14 |
| autocti_workspace Smoke Tests | 5.3 m | 4.9 m | 9.5 m | 7 |
| PyAutoFit Tests | 4.6 m | 4.5 m | 7.4 m | 26 |
| PyAutoCTI Tests | 4.2 m | 4.4 m | 9.5 m | 43 |
| HowToGalaxy Smoke Tests | 4.1 m | 5.1 m | 5.4 m | 17 |
| autogalaxy_workspace Smoke Tests | 3.9 m | 3.7 m | 12.2 m | 17 |
| PyAutoLens Tests | 3.4 m | 3.1 m | 5.1 m | 27 |
| PyAutoGalaxy Tests | 3.4 m | 3.4 m | 8.2 m | 26 |
| autocti_workspace_test Smoke Tests | 3.2 m | 3.6 m | 9.5 m | 50 |
| PyAutoArray Tests | 2.7 m | 2.7 m | 4.2 m | 49 |
| autofit_workspace Smoke Tests | 2.5 m | 2.4 m | 6.6 m | 25 |
| autofit_workspace_test Smoke Tests | 2.0 m | 1.9 m | 9.3 m | 49 |
| HowToFit Smoke Tests | 1.8 m | 1.9 m | 4.7 m | 17 |
| PyAutoNerves Tests | 0.8 m | 0.8 m | 5.9 m | 45 |
| Navigator Check (6 repos) | 15–18 s (autocti 88 s) | 14–18 s | ≤ 4 m | — |
| PyAutoHeart Workspace Smoke (weekly) | 38 m | — | 38 m | 1 |
| PyAutoBrain Nightly Release | 83 m | — | 85 m | 2 |

Overhead beside the scripts: the two `_test` flagships spend **~2–3 minutes
per leg on checkout + install** before the first script runs
(autogalaxy_test: 714 s gate vs 537 s of scripts; autolens_test: 552 s vs 435 s).
That fixed cost is the same for every PR and is the ceiling phase 7's caches
work under.

## 2. The `_test` workspaces — the epic's target

Per-script seconds, Python 3.12 leg (3.13 is within a few percent; §4).

**autogalaxy_workspace_test** — 39 scripts, 537 s total, median 12.7 s.
Bands: 16 scripts < 10 s, 14 in 10–20 s, 9 in 20–40 s, none ≥ 40 s.

| s | script | class |
|---|---|---|
| 35.5 | multi_dataset/jax_likelihood/mge_group.py | compile (JAX full-dataset, MGE basis) |
| 31.7 | interferometer/jax_likelihood/delaunay_mge.py | compile |
| 30.1 | imaging/visualization/visualization.py | **execution — plot output** |
| 25.0 | multi_dataset/jax_likelihood/delaunay_mge.py | compile |
| 23.1 | imaging/jax_likelihood/mge_group.py | compile |
| 22.6 | interferometer/jax_likelihood/mge_group.py | compile |
| 21.9 | imaging/jax_likelihood/rectangular_mge.py | compile |
| 21.7 | imaging/jax_likelihood/delaunay.py | compile |

Eight of the nine scripts above 20 s are `jax_likelihood` — the `ENV: jax
full_datasets` class the ledger already named (compile ~12–18 s + full-res
vmap + ~5–7 s import). `visualization.py` is the one execution-dominated
outlier. The 20–40 s band alone is 232 s of the 537 s.

**autolens_workspace_test** — 27 scripts, 435 s total, median 12.0 s.
Bands: 10 < 10 s, 9 in 10–20 s, 6 in 20–40 s, 2 ≥ 40 s.

| s | script | class |
|---|---|---|
| 45.0 | multi_dataset/jax_likelihood/shared_preloads.py | compile (multi-dataset, Eigen-pool coverage class) |
| 42.5 | misc/jax_assertions/delaunay_nn_caps.py | compile + assertion sweep |
| 33.0 | imaging/jax_likelihood/rectangular.py | compile |
| 32.8 | misc/jax_assertions/delaunay_nn.py | compile + assertion sweep |
| 27.8 | interferometer/jax_likelihood/rectangular.py | compile |
| 27.0 | point_source/jax_likelihood/point.py | compile |
| 22.7 | interferometer/datacube/shared_preloads.py | compile |
| 20.4 | imaging/jax_likelihood/mge.py | compile |

Every script above 20 s is JAX-compiled. The eight above are 251 s of the
435 s; the `multi_dataset/jax_likelihood/*` scripts (the Eigen-pool bug class)
must keep their coverage through phase 6, per the epic's standing assumptions.

**autofit_workspace_test** — 14 scripts, 47 s; one outlier
(`graphical/analytic_gaussian_collapse.py` 17.3 s), the rest ≤ 4.5 s.
**autocti_workspace_test** — 3 scripts, 17 s. Neither is a target.

**What the rebuild waves should expect.** In both flagships the time is in
~15 compile-dominated scripts, not spread thin: shrinking datasets (pixel scale,
over-sampling, MGE basis size — phase 5/6 levers) shrinks both the compile
graph and the execution, and the persistent JAX cache (phase 7) removes the
compile share on cache-hit runs before the content changes are even measured.
That is why the ledger's review puts 7 before 5/6: the waves should target what
remains once compile is cached.

## 3. User-facing workspaces and HowTos — the import floor

| Repo | scripts | total | median | > 10 s |
|---|---|---|---|---|
| autolens_workspace | 39 | 303 s | 7.6 s | 8 |
| HowToLens | 50 | 254 s | 4.9 s | 3 |
| HowToGalaxy | 32 | 135 s | 4.1 s | 1 |
| autogalaxy_workspace | 16 | 104 s | 7.7 s | 2 |
| autocti_workspace | 3 | 94 s | 17.0 s | 3 |
| autofit_workspace | 10 | 25 s | 1.7 s | 0 |
| HowToFit | 14 | 27 s | 0.9 s | 1 |

The user-facing surface is **floor-dominated**: HowToLens runs 47 of 50
scripts under 10 s at a ~5 s median, so ~230 of its 254 s is the per-process
import + config floor, not science. The floor itself scales with the stack —
HowToFit's median is 0.9 s (autofit alone), HowToGalaxy's 4.1 s, HowToLens's
4.9 s, autolens_workspace's 7.6 s (autolens import + `PYAUTO_SMALL_DATASETS`
work). The few genuine bottlenecks:

| s | repo | script |
|---|---|---|
| 61.0 | autocti_workspace | imaging_ci/modeling/start_here.py — the slowest script in the organism |
| 18.7 | autolens_workspace | interferometer/features/pixelization/delaunay.py |
| 18.6 | autolens_workspace | guides/mappings.py (new, image-source-mappings p3) |
| 15.7 | HowToLens | chapter_3_pixelizations/tutorial_6_borders.py |
| 13.4 | autolens_workspace | imaging/features/pixelization/delaunay.py |
| 13.0 | HowToLens | chapter_2_lens_modeling/tutorial_5_linear_profiles.py |
| 13.0 | autolens_workspace | multi_galaxy/features/advanced/shapelets/modeling.py |
| 12.6 | autogalaxy_workspace | imaging/start_here.py |

Implication for phase 8: per-script fixes buy little here except the autocti
outlier; the lever is the shared floor (autolens import time, which the
phase-3 import leg now measures on every library CI run, and whatever the
`PYAUTO_SKIP_*` profile still lets through).

## 4. Python 3.13 vs 3.12

Leg totals agree within ~5 % for the `_test` flagships (autogalaxy_test 537 vs
541 s; autolens_test 435 vs 434 s). The user workspaces run **faster on 3.13**
(autolens_workspace 303 → 228 s, HowToLens 254 → 219 s, autogalaxy_workspace
104 → 84 s) — an import/interpreter floor effect, consistent with §3;
autocti_workspace is the exception (94 → 132 s). Comparability key stays
runner × Python leg, as the design doc says.

## 5. Hang events still on the board

Six `suspect_cancelled` events, all historical (2026-07-27 → 2026-08-29), five
of them autogalaxy_workspace_test runs killed at ~6 h (21,600 s) — the
jax-compile-stall arc's runs before the kill timer existed — plus one 397 s
Workspace Smoke cancel on 2026-08-29. None is from the current window's
runs; they age out of the 50-run window on their own.

## 6. Unit tests and imports — pending the first library CI run

`lib-tests.yml` emits `unit-timings-<py>` from the next push or PR on any
library; the daily run then records `timings/unit/<repo>.jsonl` and the
Unit-test timing / Import timing rows come alive (imports `building` for three
runs). Until then the only library figures are the gate medians in §1
(PyAutoFit 4.6 m, PyAutoCTI 4.2 m, PyAutoLens 3.4 m, PyAutoGalaxy 3.4 m,
PyAutoArray 2.7 m, PyAutoNerves 0.8 m — each including a source install of its
dependency chain). Phase 9 reads the per-test and per-import rows once they
exist; this section is the placeholder it fills.

## What this digest feeds

- **Phase 7 (CI caches)** — the compile share of the ~15 scripts in §2 is the
  expected win; measure cache-hit vs cache-miss legs against these totals.
- **Phase 5 (autogalaxy_workspace_test rebuild)** — the nine 20–40 s scripts
  in §2 are the target list; `visualization.py` is the one execution-dominated
  case and needs a different lever.
- **Phase 6 (autolens_workspace_test rebuild)** — the eight scripts ≥ 20 s;
  keep `multi_dataset/jax_likelihood/*` reproducing the Eigen-pool class.
- **Phase 8 (user workspaces / HowTos)** — the shared import floor, plus the
  autocti `imaging_ci/modeling/start_here.py` outlier.
- **Phase 9 (unit-test / import census)** — §6, once the unit record exists.
