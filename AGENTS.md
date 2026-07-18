# PyAutoHeart — Agent Guidance

PyAutoHeart is the **health and vital-signs authority** of the PyAuto organism:
it owns health checks, release-readiness checking, workspace validation, URL
hygiene, generated-artifact/noise classification, and continuous monitoring of
the PyAuto repos. `pyauto-heart readiness` is the authoritative "is it safe to
release?" gate.

## The boundary

The organs, boundaries and the `Brain → Heart (gate) → Build (execute)` call
chain are defined once in `PyAutoBrain/ORGANISM.md`. Heart's side of it:
**observer only** — it reads and emits the authoritative
green/stale/yellow/red verdict, never writes into other repos, and never
triggers Build. **STALE** is the freshness tier: nothing known-bad, but some
evidence is missing or expired — the remedy is re-running a check, never
fixing code, and evidence whose last known result was adverse stays
yellow/red (`heart/readiness.py` docstring is the canonical definition).
Releases still require GREEN; the dev-ship gate (PyAutoBrain `AUTONOMY.md`
leg 4) treats STALE as passing because an evidence gap is organism-scope,
not branch-scope.

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

See [`docs/internals.md`](docs/internals.md) for Heart's internals — the check
framework, the <30s tick budget, how to add a check, and the hard rules
(observer-only, colour coding, atomic state writes). Read it when changing
Heart's own code, not by default.

<!-- repos_sync:history:begin -->
## Never rewrite history

Never rewrite pushed history on any repo with a remote — no `git init` over a
tracked repo, no force-push to `main`, no fresh-start "Initial commit", no
`filter-repo` / `filter-branch` / `rebase -i` on pushed branches. To get a
clean tree: `git fetch origin && git reset --hard origin/main && git clean -fd`.
<!-- repos_sync:history:end -->
