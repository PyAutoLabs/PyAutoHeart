---
name: cli-noise-clean
description: Audit CLI output from tests and workspace scripts for warnings, stray prints, and library noise, then report fixes.
---

Audit CLI output across PyAuto repos for noise — warnings, stray print statements, verbose logging, and third-party library messages — then report what needs fixing.

A **PyAutoHeart** check — output-noise classification is part of the validation surface Heart owns. It reports; it does not apply the fixes itself.

## Usage

```
$cli-noise-clean                # full audit: pytest + workspace scripts
$cli-noise-clean pytest         # pytest collection + short run only
$cli-noise-clean scripts        # workspace scripts only
```

In Claude, invoke the same skill as `/cli_noise_clean`.

## Environment Variables

All workspace script runs use these to keep execution fast:

```bash
PYAUTO_TEST_MODE=2
PYAUTO_WORKSPACE_SMALL_DATASETS=1
PYAUTO_DISABLE_JAX=0            # JAX ON so we catch JAX-specific noise
```

## Steps

### 1. Determine audit scope

- **Default (no argument):** run both pytest and workspace script audits
- **`pytest`:** skip workspace scripts
- **`scripts`:** skip pytest

### 2. Pytest noise audit

For each library repo (PyAutoConf, PyAutoFit, PyAutoArray, PyAutoGalaxy, PyAutoLens):

```bash
cd <repo_path>
python -m pytest test_<pkg>/ -x -q --co 2>&1 | grep -v "^test_\|^<\|^$\|^=\|collected"
```

This captures noise emitted during **test collection** (import-time warnings, JAX init, SQLAlchemy mapper config). Then run a small subset of actual tests to catch runtime warnings:

```bash
python -m pytest test_<pkg>/ -x -q --tb=no -W all 2>&1 | head -100
```

Collect all warning/noise lines and deduplicate.

### 3. Workspace script noise audit

Run representative scripts from each workspace. These exercise real code paths (modeling, simulation, plotting) that unit tests may skip.

**Scripts to run:**

| Workspace | Scripts |
|-----------|---------|
| `autolens_workspace` | `imaging/simulators/start_here.py`, `imaging/modeling/start_here.py` |
| `autogalaxy_workspace` | `imaging/simulators/start_here.py`, `imaging/modeling/start_here.py` |
| `autofit_workspace` | `howtofit/chapter_1/tutorial_1_models.py` |

For each script:

```bash
cd <workspace_path>
PYAUTO_TEST_MODE=2 PYAUTO_WORKSPACE_SMALL_DATASETS=1 \
NUMBA_CACHE_DIR=/tmp/numba_cache MPLCONFIGDIR=/tmp/matplotlib \
  python <script> 2>&1 | grep -viE "^(result|model|log_likelihood|figure|saved)" | head -80
```

Capture stderr separately to isolate library warnings from expected stdout:

```bash
PYAUTO_TEST_MODE=2 PYAUTO_WORKSPACE_SMALL_DATASETS=1 \
NUMBA_CACHE_DIR=/tmp/numba_cache MPLCONFIGDIR=/tmp/matplotlib \
  python <script> 2>/tmp/cli_noise_stderr.txt 1>/tmp/cli_noise_stdout.txt
cat /tmp/cli_noise_stderr.txt
```

### 4. Classify noise

Group findings into categories:

| Category | Example | Fix |
|----------|---------|-----|
| **Third-party warnings** | JAX CUDA plugin, numpy deprecation | `filterwarnings` in `pyproject.toml` |
| **ORM/DB warnings** | SQLAlchemy relationship overlaps | Fix relationship definitions |
| **Stray print()** | VRAM profiling, aggregator output | Convert to `logger.debug()` or gate behind verbosity |
| **Verbose logging** | INFO-level autoarray messages | Set log level to WARNING in conftest.py |
| **Docstring warnings** | Badly formatted numpydoc | Fix the docstring |
| **JAX compilation** | Tracing/compilation messages during modeling | Filter or configure JAX logging level |

### 5. Report

Output a summary table:

```
CLI Noise Audit — <date>

Noise sources found: N

| # | Category        | Source                          | Message (truncated)              | Repo       | Fix                    |
|---|----------------|---------------------------------|----------------------------------|------------|------------------------|
| 1 | Third-party    | jax_plugins.xla_cuda12          | cuda_plugin_extension not found  | all        | pyproject filterwarning |
| 2 | ORM warning    | autofit/database/model/fit.py   | relationship will copy column    | PyAutoFit  | add overlaps param     |
| ...                                                                                                                         |

New since last audit: M
Previously seen: K
```

If this is a follow-up audit (previous results exist in `/tmp/cli_noise_baseline.txt`), diff against the baseline and highlight new entries.

### 6. Save baseline

Save the current findings to `/tmp/cli_noise_baseline.txt` so future runs can detect regressions:

```bash
# Format: category|source|message_hash|repo
echo "<findings>" > /tmp/cli_noise_baseline.txt
```

## Notes

- This skill is **read-only** — it reports noise but does not fix it. The operator decides what to fix.
- JAX is intentionally left enabled (`PYAUTO_DISABLE_JAX=0`) because JAX-specific noise is a primary target.
- Modeling scripts with `PYAUTO_TEST_MODE=2` exit after the first likelihood evaluation, so they run in seconds but still exercise the full import + setup + first-iteration path.
- If a script hangs or takes more than 60 seconds, kill it and note the timeout in the report.
