# smoke-test — reference detail

Factored out of `SKILL.md`. The body is authoritative for the flow; this holds
the verbose mechanics.

## Notebook smoke

Each notebook in `smoke_notebooks.txt` runs via
`jupyter nbconvert --to notebook --execute`, with the executed copy written to a
`/tmp` dir so the on-disk notebook stays clean. On failure, regenerate the single
failing notebook from its source `.py` via PyAutoBuild's `py_to_notebook` and
retry once (catches stale notebooks where the script moved on but the `.ipynb`
wasn't refreshed by `/pre_build`'s `generate.py`). Whole-workspace regeneration
stays `generate.py`'s job — smoke only regenerates the one failing notebook.

## Environment config

Each workspace's `config/build/env_vars.yaml` has:

- `defaults` — env vars applied to every script (`PYAUTO_TEST_MODE`,
  `PYAUTO_SMALL_DATASETS`, …).
- `overrides` — per-pattern exceptions that `unset` specific vars for matching
  scripts.
- `args_default` (optional) — string appended after the script path on every
  `python` invocation (e.g. euclid needs `--dataset`/`--sample`).

Pattern matching (same as `no_run.yaml`): a pattern containing `/` is a substring
match against the script path; without `/` it matches the file stem exactly. Build
the prefix: start from `defaults`, drop any `unset` vars whose override pattern
matches, format as `KEY=val ...`.

## Why wipe output

PyAutoFit resumes from cached `samples.csv` when an output dir exists. If the
model schema evolved since the cached run, the header no longer matches
`model.unique_prior_paths` and `Sample.parameter_lists_for_paths` raises
`KeyError`. Workspaces are templates with no long-term real results, so wipe
`output/*` (glob — **not** `output` itself, which is tracked via its `.gitignore`).

## Running the scripts

Read `smoke_tests.txt` (paths relative to the workspace root). Skip entries
matching `config/build/no_run.yaml` (`SKIPPED`). Resolve each path:

1. `<workspace_root>/<path>` exists → use it (root-level scripts, explicit
   `scripts/<name>`).
2. else `<workspace_root>/scripts/<path>` exists → use it (legacy bare names).
3. else `MISSING`, continue.

Run in parallel (scripts have no interdependencies — no `start_here.py` ordering):

```bash
cd <workspace_root>
<env_var_prefix> python <resolved_script_path> <args_default> > /tmp/smoke_<workspace>_<script_slug>.log 2>&1 &
```

`wait` to collect exit codes after launching all.

## Issue comment

```bash
gh issue comment <number> --repo <owner/repo> --body "$(cat <<'SMOKE_EOF'
## Smoke Test Results — <YYYY-MM-DD>

| Workspace | Passed | Failed | Total |
|-----------|--------|--------|-------|
| autofit_workspace | X | Y | Z |
| ... | | | |

<details>
<summary>Failures</summary>

### <workspace>/<script_path>
```
<traceback>
```

</details>

SMOKE_EOF
)"
```

All passing → just `## Smoke Test Results — <date>` + "All X smoke tests passed
across Y workspaces."

## Status cache

`mkdir -p ~/.cache/pyauto/smoke`, then per tested workspace write
`~/.cache/pyauto/smoke/<workspace>.json`:

```json
{
  "workspace": "autolens_workspace",
  "completed_at": "2026-04-28T12:34:56Z",
  "passed": 12,
  "failed": 1,
  "skipped": 0,
  "total": 13,
  "duration_seconds": 245.3
}
```

`workspace` = directory name (not the argument shorthand); `completed_at` = ISO
8601 UTC seconds; `total = passed + failed + skipped`; `duration_seconds` =
wall-clock for that workspace's parallel run, one decimal. Overwrite the same
workspace's file; leave untested workspaces' files alone. Idempotent; skip
silently if `mkdir` fails.

## Execution environments

In a web-github / ci-only session (no local tree), clone the workspace repos and
the library repos for `PYTHONPATH` into the working directory, then run the same
steps:

```bash
WORK_DIR="$(pwd)"
for ws in autofit_workspace autogalaxy_workspace autolens_workspace \
          autolens_workspace_test euclid_strong_lens_modeling_pipeline HowToLens; do
  [ -d "$WORK_DIR/$ws" ] || git clone "https://github.com/Jammy2211/$ws.git" "$WORK_DIR/$ws"
done
for lib in PyAutoConf PyAutoFit PyAutoArray PyAutoGalaxy PyAutoLens; do
  case "$lib" in PyAutoConf|PyAutoFit) ORG=rhayes777 ;; *) ORG=Jammy2211 ;; esac
  [ -d "$WORK_DIR/$lib" ] || git clone "https://github.com/$ORG/$lib.git" "$WORK_DIR/$lib"
done
export PYTHONPATH="$WORK_DIR/PyAutoConf:$WORK_DIR/PyAutoFit:$WORK_DIR/PyAutoArray:$WORK_DIR/PyAutoGalaxy:$WORK_DIR/PyAutoLens:$PYTHONPATH"
export NUMBA_CACHE_DIR=/tmp/numba_cache MPLCONFIGDIR=/tmp/matplotlib
```

Use `$WORK_DIR/<workspace>` as each workspace root; post results to the issue as
normal. This is the same validation with a different repo source — not a separate
"mobile mode".
