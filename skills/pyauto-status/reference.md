# Active Work Dashboard — the `/health status` leg

> Reference procedure, not a top-level command. This was `/pyauto-status`; it is
> now reached as **`/health status`** (see `PyAutoBrain/skills/health/health.md`).
> `/health` is the single health door; this file is the dashboard leg it drives.

Show a dashboard of all active work across the PyAuto repos. Use it to check for
conflicts before starting work, or when resuming a session to see what's in
flight.

This is a **PyAutoHeart** status view — Heart owns the health/readiness surface;
it **reads** the PyAutoMind work registry (`active.md` / `planned.md` /
`complete.md`) but never mutates it. Read-only.

## Steps

### 1. Read the work registry (Mind)

Read all three registry files (schemas in `PyAutoMind/README.md`):

- **`PyAutoMind/planned.md`** — issued tasks blocked from starting (each has a
  `blocked-by` field).
- **`PyAutoMind/active.md`** — tasks in progress (`status`, optional
  `library-pr`, `worktree`, `repos:`).
- **`PyAutoMind/complete.md`** — recently completed (show the last 5 for context).

### 2. Live-scan repos and worktrees

Scan each canonical checkout (PyAutoConf, PyAutoFit, PyAutoArray, PyAutoGalaxy,
PyAutoLens, autofit_workspace, autogalaxy_workspace, autolens_workspace,
autolens_workspace_test, euclid_strong_lens_modeling_pipeline, HowToLens):

```bash
git -C <repo_path> branch --show-current
git -C <repo_path> status --short
git -C <repo_path> log --oneline -1
```

Then scan every task worktree root:

```bash
source admin_jammy/software/worktree.sh
for entry in "$PYAUTO_WT_ROOT/<task>"/*; do
  [[ -L "$entry" ]] && continue
  [[ -e "$entry/.git" ]] || continue      # linked worktrees use a .git FILE
  git -C "$entry" branch --show-current
  git -C "$entry" status --short
done
```

Record task + repo for each worktree hit.

### 3. Cross-reference and display

Compare live git state with the registries and print a scannable dashboard —
active work first, warnings second, idle repos last:

```
Planned (queued, not yet started)
  <task> (#<n>) — classification, affected repos, blocked-by, ready/blocked verdict

Active Work
  <task> (#<n>) — <status>
    Worktree: ~/Code/PyAutoLabs-wt/<task>
    <repo>: feature/<task>  (pushed, PR #<n> open | dirty, N files | N ahead)

Recently Completed
  <task> (#<n>) — completed <date>, library-pr: <url>

Warnings
  UNREGISTERED / ORPHAN / MISSING / STALE / DIRTY (see step 5)

Idle Repos (on main, clean)
  <list>
```

### 3a. URL-check status (Heart health surface)

For each PyAuto repo, query the weekly URL-check cron's tracking issue:

```bash
gh issue list --repo PyAutoLabs/<repo> \
  --search '"[url-check]" in:title' --state open --json number,title,updatedAt
```

Show a per-repo `⚠ #<n>` (with age) or `✓ clean` line. URL hygiene is a Heart
check; the per-repo allowlist is `.url_check_allowlist.txt`.

### 4. Re-check planned tasks

For each `planned.md` task, test whether its `blocked-by` conflict still exists:
blocking task no longer in `active.md` → **Ready to start**; else **Still
blocked**. This is the key value of the dashboard for planned work.

### 5. Flag issues

- **Unblocked planned tasks** — conflicts resolved, ready to start.
- **Conflicts** — a repo claimed by two `active.md` `worktree:` entries.
- **Unregistered main-checkout work** — a canonical checkout on a feature branch
  not referenced by any task's `worktree:` tree (cross-check worktree branches
  first; a worktree on `feature/foo` is expected, not stale).
- **Orphan worktree roots** — a dir under `$PYAUTO_WT_ROOT` with no `active.md`
  entry.
- **Missing worktrees** — an `active.md` `worktree:` pointing at a gone dir.
- **Stale entries** — a task whose repos are back on `main` everywhere.
- **Dirty repos** — uncommitted changes (note which task they belong to).

### 6. Suggest actions

Per flag: ready planned task → `/start_library` / `/start_workspace`; conflict →
finish one task first; unregistered → register in `active.md` or reset to main;
stale → remove from `active.md` or re-run `/start_library`.

## Notes

- Read-only — never modifies files or branches. Run at the start of any resumed
  session.
- **Execution environments** (see PyAutoBrain `skills/WORKFLOW.md`): in a
  web-github / analysis-only session with no local tree, pull `PyAutoMind` for the
  registry, then read branch/PR state via the GitHub API
  (`gh api repos/<owner>/<repo>/branches/<branch>`, `gh pr list --head <branch>`)
  instead of local `git` and worktree scans. Same dashboard, different state
  source — there is no special "mobile" mode.
