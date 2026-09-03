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

<!-- repos_sync:remote:begin -->
## Remote sessions (Claude Code on web and mobile)

Three facts, all measured in a web/mobile container, where this file is loaded
and little else is. They ride in every organ because a session may hold any
subset of them — and the session that needs this most is the one holding
several, which is exactly the session no hook fires in.

- **Bootstrap in the first turn, unconditionally** — before the first test
  command, not as a remedy once something looks wrong:

  ```
  bash PyAutoMind/scripts/session_bootstrap.sh          # ~10s cold, ~1s warm
  bash PyAutoMind/scripts/session_bootstrap.sh --check  # report only
  ```

  A session holding several organs registers no SessionStart hook — Claude Code
  reads project hooks from the project directory, which in that layout is the
  repos' *parent*, not a repo — so nothing has set this session up. It was once
  phrased as a remedy keyed to `No module named pytest` or collection
  `ImportError`s naming `yaml`; that symptom stopped appearing when the
  container image moved to Python 3.12, while the environment is still wrong in
  ways that read like a bad command rather than a stale session (`pytest -n
  auto` → `unrecognized arguments: -n`). The bootstrap also **unshallows the
  clones**: a remote session clones shallow, and `git merge-base --is-ancestor`
  then answers "not an ancestor" for a commit whose ancestry is merely absent —
  the answer the ship and close-out procedures act on when proving a branch
  merged.

- **Then run the suite in parallel.** 4 cores, subprocess-heavy suites, no
  single slow test: about 3.5x. `python3 -m pytest -q -n auto`, with
  `pytest-xdist` supplied by the bootstrap above.

- **There is no `gh`, and installing one does not help.** A remote session
  reaches GitHub through the `mcp__github__*` tools, already scoped to the
  session's repos. `gh` installs in two seconds and is a trap: it authenticates,
  then 403s every repo-scoped call, because the egress proxy serves neither the
  REST repo paths nor GraphQL beyond a pinned set of PR-review operations — a
  binary that looks healthy and fails everything that matters. It also defeats
  the surface probe, which keys off `gh auth status`. Read
  `PyAutoBrain/skills/GITHUB_ACCESS.md` at the top of any run that touches
  GitHub; it maps each `gh` operation onto its MCP tool. Spell that path from
  the workspace root, as written: a multi-organ session is cwd'd at the repos'
  *parent*, so a bare `skills/…` reads as a missing file rather than a missing
  repo prefix.
<!-- repos_sync:remote:end -->

<!-- repos_sync:history:begin -->
## Never rewrite history

Never rewrite pushed history on any repo with a remote — no `git init` over a
tracked repo, no force-push to `main`, no fresh-start "Initial commit", no
`filter-repo` / `filter-branch` / `rebase -i` on pushed branches. To get a
clean tree: `git fetch origin && git reset --hard origin/main && git clean -fd`.
<!-- repos_sync:history:end -->

<!-- repos_sync:deliverable:begin -->
## Sessions end at their deliverable

A session ends when it reports its deliverable — never arm anything that
outlives the turn to wait for CI, a review or a merge: no `send_later`, no
`subscribe_pr_activity`, no `CronCreate`, no `ScheduleWakeup`, no `/loop`, no
`RemoteTrigger` create/update/run. Judge once, report, stop; the human re-runs
`/prm` (or the batch review) when it is green. Measured: five batch members
armed hourly check-ins on 2026-08-31, and a mobile `/prm` re-armed a 60-minute
`send_later` hourly all night on 2026-09-03 with no task active, draining usage.
<!-- repos_sync:deliverable:end -->
