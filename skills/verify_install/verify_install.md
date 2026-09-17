# Verify Install: Test PyAutoLens as a New User

Release-readiness gate for PyAutoLens. Runs a suite of independent install-path checks
in throwaway venvs / conda envs and reports a per-check PASS / FAIL / SKIP table.

The actual work lives in **`PyAutoHeart/heart/checks/verify_install.sh`** — release-readiness
checking is PyAutoHeart's job (PyAutoHands is a pure executor). This skill is a thin wrapper:
invoke `pyauto-heart verify_install` (which also writes the JSON sidecar that
`pyauto-heart readiness` consumes), read the report, expand any failures, and prompt the user
about cleanup if they ran with `--keep`.

## What the checks cover

| Check | What it verifies |
|-------|------------------|
| A | `pip install autolens` in a venv on default `python3`; `start_here.py` and `welcome.py` both run cleanly. |
| B | One exact `autolens` version installs and imports cleanly on `python3.12` and `python3.13`, then the same exact version is rejected by `python3.11` specifically because `Requires-Python` is `>=3.12`. An **unpinned** `pip install autolens` on `python3.11` is then required to be refused too — it was not, until 2026-08-19: pip backtracked to `2026.7.29.1` and installed a stale JAX-less stack silently. The `2026.7.29.1.post1` tombstone closes that, and this leg is what stops it reopening. |
| C | The conda flow from `installation/conda.rst` works end-to-end (`conda create … python=3.12` → `pip install autolens` → clone workspace → run `welcome.py` + `start_here.py`). |
| D | `pip install "autolens[optional]"` resolves cleanly and imports. |
| E | `pip install autolens==2026.2.26.4` (a yanked release the docs reference) still installs on `python3.12` by explicit pin. |
| F | The **Colab gate**. A `python3.12` venv (Colab's interpreter) is seeded from Google's own Colab package manifest (`googlecolab/backend-info`'s `pip-freeze.txt`): the with-deps closure of the PyAuto stack is resolved but **not** installed, and only the part of it Colab also ships is installed, at Colab's pinned versions. The injected setup cell then runs verbatim on top (`pip install autonerves --no-deps` → `setup_colab.setup("autolens")` → `--no-deps` bootstrap → workspace clone at the release tag), and the gate audits what that bootstrap left: it walks every declared requirement of the five installed libraries, AST-scans **every** `import` in their source at any depth, and imports each third-party module for real. An unguarded import of a module Colab will not have is **FAIL**; so is a headline `af.Emcee()` / `af.DynestyStatic()` / `af.Nautilus()` / `af.LBFGS()` that cannot be constructed, and a declared dependency that is both absent on Colab and actually imported. Version conflicts, guarded imports and never-imported gaps are reported as WARNs. A real notebook cell (`al.Imaging.from_fits` on the bundled `dataset/imaging/cosmos_web_ring`) runs last. SKIPs while the installed `autonerves` predates the `setup_colab` registry. |

Check B requires `python3.11`, `python3.12`, and `python3.13`; Checks E and F
require `python3.12`. A missing required interpreter is **FAIL**. Optional host
capabilities such as conda remain **SKIP** when unavailable and do not count
toward overall failure.

### What Check F does and does not cover

Check F covers **Colab's package set and Colab's interpreter** — the two things
that make a notebook die there and nowhere else. A dependency imported lazily
inside a function leaves `import autolens` working and only detonates on the
line that reaches it, and the workspace smoke gate cannot see those either (it
runs at `PYAUTO_TEST_MODE=2` and never constructs a sampler). That is the gap
this check closes; the fix for anything it finds is normally a new entry in
`_SHARED_EXTRAS` in `autonerves/setup_colab.py`.

It does **not** cover:

- **the GPU** — no accelerator is present, and `setup` is called with
  `raise_error_if_not_gpu=False`;
- **Colab's operating system, CUDA stack or `apt` packages** — only the pip
  package set is reproduced;
- **the manifest's lag** — `googlecolab/backend-info` is refreshed when Google
  cuts an image, so it trails the live runtime by a day or two. A failure
  caused purely by a version Colab shipped yesterday is possible; the report
  always names the manifest's source (`live`, `cache` or the vendored
  snapshot) and date so the evidence can be dated.

**In a `--version` rehearsal the gate audits the candidate, not the release.**
The injected setup cell is verbatim, and verbatim means unpinned: an unpinned
`pip install autonerves` can never select a dev pre-release, and the released
`setup_colab.setup()` then reinstalls the whole stack `--no-deps` unpinned too,
so the cell pulls the venv back down to the current PyPI release whatever the
seed step pinned. Check F therefore audits that first and reports it as an
advisory **`WARN`** row — a broken released bootstrap is not evidence against
shipping the candidate, because the release is the remedy — then **re-pins** the
venv to the candidate (`autonerves`, `autofit`, `autoarray`, `autogalaxy`,
`autolens` all `==<version>` from the rehearsal index, `--no-deps`, then
`setup_colab` reloaded and its own package list reinstalled exactly as
`_colab_setup` does) and gates on that. A `WARN` row never changes `ready`, so
it never moves the Heart verdict; it prints in the table, travels into the
sidecar and renders on the dashboard. A continuous run without `--version` is
unchanged: the released bootstrap *is* the candidate, so there is one audit.

The manifest is fetched live, cached at `$HEART_STATE_DIR/colab_pip_freeze.txt`,
and falls back to `heart/checks/colab_pip_freeze.snapshot.txt` when both are
unavailable. Deliberate exemptions live in `heart/config/colab_gate.yaml`
(`accepted_missing`), each with a written reason that travels into the report.

**`COLAB_GATE_AUTONERVES_SRC`** (development / witness runs only) installs a
path or requirement `--no-deps` over the installed `autonerves` — after the
candidate re-pin in a `--version` rehearsal, and before the single audit in a
continuous run. It exists because the package list the gate measures lives in
`autonerves/setup_colab.py`, so a fix to it cannot otherwise be rehearsed until
it is on PyPI:

```bash
COLAB_GATE_AUTONERVES_SRC=/path/to/PyAutoNerves pyauto-heart verify_install F
```

Never set it in CI — a release gate must read the wheels that are about to ship.

## Running without a skill harness

The script is self-contained and runs from any shell. The canonical entry point is
`pyauto-heart verify_install`, which runs the checks and writes the readiness sidecar.
(`autohands verify_install` still works as a thin shim that delegates here, for anyone
with PyAutoHands on PATH.)

```bash
pyauto-heart verify_install                       # run all checks (default)
pyauto-heart verify_install A                     # run a single check
pyauto-heart verify_install A C E                 # run a subset
pyauto-heart verify_install --version 2026.4.5.2  # pin a specific version (applies to A/B/C/D)
pyauto-heart verify_install --testpypi            # TestPyPI rehearsal; PyPI fallback for third-party deps
pyauto-heart verify_install B --version 9999.0.0.dev0 --find-links dist/
                                                    # local-wheel development evidence
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

Use `--testpypi` for a pre-release rehearsal. Use `--find-links <wheel-dir>`
for local artifacts; its sidecar is labelled `index: find-links`, so even a
passing result remains STALE until PyPI or TestPyPI evidence replaces it. A
failing local artifact remains RED.

If the user only wants a specific check (e.g. "just re-run the conda check"), pass
the letter:

```bash
pyauto-heart verify_install C
```

### 2. Surface the report

The script prints a results table and any failure detail to stdout, then cleans up.
Show the user the **table** verbatim and call out:

- which checks PASSed,
- which were SKIPped (and why — for example, conda is unavailable),
- which FAILed (with the captured stderr expanded inline).

### 3. Cleanup

By default the script removes every venv / conda env / workspace clone it created.
If the user wants to inspect a specific environment, re-run the relevant check with
`--keep`.

## Files

- `PyAutoHeart/heart/checks/verify_install.sh` — the runnable script; source of truth for
  what each check does.
- `PyAutoHeart/heart/checks/colab_gate.py` — Check F's `seed` / `verify` gate, plus
  `colab_pip_freeze.snapshot.txt` (the vendored Colab manifest) and
  `PyAutoHeart/heart/config/colab_gate.yaml` (accepted misses). Owned by PyAutoHeart, which owns all release-readiness checking; the
  `--report-json` sidecar it writes feeds `pyauto-heart readiness`.
- `verify_install.md` — this file; explains the skill and how to invoke it.

If a check needs to change (e.g. a new install-doc claim worth verifying), edit
`PyAutoHeart/heart/checks/verify_install.sh`. Update the table at the top of this file
if the set of checks changes.
