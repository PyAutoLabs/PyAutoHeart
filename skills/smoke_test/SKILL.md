---
name: smoke-test
description: Run targeted workspace smoke tests to verify downstream scripts still work after library changes.
user-invocable: true
---

Run a curated set of workspace scripts to verify that library changes haven't
broken downstream tutorials and examples.

A **PyAutoHeart** validation skill — Heart owns tests/validation/readiness. This
is the workspace-script check the ship workflow feeds into: `/ship_library` and
`/ship_workspace` gate through the Health Agent → Heart, and this skill is the
workspace half of that verdict. It reads the PyAutoMind registry for the active
issue but its job is validation, not state. Organ boundary + execution-environment
model: PyAutoBrain `skills/WORKFLOW.md`.

## Usage

```
/smoke-test                     # run all six workspaces (default)
/smoke-test autofit             # run only autofit_workspace (only when explicitly requested)
/smoke-test autogalaxy autolens # run specific workspaces (only when explicitly requested)
```

## Workspace mapping

| Argument | Directory | Script list | Notebook list |
|----------|-----------|-------------|---------------|
| `autofit` | `autofit_workspace` | `smoke_tests.txt` | `smoke_notebooks.txt` |
| `autogalaxy` | `autogalaxy_workspace` | `smoke_tests.txt` | `smoke_notebooks.txt` |
| `autolens` | `autolens_workspace` | `smoke_tests.txt` | `smoke_notebooks.txt` |
| `autolens_test` | `autolens_workspace_test` | `smoke_tests.txt` | — |
| `euclid` | `euclid_strong_lens_modeling_pipeline` | `smoke_tests.txt` | — |
| `howtolens` | `HowToLens` | `smoke_tests.txt` | — |

`smoke_tests.txt` (workspace root) lists `.py` scripts; `smoke_notebooks.txt`
lists `.ipynb` notebooks under `notebooks/`. Notebook + env-var semantics:
[`reference.md`](reference.md) → "Notebook smoke" and "Environment config".

## Steps

### 1. Determine which workspaces to test

**Default: run ALL six workspaces** — library changes propagate down the
dependency chain, so never assume only one is affected. Run a subset only when
the user explicitly passes workspace names.

### 2. Load env config + wipe stale output

For each workspace, read `config/build/env_vars.yaml` to build the per-script env
prefix (`defaults` minus matching `overrides` `unset`s, plus optional
`args_default`). Before launching, wipe `<workspace_root>/output/*` (glob — keep
the tracked `output/` dir). Detail: [`reference.md`](reference.md) →
"Environment config" and "Why wipe output".

### 3. Run the scripts (parallel)

Read `smoke_tests.txt`; skip entries listed in `config/build/no_run.yaml`
(`SKIPPED`). Resolve each path (workspace root, then `scripts/`, else `MISSING`)
and run with its env prefix, **in parallel** via background processes + `wait`.
Exact path-resolution + parallel-launch recipe: [`reference.md`](reference.md) →
"Running the scripts".

### 4. Track + report

Keep a running tally (continue past failures, capture each traceback). Print a
per-workspace `Passed | Failed | Total` summary table and list each failure with
its traceback.

### 5. Post results to the active issue

Post the summary table (+ collapsible failures) to the active source-code issue
via `gh issue comment`. Find the issue URL in `PyAutoMind/active.md`; if none,
ask the user. Comment template: [`reference.md`](reference.md) → "Issue comment".

### 6. Persist summary to the status cache

Write a per-workspace JSON summary to `~/.cache/pyauto/smoke/<workspace>.json`
so `/pyauto-status` can show the latest smoke state. Shape + field rules:
[`reference.md`](reference.md) → "Status cache". Idempotent; safe to skip if the
cache dir can't be created.

## Notes

- Env vars, their exceptions, and `args_default` live in each workspace's
  `config/build/env_vars.yaml`; the skip list in `config/build/no_run.yaml`. Edit
  those files — don't hardcode env vars here.
- `smoke_tests.txt` files live in each workspace root.
- Toggling `PYAUTO_SMALL_DATASETS` requires deleting `<workspace>/dataset/` (auto-
  simulation only re-creates missing datasets). `euclid_strong_lens_modeling_pipeline`
  does **not** use `PYAUTO_SMALL_DATASETS` — it tests against real Euclid VIS imaging.
- **Execution environments** (see WORKFLOW.md): in a web-github / ci-only session
  with no local tree, clone the workspace + library repos into the working
  directory, export `PYTHONPATH`/`NUMBA_CACHE_DIR`/`MPLCONFIGDIR`, and run the same
  steps. Detail: [`reference.md`](reference.md) → "Execution environments".
