# Release validation — the `release` profile and acceptance criteria

This document is the **spec** half of Heart's release-validation tier. Heart
owns the *definition* of what a release-grade validation run must do; it does not
execute the build or the integration run (that is the Brain Release Agent
dispatching Build's `release.yml` and the evolved `workspace-validation.yml`).

M2 shipped the report schema, `pyauto-heart validate --ingest`, and the
readiness hard gate. **M3 wires the acceptance criteria below into
`workspace-validation.yml` itself** via a `mode: release` input (alongside the
untouched, default `mode: smoke` per-PR path):

- The `release` env profile lives in each workspace/`*_workspace_test` repo as
  `config/build/env_vars_release.yaml` — a self-contained sibling of
  `env_vars.yaml` (the `smoke` profile), passed to Build's `run_python.py`
  unmodified via its existing `--env-config` flag. No changes were needed in
  PyAutoBuild's executor primitives to support this — `--env-config` already
  accepted an arbitrary path.
- `run_scripts`, when `mode: release`, `pip install`s the Stage-2 TestPyPI
  wheels at the rehearsed version and puts **no** library source on
  `PYTHONPATH`, still executing from inside the workspace checkout.
- `verify_install_release` runs `heart/checks/verify_install.sh --testpypi
  --version <version>` A–E against the same wheels.
- `emit_release_report` reshapes Build's `aggregate_results.py` report.json
  (via the new `heart/validate.py::to_stage_report` / `pyauto-heart validate
  --emit-stage-report`) into the `{"stage": "integrate", ...}` contract
  `--ingest` expects, folding in the `verify_install` result and the
  Release-Agent-supplied `commit_shas`, and uploads it as the
  `release-stage-report` artifact for the Release Agent to feed into
  `pyauto-heart validate --ingest`.

`mode: release` is scoped to the `autofit`/`autogalaxy`/`autolens` workspaces
and their `*_workspace_test` siblings only — the HowTo* tutorial repos have no
`env_vars_release.yaml` and stay out of the release-fidelity script matrix
(they are still exercised under `mode: smoke`, unchanged).

## Why a distinct profile

The per-PR smoke gate and the release gate are different jobs:

- **smoke** — a fast structural / integration check: *does the model compose and
  the script run end-to-end?* Cheap, runs on every PR.
- **release** — *does the exact source about to ship, installed from the built
  wheel, pass at release fidelity?* Slow, runs only for a release rehearsal.

Both tiers' `config/build/env_vars.yaml` today default to smoke values
(`PYAUTO_TEST_MODE=2`, `PYAUTO_SMALL_DATASETS=1`, `PYAUTO_DISABLE_JAX=1`,
`PYAUTO_FAST_PLOTS=1`). That is correct for a per-PR smoke and wrong for a
release gate. The two must be **named, distinct profiles** so a release run
cannot silently inherit smoke fidelity.

## The `release` profile (wired in M3)

The intended release-fidelity env is already documented in
`PyAutoBuild/.github/workflows/release.yml`:

| Tier                 | `PYAUTO_TEST_MODE`               | `PYAUTO_SMALL_DATASETS` | `PYAUTO_FAST_PLOTS` |
|----------------------|----------------------------------|-------------------------|---------------------|
| user workspaces      | `1` (reduced iterations)         | `1` (capped grids)      | `1`                 |
| `*_workspace_test`   | `0` (real searches, `n_like_max`)| unset (full-res)        | unset               |

Per-script `overrides:` still layer **on top of** the selected profile — e.g.
unset `PYAUTO_SMALL_DATASETS` for full-resolution FITS scripts, keep JAX on for
`jax_likelihood_functions/` and `jax_substructure/`. The profile sets the floor;
overrides remain per-script.

The full library env-var surface (canonical entry:
`PyAutoConf/autoconf/test_mode.py`) is 13 `PYAUTO_*` vars, not the 4 smoke
defaults:

```
PYAUTO_TEST_MODE, PYAUTO_SMALL_DATASETS, PYAUTO_FAST_PLOTS, PYAUTO_OUTPUT_MODE,
PYAUTO_DISABLE_JAX, PYAUTO_SKIP_FIT_OUTPUT, PYAUTO_SKIP_VISUALIZATION,
PYAUTO_SKIP_CHECKS, PYAUTO_SKIP_LATENTS, PYAUTO_SKIP_WORKSPACE_VERSION_CHECK,
PYAUTO_LATENT_NAN_INJECT, PYAUTO_DISABLE_IPYTHON_DISPLAY, PYAUTO_LIVE_VIEWER_LOG
```

plus a few per-script switches outside any yaml default (`PYAUTO_MASS_MODE` /
`PYAUTO_MASS_FAST`, `JAX_PILOT` / `JAX_PLATFORM_NAME` / `JAX_PLATFORMS`).

**Explicitly NOT Heart's to set.** `config/general.yaml`'s `test:` block
(`check_likelihood_function`, `lh_timeout_seconds`,
`disable_positions_lh_inversion_check`) and `version:` toggles are
workspace-run/user settings. The release validation runs the scripts **as the
workspace ships them** and does not mutate these. Heart's only version signal is
the existing `version_skew` check. This `release` profile is an env-var profile,
not a Heart-owned config mutation.

## Wheel-install requirement (wired in M3)

Two verified gaps the M3 integration run MUST close (they are why the report
carries `profile` and `commit_shas`, so the gate can enforce them):

1. **Test BUILDS, not SOURCE.** Today `workspace-validation.yml` shadows the
   PyAuto packages with source checkouts via `PYTHONPATH`, so the gating run
   never touches a wheel — the exact blind spot that let the PyAutoFit `[nss]`
   git-URL break every TestPyPI upload for weeks. The release run MUST
   `pip install` the TestPyPI wheels published by the M1 rehearsal and put **no**
   source on `PYTHONPATH`.

2. **Wheel-based config resolution.** autoconf resolves the *workspace's*
   `config/` only when scripts run from inside the workspace checkout; a bare
   wheel falls back to the library's *packaged* defaults. So the run must
   `pip install` the wheels **but still execute scripts from within the workspace
   checkout** (for `config/` + `dataset/`), with no source on `PYTHONPATH`.

The integration run also performs `verify_install` A–E against the same wheels.

## How the gate enforces these

`heart/validate.py` records `profile` and per-repo `commit_shas` in
`validation_report.json`; `heart/readiness.py` then requires, for GREEN:

- `release_ready == true` (no stage failed — else RED),
- `profile == release` (else YELLOW — a smoke-fidelity run is not a release gate),
- `commit_shas` matching the current `main` HEADs (else YELLOW — stale source),
- freshness (a rehearsal older than `VALIDATION_STALE_DAYS` is YELLOW).

Before M3 (or if the Release Agent only runs the M1 rehearsal and skips
dispatching `workspace-validation.yml` in `mode: release`), an ingested
rehearsal-only report still (correctly) gates YELLOW: the source was built and
TestPyPI-installed, but not yet exercised at release fidelity. `mode: release`
is what supplies the `integrate` stage that flips this to GREEN-eligible.
