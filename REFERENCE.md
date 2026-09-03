# PyAutoHeart reference

The operational detail behind [README.md](README.md): the full CLI surface, the
run model, state layout, checks, verdict semantics, and the board's surfaces.
(Absorbed the former `health_agent/capabilities.md`; the machine-readable
contract agents consume is `health_agent/capabilities.yaml`.)

## Running

Heart runs from its checkout — no pip install. `bin/pyauto-heart` resolves its
own repo root; put it on `PATH` (the PyAutoBrain installer does). State lives
under `~/.pyauto-heart/` (override with `HEART_STATE_DIR`); the checkout itself
is never written by checks (the observer rule — the one exception is
`pyauto-heart publish`, which commits Heart's OWN `state/devbox_board.json`).
Which repos are polled, and with what thresholds, is `config/repos.yaml`.
Tests: `pytest tests/`.

## CLI surface (`bin/pyauto-heart`)

| Subcommand | Purpose | Health role |
|---|---|---|
| `watch` / `live` | foreground monitor loop (live board on a tty) | runs the tick on a schedule |
| `tick` | one-shot refresh of all checks into `state.json` | produces the snapshot |
| `stop` | kill the daemon (`--all` sweeps orphans) | operational |
| `status` | coloured snapshot (`--json`, `--quiet`) | the agent's detail query |
| `readiness` | the authoritative green/stale/yellow/red verdict + score | **the gate** |
| `dashboard` | the unified board (`--oneline/--md/--md-brief/--html/--json/--badge`, `--cloud`, `--devbox`) | every surface, one renderer |
| `publish` | push the distilled dev-box board into the repo | fills the cloud page's grey rows |
| `logs` | tail the daemon log | operational |
| `fix` | emit a Claude remediation bundle (`ci`/`dirty`/`drift`/`timing`) | remediation entry point |
| `validate` | ingest release-validation artifacts into `validation_report.json` | release rehearsal evidence |
| `freeze` | set/clear the release-validation freeze flag (`--set/--until/--clear/--show`) | the validation window, made visible |
| `smoke` | isolated local workspace smoke suites | deep validation |
| `verify_install` | deep pip/conda install-path check (slow) | deep readiness signal |
| `url_check` / `url_sweep` | offline URL-hygiene guard / ecosystem sweep | monitoring only |

## The board and its surfaces

`heart/dashboard.py` is the ONE renderer: every surface is a projection of the
same `state.json` + `release_ready.json`, so they cannot disagree.

- **Pages board** — <https://pyautolabs.github.io/PyAutoHeart/>, published daily
  by `heart-health.yml`. Blockers link the repo and the failing run, and carry
  one-tap 📋 buttons copying a ready-made `/bug …` Claude prompt; grey
  dev-box-only rows say what they watch and copy the observe command. An
  **evidence gap** carries its remedy instead: ⌨ copies the command that
  re-runs that check, 📋 the prompt that does the same in a chat, and the
  "clear them all" chip above the list copies ONE plan (prompt, plus a command
  chain when every gap has one) that closes the whole tier — the remedies are
  keyed off the readiness gate key, never guessed from the reason text.
- **README strip** — the `heart:begin/end` block (`--md-brief`): verdict +
  linked blockers + board link, auto-committed by the same workflow.
- **Badge** — `badge.json` on the Pages site, rendered via shields.io.
- **Terminal** — `pyauto-heart dashboard` / `status` / the `watch` daemon.
- **JSON** — `--json` (schema v3: structured `blockers` with prompts/links and,
  on a stale row, the `command` that closes it; board-level `stale_plan`;
  per-section `action`/`links`/`observed_ago`) — what the Health Agent and
  mobile consume.
- **Issue** — one `[heart-health]` tracking issue opens while cloud checks are
  degraded and closes when clean.

### Cloud-only honesty and the dev-box publish

The cloud job only observes API-safe checks (`ci_status`, `open_prs`); the
local-only families (`heart/dashboard.py::LOCAL_ONLY_FAMILIES` — worktree
drift, script/import/unit-test/test-mode timings, profiling drift, test run,
version skew, repo state) render "not observed here" rather than fake green.
`pyauto-heart publish` distills the dev box's board for those families into
`state/devbox_board.json` (states/summaries/counts only — detail lines naming
local filesystem paths are scrubbed) and pushes it; the cloud render merges the
file, stamping each row "observed Nh ago on the dev box" and letting it expire
back to grey after 48 h (`DEVBOX_FRESH_SECONDS`).

## Checks

**Continuous** (cheap, every `<30s` tick — `heart/tick.sh`):

- **repo_state** (`checks/repo_state.sh`) — branch / dirty (real vs generated) /
  ahead / behind, per repo. RED when a library is off `main`, has uncommitted
  source, or is behind origin.
- **ci_status** (`checks/ci_status.sh`) — latest CI conclusion per repo via
  `gh` (the failing run's URL is cached and surfaced on the board). RED when a
  library's latest conclusion is not `success`.
- **open_prs** (`checks/open_prs.sh`) — open PR count + max age. YELLOW at `>= 7d`.
- **worktree_drift** (`checks/worktree_drift.sh`) — `PyAutoLabs-wt/` dirs vs
  PyAutoMind `active.md` (orphan / missing / dirty). Monitoring.
- **script_timing** (`checks/script_timing.py`) — per-script duration vs rolling
  baseline (`>1.5x` slow, `>3x` regression). YELLOW.
- **test_run** (`checks/test_run.py`) — reads the workspace-validation verdict.
  YELLOW when not passing / stale / unknown (workspace debt is advisory).
- **version_skew** (`checks/version_skew.py`) — each workspace's pinned version
  vs the installed library. RED on AHEAD / MISMATCH / BAD; YELLOW on
  BEHIND / UNKNOWN.
- **noise** (`heart/noise.py`) — splits `git status` into genuine source drift
  vs regenerated-artifact noise so only real drift drives gates.

**Deep** (slow, on-demand / cloud cron, never in the tick):

- **verify_install** (`checks/verify_install.sh`) — pip, conda, and Colab
  install-path checks A–F. RED if the last run has `ready==false`; STALE if it
  is find-links-only, older than 14 days, or never run.
- **url_check / url_sweep / url_check_live** — offline regex guard, ecosystem
  sweep, and live HTTP reachability audit. **Monitoring only — never gates
  readiness.**

## Readiness verdict (`heart/readiness.py`)

`compute(snapshot)` is a pure function rolling the snapshot into one verdict:

- **RED** — library CI failing / off main / dirty / behind; version skew
  AHEAD / MISMATCH / BAD; install verification `ready==false`.
- **YELLOW** — workspace validation not passing (standing debt, advisory),
  script-timing regressions, stale open PRs / parked scripts, skew BEHIND.
- **STALE** — evidence missing or expired with nothing known-bad; the remedy is
  re-running a check, never fixing code. Evidence whose last known result was
  adverse stays yellow/red. Releases require GREEN; the dev-ship gate treats
  STALE as passing (an evidence gap is organism-scope, not branch-scope).
- **GREEN** — none of the above.

`red > yellow > stale > green`. The `score` (0–100) is advisory/sortable only —
the colour is the gate. Persisted to `~/.pyauto-heart/release_ready.json`.

## The release freeze window (`freeze`)

A release validation is a window in which the library `main` branches must not
move: a merge landing mid-validation invalidates the evidence and restales the
rehearsal (~75 minutes, measured 2026-08-29). `pyauto-heart freeze` is the flag
that says so out loud.

- **The file.** `$HEART_STATE_DIR/freeze.json` — `{reason, set_at, until,
  set_by}`, written atomically like every other sidecar. Heart is the only
  writer.
- **The verb.** `freeze --set "<reason>" --until <90m|2h|1d|ISO-8601>
  [--set-by WHO]`, `freeze --clear`, `freeze --show [--json]` (the default
  action). `--until` is **mandatory**: a freeze with no expiry never clears.
- **Expiry.** Past `until` the flag reads as clear for every consumer, while
  `--show` reports it as `expired` — a forgotten set stays visible instead of
  vanishing. An unreadable or expiry-less file also reads as clear: a freeze
  nobody can parse must not be able to block a merge.
- **Exit codes.** A *read* exits `3` while a freeze is active (so
  `pyauto-heart freeze --show` is a one-call shell gate), `0` when clear or
  expired, `2` on a usage error. `--set` always exits `0`, so a driver under
  `set -e` does not abort on the freeze it just took out.

**It is not part of the readiness verdict, deliberately.** `heart/readiness.py`
never reads it. A freeze is not a health problem, and folding it into the
colour would make every ship and release gate in the organism block on it. It
is advice — with teeth in exactly one place.

### Who sets it, who clears it, who reads it

| Call site | What it does |
|---|---|
| `PyAutoHands/skills/pre_build/pre_build.md` | **sets** it before dispatching the release workflow — the window opens there |
| `pyauto-heart validate --ingest` | **clears** it: the ingest of the evidence *is* the end of the window (never on `--out`, which is an inspection path) |
| `PyAutoHeart/skills/review_release/review_release.md` | checks it while triaging the run and **clears** a freeze the ingest did not |
| expiry | clears it on its own if none of the above happens |
| the scheduled nightly release run (`PyAutoBrain/agents/conductors/release/nightly.sh` → Hands `release.yml`) | **not wired**: it runs unattended in CI, where this dev-box state file does not exist. Set the freeze on the dev box if a nightly run's window needs to be visible locally; wiring CI would need the flag to live somewhere both can see, which is a bigger change than this one |

Readers are all in PyAutoBrain and all read-only: the `vitals` faculty prints
`FROZEN: <reason> until <ts>` as a warning line, `/prm` refuses to merge a
**library** repo's PR while it is active (`--thaw` overrides, logged to
`PyAutoMind/autonomy_log.md`), and `batch collect` carries one line. Organ and
workspace repos are not gated, and no other skill blocks on it.

## GitHub workflows (`.github/workflows/`)

- **heart-health.yml** — daily cloud sweep; renders + publishes the Pages
  board, badge, README strip; maintains the `[heart-health]` issue.
- **lib-tests.yml** / **smoke-tests.yml** / **docs-build.yml** — reusable
  workflows the libraries and workspaces call; Heart owns the definitions.
- **workspace-smoke.yml** → **workspace-validation.yml** (workflow_call body) —
  scripts + notebooks against the libraries' current `main`; the run history
  `test_run` + `readiness` consume. The release rehearsal has its own entry,
  **release-integrate.yml**, so a failed rehearsal never overwrites the smoke
  verdict (see `docs/release_validation.md`).
- **heart-tests.yml** — Heart's own pytest suite; **url-check.yml** — weekly
  URL sweep into one `[url-check]` issue.

## State (`~/.pyauto-heart/`)

`state.json` (aggregated snapshot), `release_ready.json` (the verdict),
`validation_report.json`, `freeze.json` (the release freeze window), per-repo
sidecars, rolling `timings/`, `url_check.json`, `verify_install.json`, daemon
`heart.pid`, `logs/heart.log`.

## Internals

The check framework, the `<30s` tick budget, how to add a check, and the hard
rules (observer-only, colour coding, atomic state writes):
[docs/internals.md](docs/internals.md).
