# smoke-test — reference detail

Factored out of `SKILL.md`. The body is authoritative for the flow; this holds
the verbose mechanics.

## Notebook smoke

Each notebook in `smoke_notebooks.txt` runs via
`jupyter nbconvert --to notebook --execute`, with the executed copy written to a
`/tmp` dir so the on-disk notebook stays clean. On failure, regenerate the single
failing notebook from its source `.py` via PyAutoHands's `py_to_notebook` and
retry once (catches stale notebooks where the script moved on but the `.ipynb`
wasn't refreshed by `$pre-build` (`/pre_build` in Claude). Whole-workspace
regeneration stays `generate.py`'s job — smoke only regenerates the one failing
notebook.

## Isolated smoke environments

`pyauto-heart smoke` is the local environment and execution entry point. It
does not trust the shell that launched it:

- Every workspace has a separate virtual environment under
  `$HEART_STATE_DIR/smoke-envs/<workspace>/py<major.minor>/`. This separation is
  required because workspace contracts can carry different JAX bounds.
- The workspace-owned `.github/scripts/smoke_install.sh` is executed with the
  environment's `pip` first on `PATH`, exactly like Heart's reusable CI smoke
  workflow. A legacy workspace without an epilogue gets its local chain and
  conventional `optional` extras from the libraries' `pyproject.toml` files;
  Heart never carries a duplicate list of third-party requirements.
- The fingerprint includes Python identity, the install epilogue and every
  chain library's `pyproject.toml`. Any change rebuilds at the environment's
  fixed path under a per-environment lock (virtualenv entry-point shebangs make
  finished environments non-relocatable). The previous complete environment is
  retained as a rollback backup until preflight succeeds, so a failed rebuild
  restores it intact.
- Runtime `PYTHONPATH` is replaced with the selected local library roots plus
  `PyAutoHands/autohands`; it is not extended from the ambient shell. Current
  source edits therefore win over cached site-packages without allowing an
  unrelated checkout to leak in.
- Before scripts begin, the command runs `pip check`, imports each local library
  and verifies its file lives under the expected checkout. Workspaces with
  notebooks must also have `jupyter` inside the environment and a `python3`
  kernelspec whose executable is that environment's Python.

Use `--prepare-only` to diagnose the environment without touching workspace
outputs or running scripts. Use `--rebuild` only when explicitly forcing a
fresh resolution; ordinary metadata changes rebuild automatically.

## Environment config

Each workspace's `config/build/profile_smoke.yaml` has:

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

`pyauto-heart smoke` invokes the workspace-owned
`.github/scripts/run_smoke.py`. Do not reproduce its discovery, ordering,
timeouts, skip matching or notebook behavior in an agent shell: those details
have changed before and the checked-in runner is the CI contract. The sole
legacy fallback reads `smoke_tests.txt`, resolves root-relative then
`scripts/`-relative entries, uses PyAutoHands's canonical environment resolver,
and honours `config/build/no_run.yaml`.

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
the library repos into one working directory, then point Heart at that organism
root:

```bash
WORK_DIR="$(pwd)"
for ws in autofit_workspace autogalaxy_workspace autolens_workspace \
          autolens_workspace_test euclid_strong_lens_modeling_pipeline HowToLens; do
  [ -d "$WORK_DIR/$ws" ] || git clone "https://github.com/PyAutoLabs/$ws.git" "$WORK_DIR/$ws"
done
for lib in PyAutoNerves PyAutoFit PyAutoArray PyAutoGalaxy PyAutoLens; do
  [ -d "$WORK_DIR/$lib" ] || git clone "https://github.com/PyAutoLabs/$lib.git" "$WORK_DIR/$lib"
done
pyauto-heart smoke --root "$WORK_DIR"
```

The command constructs `PYTHONPATH`, writable caches and virtual environments;
do not export ambient substitutes. Post results to the issue as normal. This is
the same validation with a different repo source — not a separate "mobile
mode".
