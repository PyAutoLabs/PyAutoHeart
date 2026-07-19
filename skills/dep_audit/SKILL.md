---
name: dep-audit
description: Audit dependency version caps across all PyAuto repos, compare against PyPI latest, and report a risk-tiered upgrade summary.
---

Audit dependency version constraints across the PyAuto ecosystem, compare them against the latest versions on PyPI, and produce a risk-tiered upgrade report.

A **PyAutoHeart** check — dependency drift is part of the health/readiness surface Heart owns. Read-only: it reports, it does not edit pins.

## Usage

```
$dep-audit              # full audit of all repos
$dep-audit PyAutoFit    # audit a single repo
```

In Claude, invoke the same skill as `/dep_audit`.

## Steps

### 1. Collect current constraints

For each library repo (PyAutoNerves, PyAutoFit, PyAutoArray, PyAutoGalaxy, PyAutoLens), read `pyproject.toml` and extract:

- Every entry in `dependencies`
- Every entry in `[project.optional-dependencies]` sections
- The `requires-python` constraint

Parse each dependency string into: package name, version constraint (floor, cap, exact pin).

Also check `PyAutoHands/requirements.txt` for any additional constraints.

### 2. Query PyPI for latest versions

For each unique package found in step 1, query the latest version:

```bash
pip index versions <package> 2>/dev/null | head -1
```

If `pip index` is unavailable, fall back to:

```bash
pip install <package>== 2>&1 | grep -oP 'from versions: .+' | grep -oP '[\d.]+' | tail -1
```

### 3. Check currently installed versions

```bash
pip list 2>/dev/null | grep -iE "<package1>|<package2>|..."
```

### 4. Classify each dependency

For each dependency, determine its status:

| Status | Meaning |
|--------|---------|
| **Current** | Installed version equals latest, cap allows it |
| **Cap stale** | Cap blocks a newer version that exists on PyPI |
| **Pinned** | Exact pin (`==`), may or may not be latest |
| **Uncapped** | No upper bound — always gets latest |
| **Floor only** | Has a minimum but no maximum |

### 5. Risk-tier the upgrades

Group packages that have stale caps or pins into tiers:

**Tier 1 — Safe cap bumps** (no known API breaks):
- Package has only minor/patch versions between current cap and latest
- No deprecation warnings in changelogs for APIs we use

**Tier 2 — Needs code verification** (possible API changes):
- Major version boundary crossed (e.g. 6.x → 7.x)
- Known deprecations in the version range
- Package is used extensively in the codebase

**Tier 3 — Major migration** (breaking changes likely):
- Package has a major API overhaul (e.g. JAX 0.4 → 0.5+)
- Would require code changes across multiple repos

**Tier 4 — Intentionally pinned** (do not upgrade without reason):
- Scientific samplers (dynesty, nautilus, zeus, ultranest, emcee) — version changes can alter results
- Packages pinned for compatibility with specific platforms

### 6. Produce the report

Display a summary table:

```
Dependency Audit — <YYYY-MM-DD>
================================

Python: >=3.12

| Package | Owner Repo | Constraint | Installed | Latest | Status | Tier |
|---------|-----------|------------|-----------|--------|--------|------|
| numpy | PyAutoNerves | >=1.24,<3 | 2.4.4 | 2.4.4 | Current | — |
| scipy | PyAutoFit | <=1.17.1 | 1.14.0 | 1.17.1 | Cap stale | 1 |
| jax | PyAutoNerves | >=0.4.35,<0.10 | 0.4.38 | 0.9.2 | Cap stale | 2 |
...

Tier 1 — Safe cap bumps: <count> packages
Tier 2 — Needs verification: <count> packages
Tier 3 — Major migration: <count> packages
Tier 4 — Intentionally pinned: <count> packages
Already current: <count> packages
```

### 7. Identify cross-repo constraint conflicts

Check for cases where the same package has different constraints in different repos (e.g. scipy capped differently in PyAutoFit vs PyAutoArray). Flag these as needing synchronisation.

### 8. Check for unused constraints

For each dependency, do a quick grep across the owning repo to verify it's actually imported. Flag any dependencies that appear in `pyproject.toml` but have zero imports — these may be candidates for removal.

### 9. Post to issue (optional)

If the user provides an issue number or if there's an active task in `PyAutoMind/active.md`, offer to post the audit results as an issue comment.

## Dependency Ownership Map

This table defines which repo "owns" each constraint (i.e., where the version spec lives in `pyproject.toml`). Downstream repos inherit via the dependency chain.

| Package | Owner | Inherited By |
|---------|-------|-------------|
| numpy | PyAutoNerves | all |
| jax, jaxlib | PyAutoNerves | all |
| jaxnnls | PyAutoNerves | all |
| scipy | PyAutoFit, PyAutoArray | Galaxy, Lens |
| matplotlib | PyAutoFit (uncapped), PyAutoArray (floored) | Galaxy, Lens |
| astropy | PyAutoArray, PyAutoGalaxy | Lens |
| scikit-image | PyAutoArray | Galaxy, Lens |
| scikit-learn | PyAutoArray | Galaxy, Lens |
| SQLAlchemy | PyAutoFit | — |
| dynesty | PyAutoFit | — |
| nautilus-sampler | PyAutoGalaxy, PyAutoLens | — |
| colossus | PyAutoGalaxy | Lens |

This map should be updated when constraints move between repos.

## Notes

- This skill is read-only — it produces an audit report but does not change any files.
- To act on the audit, use `$start-dev` (`/start_dev` in Claude) with a prompt
  file describing the upgrades.
- Run this quarterly or before any major release to catch stale constraints early.
- The "installed" column reflects the current venv, which may lag behind what the constraints allow. A fresh `pip install` in a clean venv would pull the latest allowed version.
