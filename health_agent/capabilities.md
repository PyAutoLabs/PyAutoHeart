# PyAutoHeart capability audit

Human-readable companion to [`capabilities.yaml`](./capabilities.yaml). This is
the audit the Health Agent task required: every health-related asset PyAutoHeart
exposes, so the agent knows what it can ask Heart for. The YAML is the source the
agent reads; this page is for humans.

Audited at commit `a2543d0` (Rename PyAutoPulse to PyAutoHeart).

## CLI surface (`bin/pyauto-heart`)

| Subcommand | Purpose | Health role |
|---|---|---|
| `watch` / `live` | foreground monitor loop (live board on a tty) | runs the tick on a schedule |
| `tick` | one-shot refresh of all checks into `state.json` | produces the snapshot |
| `stop` | kill the daemon (`--all` sweeps orphans) | operational |
| `status` | coloured snapshot (`--json`, `--quiet`) | the agent's detail query |
| `readiness` | the authoritative green/yellow/red verdict + score | **the gate** |
| `logs` | tail the daemon log | operational |
| `fix` | emit a Claude remediation bundle (`ci`/`dirty`/`drift`/`timing`) | remediation entry point |
| `verify_install` | deep pip/conda install-path check (slow) | deep readiness signal |
| `url_check` / `url_sweep` | offline URL-hygiene guard / ecosystem sweep | monitoring only |

`pyauto-pulse` is a compatibility wrapper for the former name; it routes here.

## Checks

**Continuous** (cheap, every `<30s` tick — `heart/tick.sh`):

- **repo_state** (`checks/repo_state.sh`) — branch / dirty (real vs generated) /
  ahead / behind, per repo. RED when a library is off `main`, has uncommitted
  source, or is behind origin.
- **ci_status** (`checks/ci_status.sh`) — latest CI conclusion per repo via `gh`.
  RED when a library's latest conclusion is not `success`.
- **open_prs** (`checks/open_prs.sh`) — open PR count + max age. YELLOW at
  `>= 7d`.
- **worktree_drift** (`checks/worktree_drift.sh`) — `PyAutoLabs-wt/` dirs vs
  PyAutoMind `active.md` (orphan / missing / dirty). Monitoring.
- **script_timing** (`checks/script_timing.py`) — per-script duration vs rolling
  baseline (`>1.5x` slow, `>3x` regression). YELLOW.
- **test_run** (`checks/test_run.py`) — reads PyAutoBuild's
  `test_results/latest/report.json` (the workspace-validation verdict). YELLOW
  when not passing / stale / unknown (workspace debt is advisory, never a hard
  block).
- **version_skew** (`checks/version_skew.py`) — each workspace's pinned version
  vs the installed library. RED on AHEAD / MISMATCH / BAD; YELLOW on
  BEHIND / UNKNOWN.
- **noise** (`heart/noise.py`) — splits `git status` into genuine source drift
  vs regenerated-artifact noise so only real drift drives gates.

**Deep** (slow, on-demand / cloud cron, never in the tick):

- **verify_install** (`checks/verify_install.sh`) — pip & conda install-path
  checks A–E in throwaway envs across Python versions. RED if last run
  `ready==false`; YELLOW if stale (`>14d`) or never run. Moved here from
  PyAutoBuild — install verification is Heart's job.
- **url_check / url_sweep / url_check_live** — offline regex guard, ecosystem
  sweep, and live HTTP reachability audit. **Monitoring only — never gates
  readiness.**

## Readiness verdict (`heart/readiness.py`)

`compute(snapshot)` is a pure function rolling the snapshot into one verdict:

- **RED** — library CI failing / off main / dirty / behind; version skew
  AHEAD / MISMATCH / BAD; install verification `ready==false`.
- **YELLOW** — workspace validation not passing (standing debt, advisory),
  script-timing regressions, stale open PRs / parked scripts, skew BEHIND, stale
  or unrun install verification, and any *unknown* (missing report / library
  absent). An unknown is never silently green.
- **GREEN** — none of the above.

`red dominates yellow dominates green`. The `score` (0–100) is advisory/sortable
only — the colour is the gate. Persisted to `~/.pyauto-heart/release_ready.json`.

## GitHub workflows (`.github/workflows/`)

- **lib-tests.yml** — reusable unit-test workflow for the 5 libraries (3.12/3.13);
  each library's `main.yml` is a thin caller. Heart owns the test definition.
- **pulse-health.yml** ("Heart Health") — daily cloud-safe `ci_status` +
  `open_prs` sweep; opens/updates one `[heart-health]` issue, closes when clean.
- **url-check.yml** ("URL Check (central)") — weekly ecosystem URL sweep into one
  `[url-check]` issue. Monitoring only.
- **workspace-validation.yml** — heavy scripts + notebooks validation against the
  libraries' current `main`; writes the `report.json` the `test_run` check +
  `readiness` consume. Reuses Build's executor primitives — does not duplicate
  them.

## State (`~/.pyauto-heart/`)

`state.json` (aggregated snapshot), `release_ready.json` (the verdict), per-repo
sidecars, rolling `timings/`, `url_check.json`, `verify_install.json`, daemon
`heart.pid`, and `logs/heart.log`.

## Documentation describing health checks

`README.md` (user-facing), `AGENTS.md` (the Brain/Heart/Hands boundary + call
chain), `CLAUDE.md` (internals: the check framework, the `<30s` tick budget, how
to add a check, the observer-only / colour / atomic-write hard rules).
