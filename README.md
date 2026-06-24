# PyAutoHeart

The **health and vital-signs layer** of the PyAuto organism: a
continuous-monitoring daemon that owns release-readiness checking, workspace
validation, URL hygiene, generated-artifact/noise classification, and the
ecosystem's quality gates. `pyauto-heart readiness` is the authoritative "is it
safe to release?" verdict. In the organism metaphor, PyAutoBrain plans and
orchestrates, PyAutoHeart checks health and readiness, and PyAutoHands executes
build/release/deployment work. See `AGENTS.md` for the boundary.

## What it does

Polls 19 PyAuto repos every N minutes for:

- **Repo state** — branch / dirty / ahead / behind
- **CI status** — latest workflow conclusion per repo (via `gh run list`)
- **Open PRs** — count + max age, classified by staleness
- **Worktree drift** — `~/Code/PyAutoLabs-wt/` dirs vs `active.md` claims
- **Script timing** — per-script duration regressions vs a rolling baseline
- **Test run** — the latest PyAutoBuild release-run verdict (ready / counts /
  stale parked scripts), read from `test_results/latest/report.json`
- **Version skew** — each workspace's pinned version vs the installed library
  (AHEAD = a release blocker; BEHIND = caution)

…then rolls them into a single **release-readiness verdict**
(`pyauto-heart readiness`): green / yellow / red + a 0–100 score and the
reasons behind it. This is **advisory** — PyAutoBuild keeps its own
authoritative release gates; Heart just makes the picture continuous.

Caches results to `~/.pyauto-heart/state.json` for fast `status` reads.
Surfaces drift the day it appears instead of the day before a release.

All output is colour-coded:

- **green** — passing / clean / nominal
- **yellow** — warning / stale / mild drift
- **red** — failing / actionable now

`NO_COLOR=1` or `--no-color` strips colours for pipes / CI / redirection.

## Compatibility during the rename

`pyauto-heart` and the `heart` Python package are the canonical names. The
former `pyauto-pulse` command, `pulse` Python package, and `PULSE_*`
environment variables remain as compatibility aliases for existing automation.
They route to the Heart implementation and can be migrated gradually.

## Quick start

PyAutoHeart is **not** pip-installed. Like the other PyAuto repos it runs
from its checkout via `PYTHONPATH` + `PATH` in `~/.bashrc`:

```bash
# Setup (one time) — add to ~/.bashrc, then `source ~/.bashrc`
export PYTHONPATH="$PYTHONPATH:$HOME/Code/PyAutoLabs/PyAutoHeart"   # makes `import heart` work
export PATH="$HOME/Code/PyAutoLabs/PyAutoHeart/bin:$PATH"          # puts the CLI on PATH
```

```bash
# One-off refresh
pyauto-heart tick

# Pretty-print the cached state
pyauto-heart status

# Run the daemon in a tab (Ctrl-C to stop)
pyauto-heart watch                 # default 300s interval; live board on a tty
pyauto-heart watch 60              # tick every 60s
pyauto-heart live                  # force the live clear-and-redraw board
HEART_INTERVAL=120 pyauto-heart watch
```

**Live vs plain.** On a terminal, `watch` clears the screen each cycle,
streams the tick's per-repo progress, renders the colour board, then counts
down to the next tick. When stdout is not a tty (an agent runs it, or output
is piped), it degrades to plain streamed text. Force either with
`HEART_LIVE=1` (live) / `HEART_LIVE=0` (plain); `live` is shorthand for the
former.

**Dirty vs generated.** Many workspaces commit regenerated artifacts
(`*.fits`, `tracer.json`, build-generated `README.md`, …) that perpetually
show as dirty. Heart splits these out: `dirty=<n>` counts genuine source
changes (drives yellow), while `+<n> gen` is the regenerated-artifact noise
(informational, dimmed). The patterns live in `config/repos.yaml`
(`noise_globs`). Untracked directories are treated as generated output too.

After PyAutoBuild#TBD lands, the same commands work via the unified CLI:

```bash
autobuild watch
autobuild status
autobuild tick
autobuild fix ci PyAutoFit
```

## Release readiness

`pyauto-heart readiness` answers "is it safe to release?" from the cached
state, as a single verdict computed on every tick (and shown at the top of
`status`):

```bash
pyauto-heart readiness            # verdict + score + reasons
pyauto-heart readiness --json     # machine-readable (for scripts / skills)
```

- **RED** — a real release blocker: any of the 5 libraries has failing CI, is
  off `main`, has uncommitted source changes, or is behind origin; the latest
  Build test run is not ready; or a workspace is pinned **ahead** of its
  installed library.
- **YELLOW** — caution: timing regressions, stale PRs, stale parked scripts, a
  workspace pinned **behind**, or an *unknown* (e.g. no recent test-run report
  — never silently treated as green).
- **GREEN** — none of the above.

Red always dominates yellow. The verdict is written to
`~/.pyauto-heart/release_ready.json`. It is **advisory**: PyAutoBuild keeps its
own authoritative gates (`verify_workspace_versions`, the release pipeline) —
Heart just surfaces the same signals continuously so drift is visible the day
it appears, not the day of a release.

## Automation (hybrid CI layer)

Heart runs in two places so it doesn't need a babysat terminal:

- **Cloud** — `.github/workflows/pulse-health.yml` runs the cloud-safe checks
  (CI status + open PRs, pure `gh` API) on a daily schedule and opens-or-updates
  a single `[heart-health]` tracking issue when anything is red/degraded,
  closing it when clean. No agent, no Slack, no secret beyond `GITHUB_TOKEN`.
- **Local** — a guarded block in `~/.bashrc` starts `pyauto-heart watch` in the
  background on your first interactive login, so the local-only checks
  (repo state, worktree drift, script timing, test run, version skew) keep
  refreshing while a shell is open. It's idempotent (the daemon's pidfile guard
  prevents duplicates); opt out with `PYAUTO_HEART_NO_AUTOSTART=1`.

  The bashrc daemon only ticks while a WSL shell is open. For reboot-survival,
  register a Windows Task Scheduler job that calls the local tick on a timer:

  ```powershell
  # From an elevated PowerShell, runs every 15 min even with no shell open:
  schtasks /create /tn "PyAutoHeart tick" /sc minute /mo 15 ^
    /tr "wsl -u jammy bash -lc 'pyauto-heart tick'"
  ```

## Daily usage pattern

The bashrc auto-start usually means a daemon is already running. To watch it
live in a tab:

```bash
pyauto-heart live                 # live clear-and-redraw board
```

…and leave it running. Glance at the tab to see the current state.
When something turns red:

```bash
pyauto-heart fix ci <repo>          # CI failure
pyauto-heart fix dirty <repo>       # clean up a dirty tree (real vs generated)
pyauto-heart fix drift              # worktree state
pyauto-heart fix timing <project>   # script timing regressions
```

…emits a context bundle + Claude Code invocation you can paste/run.

## Architecture

```
bin/pyauto-heart                 # dispatcher (mirrors autobuild's pattern)

heart/
  _color.sh                      # ANSI helpers (bash side)
  heart_color.py                 # ANSI helpers (Python side)
  _common.sh                     # shared globals + helpers
  daemon.sh                      # the foreground watch loop
  tick.sh                        # one refresh cycle
  state.py                       # atomic JSON cache I/O
  status.py                      # pretty-print cached state
  readiness.py                   # composite release-readiness verdict
  fix.py                         # emit Claude invocations on demand
  noise.py                       # dirty real-vs-generated classifier
  checks/
    repo_state.sh
    ci_status.sh
    open_prs.sh
    worktree_drift.sh
    script_timing.py
    test_run.py                  # latest PyAutoBuild report.json verdict
    version_skew.py              # workspace pin vs installed library

config/
  repos.yaml                     # polled repos + thresholds + noise globs

.github/workflows/
  pulse-health.yml               # stable filename; displayed as Heart Health

tests/                           # pytest, runs in <3s
```

State cache at runtime:

```
~/.pyauto-heart/
  state.json                     # aggregated latest snapshot
  release_ready.json             # the readiness verdict
  heart.pid                      # daemon pidfile
  per-repo/<name>.<check>.json   # per-repo sidecars
  timings/<workspace>__<dir>__<file>.json  # rolling per-script duration history
  logs/heart.log                 # daemon stderr + tick events
  worktree_drift.json
  script_timing.json
  test_run.json
  version_skew.json
```

## Configuration

`config/repos.yaml` lists the 18 polled repos and the classification
thresholds. To add or remove a repo, edit the file and restart the daemon
(`pyauto-heart stop && pyauto-heart watch`).

## Tests

```bash
# Tests run with the venv's pytest — no install needed (stdlib + PyYAML only).
pytest tests/ -v
```

## Roadmap

- v1.3 (shipped): `stop --all` (pgrep-based) to recover a daemon whose pidfile
  was lost
- next: desktop notification dispatch; stash staleness
- v2: TUI panel (textual/rich) with hotkey drill-down
- v3: cross-machine sync (sqlite); team-shared cache

## Relationship to other PyAuto repos

- **PyAutoBuild** — provides the primitives (`autobuild run_all`, `autobuild url_check`, etc.) and writes `test_results/latest/report.json`, which Heart reads for the test-run check and readiness verdict. Heart shells out / reads files but never imports PyAutoBuild Python. The readiness verdict is advisory — Build keeps its own release gates.
- **PyAutoPrompt** — Heart reads `active.md` for worktree drift detection (read-only).
- **admin_jammy** — Heart sources `software/worktree.sh` for `PYAUTO_WT_ROOT` etc.

Heart never writes to any other repo. State lives entirely under `~/.pyauto-heart/`.
