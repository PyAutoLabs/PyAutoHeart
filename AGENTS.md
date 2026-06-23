# PyAutoPulse — Agent Guidance

PyAutoPulse is the **health authority** of the PyAuto release ecosystem: it owns
all health and release-readiness checking plus continuous monitoring of the
PyAuto repos. `pyauto-pulse readiness` is the authoritative "is it safe to
release?" gate.

## The boundary (one description, mirrored in all three repos)

- **PyAutoPulse — the health authority.** All health/readiness logic lives here:
  version drift, install-path, URL hygiene, CI/worktree/timing monitoring.
  `pyauto-pulse readiness` is the **authoritative** green/yellow/red verdict —
  the single "is it safe to release?" gate. Pulse is an observer: it reads and
  emits verdicts; it never writes into other repos and never triggers Build.
- **PyAutoBuild — the executor.** Packaging, tagging, notebook generation, and
  PyPI publication via `release.yml`. Build runs **no** readiness checks of its
  own and never re-derives a gate decision; it just executes.
- **PyAutoAgent — the brain.** Hosts the agents that connect the two. It owns no
  checks and no release steps; it gates on Pulse and delegates execution to
  Build.

## The call chain (always this order)

```
Agent  →  Pulse (gate)  →  Build (execute)
```

The agent asks `pyauto-pulse readiness --json`; only on a **green** verdict does
it trigger Build's release. Pulse never triggers Build; Build never re-derives a
gate decision the agent already made.

## Where things live

- Continuous checks (cheap, in the <30s `tick`): repo state, CI status, open PRs,
  worktree drift, script timing, version skew.
- Deep checks (on-demand / cloud cron, never in the tick): `verify_install` (pip
  & conda install-path) and the URL-hygiene sweep (`url_sweep` + the central
  `.github/workflows/url-check.yml`).
- `readiness` rolls these into the authoritative verdict (URL hygiene is
  monitoring only and does **not** gate it).

See [`CLAUDE.md`](CLAUDE.md) for Pulse's internals — the check framework, the
<30s tick budget, how to add a check, and the hard rules (observer-only, colour
coding, atomic state writes).
