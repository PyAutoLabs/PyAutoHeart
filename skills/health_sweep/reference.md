# Green-Light Sweep — the `/health check` leg

> Reference procedure, not a top-level command. This was the `/health_check`
> skill; it is now reached as **`/health check`** (see
> `PyAutoBrain/skills/health/health.md`). `/health` is the single health door;
> this file is the sweep leg it drives. A **PyAutoHeart** capability — Heart owns
> health assessment; the Brain's `/health` door drives it.

Quick "is everything still green?" sweep across the PyAuto stack. Refreshes local `main` against `origin/main` for every repo, then runs unit tests in libraries and smoke tests in workspaces. Reports a single pass/fail matrix.

**Distinct from:**
- `repo_cleanup` — heavy hygiene sweep (branches, stashes, worktrees). This sweep only reads state and runs tests.
- `smoke_test` — covers workspaces only, no library pytest, no main sync. This sweep calls into it.
- `verify_install` — fresh-user PyAutoLens install check. This sweep assumes the existing dev environment.

## Scope

**Libraries (run `pytest`):**
- PyAutoConf
- PyAutoArray
- PyAutoFit
- PyAutoGalaxy
- PyAutoLens
- PyAutoBuild

**Workspaces (run `/smoke_test`):**
- autofit_workspace
- autogalaxy_workspace
- autolens_workspace
- autofit_workspace_test
- autogalaxy_workspace_test
- autolens_workspace_test

**Out of scope — never touched:**
- `autofit_workspace_developer`, `autolens_workspace_developer` — dev scratch
- `autolens_base_project` — template
- Anything not listed above (e.g. `z_projects`, `bad`, `priors`, `euclid_strong_lens_modeling_pipeline`)

Skip any in-scope entry that is missing or not a git repo.

## Steps

### 1. Pre-flight branch check

For every in-scope repo, read the current branch and any in-progress git operation:

```bash
git -C <repo> rev-parse --abbrev-ref HEAD
test -e <repo>/.git/MERGE_HEAD && echo "MERGE in progress"
test -d <repo>/.git/rebase-merge -o -d <repo>/.git/rebase-apply && echo "REBASE in progress"
```

**Stop conditions** (abort the whole sweep, run nothing else):
- Any repo on a branch other than `main`. Report the full list of off-main repos with their branch names. The user needs to ship or park that work first.
- Any repo mid-merge or mid-rebase. Report which.

Print the stop reason and the offending repos. Do not proceed to step 2.

### 2. Sync `main` from origin

For every repo (now confirmed on `main`):

```bash
git -C <repo> fetch origin main
```

Then, per repo:
- **Clean working tree** (`git status --porcelain` is empty) → `git -C <repo> merge --ff-only origin/main`. Record `synced` or `already up to date`.
- **Dirty working tree** → skip the merge. Record `dirty — sync skipped`. Tests still run against the local state.

Never stash, never auto-resolve, never force. Dirty repos get tested as-is so in-progress work is not disturbed.

### 3. Run library unit tests

For each library, in parallel:

```bash
cd <library_repo>
pytest -q > /tmp/health_<library>.log 2>&1 &
```

Use `wait` to collect exit codes. Capture pass/fail counts and the first failing test name + tail of traceback for any failure.

If a library has no `test/` or `tests/` directory, mark as `no tests` and continue.

### 4. Run workspace smoke tests

Invoke the existing `/smoke_test` skill against the in-scope workspaces. Defer to its env-var / no_run / parallelism logic — do not reimplement. Capture its per-workspace pass/fail counts.

The `_test` variants (`autofit_workspace_test`, `autogalaxy_workspace_test`, `autolens_workspace_test`) have no `pytest` suite — smoke only. The `/smoke_test` skill already handles `autolens_workspace_test`; for the others, point it at their `smoke_tests.txt` if present, otherwise mark as `no smoke tests` and continue.

### 5. Report matrix

Single table, one row per repo:

```
Repo                       | Branch | Sync             | Tests
---------------------------|--------|------------------|---------------------------
PyAutoConf                 | main   | synced           | ✓ unit (412/412)
PyAutoArray                | main   | already up to date | ✓ unit (1240/1240)
PyAutoFit                  | main   | dirty — skipped  | ✗ unit (3 failed)
PyAutoGalaxy               | main   | synced           | ✓ unit (980/980)
PyAutoLens                 | main   | synced           | ✓ unit (1530/1530)
PyAutoBuild                | main   | synced           | no tests
autofit_workspace          | main   | synced           | ✓ smoke (8/8)
autogalaxy_workspace       | main   | synced           | ✓ smoke (8/8)
autolens_workspace         | main   | synced           | ✗ smoke (1 failed)
autofit_workspace_test     | main   | synced           | no smoke tests
autogalaxy_workspace_test  | main   | synced           | ✓ smoke (5/5)
autolens_workspace_test    | main   | synced           | ✓ smoke (7/7)
```

Below the matrix, list each failure with its first failing test name and a short traceback tail (≤ 30 lines). For dirty-skipped repos, add a one-line reminder so the user knows local main is behind origin.

End with a single-line verdict: `All green` or `N failures across M repos — see above`.

## Notes

- This is a read-mostly sweep. The only mutation is `git fetch` + `git merge --ff-only` on clean repos. No branch creation, deletion, stash, or rebase.
- Run-time dominated by smoke tests. Library pytest in parallel is fast; smoke tests can take many minutes — that's expected.
- Do not post results anywhere (no GitHub issue comment). This is a local check; output is for the user only.
- If the user wants to skip the sync step (e.g. running offline), they can pass `--no-sync` to `/health check`. In that mode, step 2 is replaced with a single `git status` per repo and the Sync column reads `skipped (--no-sync)`.
