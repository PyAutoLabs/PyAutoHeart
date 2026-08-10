---
name: smoke-test
description: Run targeted workspace smoke tests to verify downstream scripts still work after library changes.
---

Run a curated set of workspace scripts to verify that library changes haven't
broken downstream tutorials and examples.

A **PyAutoHeart** validation skill — Heart owns tests/validation/readiness. This
is the workspace-script check the ship workflow feeds into: `$ship-library` and
`$ship-workspace` (`/ship_library` and `/ship_workspace` in Claude) gate through
the Health Agent → Heart, and this skill is the
workspace half of that verdict. It reads the PyAutoMind registry for the active
issue but its job is validation, not state. Organ boundary + execution-environment
model: PyAutoBrain `skills/WORKFLOW.md`.

## Usage

```
$smoke-test                     # pyauto-heart smoke (all six; default)
$smoke-test autofit             # pyauto-heart smoke autofit
$smoke-test autogalaxy autolens # pyauto-heart smoke autogalaxy autolens
```

In Claude, invoke the same skill as `/smoke_test`.

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

### 2. Prepare and preflight isolated environments

Run `pyauto-heart smoke` with the selected workspace keys. Do not invoke the
workspace runners directly and do not repair the active shell by installing
packages into it. The command creates one cached environment per workspace from
its CI `smoke_install.sh`, invalidates it when Python/install metadata changes,
and preflights interpreter, package and Jupyter-kernel ownership before any
science script starts. Detail: [`reference.md`](reference.md) → "Isolated smoke
environments".

Useful diagnostic forms:

```bash
pyauto-heart smoke autogalaxy --prepare-only  # environment proof only
pyauto-heart smoke autogalaxy --rebuild       # discard/rebuild that cache
```

### 3. Run the workspace-owned suites

The command wipes stale `output/*` immediately before execution, then invokes
each workspace's `.github/scripts/run_smoke.py` with the prepared interpreter
and live local source on an isolated `PYTHONPATH`. The workspace runner owns
script/notebook discovery, profile resolution, skips, timeouts and reporting,
so local and CI semantics stay aligned. Heart has a compatibility runner for a
legacy workspace that has not acquired those two CI entry points yet.

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
so `$health status` can show the latest smoke state. Shape + field rules:
[`reference.md`](reference.md) → "Status cache". Idempotent; safe to skip if the
cache dir can't be created.

## Notes

- Env vars, their exceptions, and `args_default` live in each workspace's
  `config/build/profile_smoke.yaml`; the skip list in `config/build/no_run.yaml`. Edit
  those files — don't hardcode env vars here.
- Dependencies live in library `pyproject.toml` metadata and workspace
  `.github/scripts/smoke_install.sh` files. Never add a package-specific repair
  list to this skill; the cache fingerprint automatically follows those files.
- `smoke_tests.txt` files live in each workspace root.
- Toggling `PYAUTO_SMALL_DATASETS` requires deleting `<workspace>/dataset/` (auto-
  simulation only re-creates missing datasets). `euclid_strong_lens_modeling_pipeline`
  does **not** use `PYAUTO_SMALL_DATASETS` — it tests against real Euclid VIS imaging.
- **Execution environments** (see WORKFLOW.md): in a web-github / ci-only
  session with no local tree, clone the workspace + library repos into one
  organism root and pass it with `pyauto-heart smoke --root <dir>`. Detail:
  [`reference.md`](reference.md) → "Execution environments".
