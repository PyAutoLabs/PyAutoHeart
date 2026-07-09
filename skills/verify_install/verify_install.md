# Verify Install: Test PyAutoLens as a New User

Release-readiness gate for PyAutoLens. Runs a suite of independent install-path checks
in throwaway venvs / conda envs and reports a per-check PASS / FAIL / SKIP table.

The actual work lives in **`PyAutoHeart/heart/checks/verify_install.sh`** — release-readiness
checking is PyAutoHeart's job (PyAutoBuild is a pure executor). This skill is a thin wrapper:
invoke `pyauto-heart verify_install` (which also writes the JSON sidecar that
`pyauto-heart readiness` consumes), read the report, expand any failures, and prompt the user
about cleanup if they ran with `--keep`.

## What the checks cover

| Check | What it verifies |
|-------|------------------|
| A | `pip install autolens` in a venv on default `python3`; `start_here.py` and `welcome.py` both run cleanly. |
| B | `pip install autolens` succeeds and imports cleanly on `python3.9`, `python3.10`, `python3.11`, and `python3.13` — each in its own throwaway venv. Confirms the `requires-python = ">=3.9"` floor and that 3.12-only assumptions have not crept in. The loud "recommended Python version" banner is checked softly on 3.9 / 3.10 / 3.11 (banner-missing is reported in the detail line but does not fail the check, since the banner copy may evolve). |
| C | The conda flow from `installation/conda.rst` works end-to-end (`conda create … python=3.12` → `pip install autolens` → clone workspace → run `welcome.py` + `start_here.py`). |
| D | `pip install "autolens[optional]"` resolves cleanly and imports. |
| E | `pip install autolens==2026.2.26.4` (a yanked release the docs reference) still installs by explicit pin. |
| F | The Colab bootstrap path end-to-end: a venv emulating Colab's preinstalled env (`autolens` + `jax` from PyPI), a fake `google.colab` package so the on-Colab branch activates, then the injected setup cell verbatim (`pip install autoconf --no-deps` → `setup_colab.setup("autolens")` → workspace clone at the release tag) and one real notebook cell (`al.Imaging.from_fits` on `dataset/imaging/simple`). SKIPs while the installed autoconf predates the `setup_colab` registry (self-activates at the next release). |

A check that cannot run on the current host (interpreter missing, conda missing) is
reported as **SKIP** and does not count toward overall failure.

## Running outside Claude

The script is self-contained and runs from any shell. The canonical entry point is
`pyauto-heart verify_install`, which runs the checks and writes the readiness sidecar.
(`autobuild verify_install` still works as a thin shim that delegates here, for anyone
with PyAutoBuild on PATH.)

```bash
pyauto-heart verify_install                       # run all checks (default)
pyauto-heart verify_install A                     # run a single check
pyauto-heart verify_install A C E                 # run a subset
pyauto-heart verify_install --version 2026.4.5.2  # pin a specific PyPI version (applies to A/C/D)
pyauto-heart verify_install --keep                # don't clean up at the end
pyauto-heart verify_install --help
```

Or invoke the script directly:

```bash
bash $HOME/Code/PyAutoLabs/PyAutoHeart/heart/checks/verify_install.sh
```

## Running through this skill

### 1. Invoke the script

If the user specified a target version (e.g. "verify install of 2026.4.5.2"), pass it
through with `--version`. Otherwise no flags.

```bash
pyauto-heart verify_install
# or with a pinned version:
pyauto-heart verify_install --version <version>
```

If the user only wants a specific check (e.g. "just re-run the conda check"), pass
the letter:

```bash
pyauto-heart verify_install C
```

### 2. Surface the report

The script prints a results table and any failure detail to stdout, then cleans up.
Show the user the **table** verbatim and call out:

- which checks PASSed,
- which were SKIPped (and why — usually a missing interpreter or conda),
- which FAILed (with the captured stderr expanded inline).

### 3. Cleanup

By default the script removes every venv / conda env / workspace clone it created.
If the user wants to inspect a specific environment, re-run the relevant check with
`--keep`.

## Files

- `PyAutoHeart/heart/checks/verify_install.sh` — the runnable script; source of truth for
  what each check does. Owned by PyAutoHeart, which owns all release-readiness checking; the
  `--report-json` sidecar it writes feeds `pyauto-heart readiness`.
- `verify_install.md` — this file; explains the skill and how to invoke it.

If a check needs to change (e.g. a new install-doc claim worth verifying), edit
`PyAutoHeart/heart/checks/verify_install.sh`. Update the table at the top of this file
if the set of checks changes.
