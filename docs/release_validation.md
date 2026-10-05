# Release validation — the `release` profile and acceptance criteria

This document is the **spec** half of Heart's release-validation tier. Heart
owns the *definition* of what a release-grade validation run must do; it does not
execute the build or the integration run (that is the Brain Release Agent
dispatching Build's `release.yml` and Heart's `release-integrate.yml` channel).

M2 shipped the report schema, `pyauto-heart validate --ingest`, and the
readiness hard gate. **M3 wires the acceptance criteria below into the shared
`workspace-validation.yml` body** via a `mode: release` input. The two modes
now have separate entry workflows (one run history per meaning):
`workspace-smoke.yml` (the continuous smoke channel) and
`release-integrate.yml` (this release-fidelity channel):

- The `release` env profile lives in each workspace/`*_workspace_test` repo as
  `config/build/profile_release.yaml` — a self-contained sibling of
  `profile_smoke.yaml` (the `smoke` profile), passed to Build's `run_python.py`
  unmodified via its existing `--env-config` flag. No changes were needed in
  PyAutoHands's executor primitives to support this — `--env-config` already
  accepted an arbitrary path.
- `run_scripts`, when `mode: release`, `pip install`s the Stage-2 TestPyPI
  wheels at the rehearsed version and puts **no** library source on
  `PYTHONPATH`, still executing from inside the workspace checkout.
- `verify_install_release` runs `heart/checks/verify_install.sh --testpypi
  --version <version>` A–F against the same wheels.
- `emit_release_report` reshapes Build's `aggregate_results.py` report.json
  (via the new `heart/validate.py::to_stage_report` / `pyauto-heart validate
  --emit-stage-report`) into the `{"stage": "integrate", ...}` contract
  `--ingest` expects, folding in the `verify_install` result and the
  Release-Agent-supplied `commit_shas`, and uploads it as the
  `release-stage-report` artifact for the Release Agent to feed into
  `pyauto-heart validate --ingest`.

  The `verify_install` result is folded in **both directions**: a failure forces
  the stage to `fail`, and the sidecar itself is carried into the stage report as
  evidence, which `--ingest` persists to `~/.pyauto-heart/verify_install.json` —
  the install-verification readiness leg's input. Until 2026-07-15 only the
  failure direction existed, so a passing Stage 3 check was discarded and the leg
  reported `install verification not run` no matter how often it passed, holding
  Heart at YELLOW.

`mode: release` is scoped to the `autofit`/`autogalaxy`/`autolens` workspaces
and their `*_workspace_test` siblings only — the HowTo* tutorial repos have no
`profile_release.yaml` and stay out of the release-fidelity script matrix
(they are still exercised under `mode: smoke`, unchanged).

## Why a distinct profile

The per-PR smoke gate and the release gate are different jobs:

- **smoke** — a fast structural / integration check: *does the model compose and
  the script run end-to-end?* Cheap, runs on every PR.
- **release** — *does the exact source about to ship, installed from the built
  wheel, pass at release fidelity?* Slow, runs only for a release rehearsal.

Both tiers' `config/build/profile_smoke.yaml` today default to smoke values
(`PYAUTO_TEST_MODE=2`, `PYAUTO_SMALL_DATASETS=1`, `PYAUTO_DISABLE_JAX=1`,
`PYAUTO_FAST_PLOTS=1`). That is correct for a per-PR smoke and wrong for a
release gate. The two must be **named, distinct profiles** so a release run
cannot silently inherit smoke fidelity.

## The `release` profile (wired in M3)

The intended release-fidelity env is already documented in
`PyAutoHands/.github/workflows/release.yml`:

| Tier                 | `PYAUTO_TEST_MODE`               | `PYAUTO_SMALL_DATASETS` | `PYAUTO_FAST_PLOTS` |
|----------------------|----------------------------------|-------------------------|---------------------|
| user workspaces      | `1` (reduced iterations)         | `1` (capped grids)      | `1`                 |
| `*_workspace_test`   | `0` (real searches, `n_like_max`)| unset (full-res)        | unset               |

Per-script `overrides:` still layer **on top of** the selected profile — e.g.
unset `PYAUTO_SMALL_DATASETS` for full-resolution FITS scripts, keep JAX on for
`jax_likelihood_functions/` and `jax_substructure/`. The profile sets the floor;
overrides remain per-script.

The full library env-var surface (canonical entry:
`PyAutoNerves/autoconf/test_mode.py`) is 13 `PYAUTO_*` vars, not the 4 smoke
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
   never touches a wheel — the exact blind spot that let a direct git-URL
   dependency break every TestPyPI upload for weeks. The release run MUST
   `pip install` the TestPyPI wheels published by the M1 rehearsal and put **no**
   source on `PYTHONPATH`.

2. **Wheel-based config resolution.** autoconf resolves the *workspace's*
   `config/` only when scripts run from inside the workspace checkout; a bare
   wheel falls back to the library's *packaged* defaults. So the run must
   `pip install` the wheels **but still execute scripts from within the workspace
   checkout** (for `config/` + `dataset/`), with no source on `PYTHONPATH`.

The integration run also performs `verify_install` A–F against the same wheels.
Check B reuses that exact TestPyPI version: it must install and import on Python
3.12 and 3.13, while Python 3.11 must reject it specifically because its
`Requires-Python` metadata is `>=3.12`. A *pinned* rejection is the only evidence
that this release holds the floor, because an unpinned install may select an
older compatible one instead.

Check F is the Colab gate. It builds a `python3.12` venv holding the package
set Google's Colab actually ships — resolved from the `googlecolab/backend-info`
`pip-freeze.txt` manifest, fetched live and cached at
`$HEART_STATE_DIR/colab_pip_freeze.txt`, with a vendored snapshot as the last
fallback — and then runs the injected setup cell verbatim on top of it. Only the
part of the stack's with-deps closure that Colab also ships is pre-installed, so
the setup cell's real `pip install ... --no-deps` is observed doing what it does
on Colab. The gate then walks the installed libraries' declared requirements,
AST-scans every `import` in their source at any depth and imports each one for
real: an unguarded import of a module Colab will not have, a headline `autofit`
search that cannot be constructed, or a declared dependency that is both absent
and imported is a **FAIL**. Version conflicts, guarded imports and never-imported
gaps are WARNs carried in the report. Until 2026-09-15 Check F installed the
stack **with** dependencies before running the cell, so the bootstrap's misses
(`corner`, `optax`, `xxhash`, `blackjax`) were invisible to it and shipped.

In a rehearsal (`--version`) that cell necessarily bootstraps the **released**
stack: it is injected verbatim, and neither its own `pip install autonerves` nor
the released `setup_colab.setup()` it calls is pinned, so pip cannot select the
candidate's dev pre-release. Audited as-is the gate would measure the release
and never the wheels about to ship — and a broken released bootstrap would hold
Heart RED over the release carrying its fix. So since 2026-09-17 Check F audits
the released bootstrap first and reports it as an advisory **`WARN`** row that
never fails the run or moves `ready`, then re-pins the venv to the candidate
(all five PyAuto packages at the rehearsal version, `--no-deps`, `setup_colab`
reloaded and its own package list reinstalled) and gates on that. A continuous
run without `--version` is unchanged: one audit, one verdict.

Check B then requires the unpinned install to be refused as well. That is a
separate guarantee, and it was not met until 2026-08-19: `pip install autolens`
on 3.11 backtracked to `2026.7.29.1` and installed a stale, JAX-less stack
silently. The `2026.7.29.1.post1` tombstone refuses it, and this leg is what
stops the backtrack reopening.

That TestPyPI A–F result feeds the install-verification readiness leg (see
"How the gate enforces these"), tagged `index: testpypi`. It proves the wheels
**about to ship** install, which is the right evidence for a release gate, and
is reported as such rather than as proof that installing from PyPI works today.

For pre-publication development, `verify_install B --version <version>
--find-links <wheel-dir>` exercises the same exact-version logic against local
wheels while retaining PyPI for third-party dependencies. Its sidecar is
labelled `index: find-links`, so it cannot be mistaken for PyPI or TestPyPI
release evidence. Heart retains a fresh passing local result as development
evidence but reports STALE until a PyPI or TestPyPI verification replaces it;
a failing local artifact remains RED.

## How the gate enforces these

`heart/validate.py` records `profile` and per-repo `commit_shas` in
`validation_report.json`; `heart/readiness.py` then requires, for GREEN:

- `validation_outcome == pass` (else RED for `fail`; **STALE** for `incomplete`,
  which means nothing failed and the rehearsal evidence is simply absent — a
  report predating the field falls back to `release_ready == false` → RED, so
  the gate fails closed on evidence it cannot classify),
- `profile == release` (else YELLOW — a smoke-fidelity run is not a release gate),
- `commit_shas` matching the current `main` HEADs (else YELLOW — stale source),
- freshness (a rehearsal older than `VALIDATION_STALE_DAYS` is YELLOW).

The install-verification leg is separate and reads
`~/.pyauto-heart/verify_install.json`, written either by a local `pyauto-heart
verify_install --report-json` invocation (`index: pypi`, `testpypi`, or
`find-links`, according to its flags) or by `--ingest` folding the block out of
a Stage 3 artifact (`index: testpypi`). Both release indexes satisfy the leg;
the index is named in every reason line so the verdict states which install path
it actually verified. Development-only `index: find-links` evidence does not
satisfy this release gate.

Before M3 (or if the Release Agent only runs the M1 rehearsal and skips
dispatching the `release-integrate.yml` channel), an ingested
rehearsal-only report still (correctly) gates YELLOW: the source was built and
TestPyPI-installed, but not yet exercised at release fidelity. `mode: release`
is what supplies the `integrate` stage that flips this to GREEN-eligible.
# Bounded investigation of a failed wheel run

`release-diagnostic.yml` investigates the saved `rectangular_rtu.py` timeout
from run `37199991757`. It is dispatched manually
with `repeats` of 2, 4 or 6 total trials. It does not run the integration matrix.

The manifest `diagnostics/release-37199991757.json` preserves the final 115
package versions from the failed installation log, the exact workspace and
Hands commits, Python 3.12.14 and the rehearsed library SHAs. In particular,
the failed release workflow downgraded JAX/JAXlib to 0.10.2, while the earlier
passing source retimes used 0.11.2. This difference is a confound to test,
not a demonstrated cause.

The runner resolves the pinned workspace's release profile through pinned
Hands, verifies package versions and wheel import origins, and executes only
the affected script in fresh processes. It captures `/proc` thread states,
`py-spy --native` and GDB stacks at 120 seconds, terminates at 300 seconds,
and stops on any candidate failure. The comparison interleaves original 0.10.2
controls and 0.11.2 candidates on the same runner and dataset, changing only
JAX/JAXlib in separate virtual environments. Original control timeouts remain
in the artifacts; a successful comparison requires a native-confirmed Cholesky
pool stall in at least one control and every candidate passing. Passing controls
alone are inconclusive, not evidence of a remedy. Diagnostic artifacts preserve the manifest,
pip installation URLs/hashes, actual runtime and environment provenance,
stdout/stderr, native capture failures, results and newly generated FITS data.
The original runner image, hardware, wheel hashes and generated FITS data
were not retained; the receipt exposes these reproduction limits.

These are **diagnostic results only**. Neither passing repetitions nor a green
workflow clears the failed release. This workflow emits no release stage
report and never calls validation ingest. Do not raise caps, quarantine the
script, or identify a causal repair without reproducible or native evidence.

The first exact hosted replay, [37205459198](https://github.com/PyAutoLabs/PyAutoHeart/actions/runs/37205459198), passed once in 9.905s and then stalled after 2.6s compilation. Both native tools captured all four Eigen workers waiting in `BlockingCounter::Wait` through `ParallelBatchMap`, `CholeskyFactorization` and `lapack_dpotrf_ffi`. The committed [native witness](../diagnostics/release-37199991757-native.txt) includes the raw artifact's SHA256. There are no FFT/ducc0 frames.

[JAX 0.10.2](https://github.com/jax-ml/jax/blob/jax-v0.10.2/jaxlib/cpu/lapack_kernels.cc) schedules LAPACK batch chunks to the thread pool and blocks waiting for them; [JAX 0.11.2](https://github.com/jax-ml/jax/blob/jax-v0.11.2/jaxlib/cpu/lapack_kernels.cc) compiles this parallel path out of open-source builds. This is the source-grounded candidate tested by the comparison, distinct from the historical FFT workaround. No upstream report was filed.

The interleaved comparison [37206724174](https://github.com/PyAutoLabs/PyAutoHeart/actions/runs/37206724174) passed all three controls (15.724/12.671/12.733s) and all three candidates (12.359/12.668/12.167s). It correctly returned **inconclusive** because the control did not stall on that runner. This does not establish a measured failure-rate improvement. The earlier exact replay supplies the native-confirmed failure; the source change removes that captured blocking path, and the candidate executions verify this script with the same release wheels.

The smoke, integration and notebook dependency recipes now require matching JAX/JAXlib `>=0.11.2,<0.12`, preventing the old `<0.11` override from downgrading into the captured LAPACK deadlock. The original 115-package diagnostic control remains pinned to 0.10.2. No timeout, test selection or release-readiness rule changes. Full release integration has not been repeated: the failed validation remains authoritative until a separately authorized post-merge validation succeeds.

### FFT pool stalls on the 2026-10-05 release wheels

The later integration `37286150846` still timed out after compilation on
JAX/JAXlib 0.11.2. Diagnostic incident `37286150846` preserves its 115 package
pins, Python 3.12.14 and workspace/Hands commits. It runs the multi-galaxy
shapelet fit in three pristine workspace copies, stopping on the first failure.
Incident `37217670612` separately replays one Galaxy MGE fit; its passing
replay did not reproduce the full integration failure.

[Control 37310709808](https://github.com/PyAutoLabs/PyAutoHeart/actions/runs/37310709808)
timed out at 300.047 seconds with zero provenance errors. Its
[native witness](../diagnostics/release-37286150846-native.txt) records four
Eigen workers waiting in `ducc0::detail_threading::latch::wait` beneath
`xla::cpu::FftThunk::Execute`. The older LAPACK signatures are absent. This
supports FFT pool re-entry as the cause of this reproduced stall; it does not
establish that every timed-out script has the same cause.

The optional `disable_cpu_eigen_threads: true` input applies only to the
shapelet diagnostic. It appends `--xla_cpu_multi_thread_eigen=false`, retaining
existing flags; the default control and older incident routes are unchanged.
The release script runner applies the same flag before resolving workspace
profiles. This extends the existing test-workspace workaround to release CI
without changing user-library defaults, package pins, script selection or
timeouts. It can reduce CPU parallelism; timing should be assessed separately.

[Workaround replay 37312018327](https://github.com/PyAutoLabs/PyAutoHeart/actions/runs/37312018327)
passed all three fresh fits in 47.383, 45.188 and 45.237 seconds, with zero
provenance errors and the flag recorded for each child. The package pins and
source revisions match the native-confirmed control. These separate hosted
runs support the mitigation for this reproduced FFT stall; they are not a
same-run interleaved failure-rate measurement or a full-matrix result.

Diagnostic success is not release clearance. A complete integration run and
canonical ingestion must still replace the failed validation evidence.

### User-facing compatibility policy (2026-10-04)

The package repair preserves `>=0.7,<0.12` while excluding `0.10.*` and `0.11.0`.
The LAPACK parallel batch path first appears in tagged 0.10.0 source and is disabled
in open-source builds from 0.11.1; 0.9.2 lacks the path. Only 0.10.2 was observed
deadlocking here. Exclusions of the other releases are source-based precautions.
The two diagnostic endpoints are 0.9.2 and 0.11.2, not a new blanket minimum of
0.11.2 or certification of every permitted older version.

Nerves owns the exclusions and retains its Intel-macOS marker. Each repaired
Fit/Array/Galaxy/Lens/CTI package directly requires `autonerves>2026.10.4.1` to
prevent resolver fallback to an older, permissive Nerves. Hands' three explicit
JAX installs share the exclusions. Heart's integration baseline remains the
0.11.2 series; `jax-compatibility.yml` exercises both endpoints against the saved
wheel stack and original likelihood, plus numerical/sampler and historical FFT
witnesses. The original incident comparison is now manual-only to avoid an
unnecessary old-runtime replay on every PR update.

**Release ordering:** merge/publish the policy-bearing Nerves before the repaired
family wheels. The strict bound rejects the old release's `.devN` and `.postN`
variants too; a same-day rehearsal must use a higher base version chosen by the
human release process. Source CI builds use the existing `setup.py` development
version `9999.0.0.dev0` and satisfy the guard. None of this retroactively repairs
published wheels or prevents an unconstrained installer from selecting an entirely
older family. Existing lockfiles require an explicit repaired-family upgrade.

[Local receipts](../diagnostics/jax-compatibility-20261004.json) record:

- Original rectangular likelihood, including its existing JIT/vmap assertions:
  three CPU passes and one actual CUDA pass per version, with the same incident
  wheels, 113 non-JAX pins and saved dataset. Local Python is 3.12.10, not the
  incident's 3.12.14; the hosted workflow pins 3.12.14.
- Imaging light-profile gradient checks pass for both versions against finite
  differences. Independent regularized-likelihood/Cholesky, gradient, NUFFT/direct
  DFT, Optax optimizer and short BlackJAX NUTS execution checks pass on CPU and GPU.
  The older endpoint also passes with NumPy2.0.0/SciPy1.13.0. The short NUTS run
  tests finite execution, not posterior convergence.
- The historical FFT reproducer completes 20 iterations on both endpoints with
  `--xla_cpu_multi_thread_eigen=false`. The separate FFT workaround is retained.
- All six real built wheels have the intended metadata. 25 live resolver cases
  across the five consumer packages accept both endpoints and reject 0.10.2,
  0.11.0 and old Nerves, with older published candidates still available. A fresh `--ignore-installed`
  resolver run without prereleases also selects the protected family and JAX0.9.2
  using synthetic stable local wheels (no release or version selection).

**Incomplete broader check:** `point_source/jax_grad/gradient.py` completed its
solved-source finite-difference checks on both versions, then exceeded the 300s
local diagnostic cap in the later portion on both. Logs/native-capture attempts
and hashes are retained. This does not establish a version-specific regression
or its cause, and is not a passing result. No cap or test selection was changed
in production. Local timings were collected with other validation running and
must not be interpreted as a performance comparison. Full release validation
remains failed and must be assessed separately after human merge/authorization.
