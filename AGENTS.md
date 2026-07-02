# PyAutoHeart — Agent Guidance

PyAutoHeart is the **health and vital-signs authority** of the PyAuto organism:
it owns health checks, release-readiness checking, workspace validation, URL
hygiene, generated-artifact/noise classification, and continuous monitoring of
the PyAuto repos. `pyauto-heart readiness` is the authoritative "is it safe to
release?" gate.

## The boundary (one description, mirrored in all three repos)

- **PyAutoHeart — the health authority.** All health/readiness logic lives here:
  version drift, install-path, URL hygiene, CI/worktree/timing monitoring.
  `pyauto-heart readiness` is the **authoritative** green/yellow/red verdict —
  the single "is it safe to release?" gate. Heart is an observer: it reads and
  emits verdicts; it never writes into other repos and never triggers Build.
- **PyAutoHands / PyAutoBuild — the executor.** Packaging, tagging, notebook
  generation, and PyPI publication via `release.yml`. Hands runs **no**
  readiness checks of its own and never re-derives a gate decision; it just
  executes.
- **PyAutoBrain / PyAutoAgent — the brain.** Hosts the agents that connect the
  two. It owns no checks and no release steps; it gates on Heart and delegates
  execution to Hands.

## The call chain (always this order)

```
Brain  →  Heart (gate)  →  Hands (execute)
```

The brain asks `pyauto-heart readiness --json`; only on a **green** verdict does
it trigger Hands' release work. Heart never triggers Hands; Hands never
re-derives a gate decision the brain already made.

For the release-**validation** rehearsal specifically (build-and-exercise the
exact source about to ship, before promoting to PyPI — see
[`docs/release_validation.md`](docs/release_validation.md)), "Brain" above
splits into two specialist agents: the **Release Agent** orchestrates
(dispatches the TestPyPI rehearsal + the wheel-based integration run, polls,
downloads artifacts, hands them to `pyauto-heart validate --ingest`), and the
read-only **Health Agent** is then consulted to report the resulting verdict.
Heart still computes and owns the authoritative verdict either way — the
Health Agent reasons over Heart's output, it does not re-derive it. Full detail
(and the manifest the Brain agents actually read): `health_agent/capabilities.yaml`.

## Where things live

- Continuous checks (cheap, in the <30s `tick`): repo state, CI status, open PRs,
  worktree drift, script timing, version skew.
- Deep checks (on-demand / cloud cron, never in the tick): `verify_install` (pip
  & conda install-path) and the URL-hygiene sweep (`url_sweep` + the central
  `.github/workflows/url-check.yml`).
- `readiness` rolls these into the authoritative verdict (URL hygiene is
  monitoring only and does **not** gate it).

See [`CLAUDE.md`](CLAUDE.md) for Heart's internals — the check framework, the
<30s tick budget, how to add a check, and the hard rules (observer-only, colour
coding, atomic state writes).
