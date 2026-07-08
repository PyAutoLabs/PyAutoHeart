# Worktree status — the /health worktrees leg

Procedure for the `/health worktrees` leg (the former standalone `worktree_status` skill).

Diagnostic skill. Lists every worktree root under `$PYAUTO_WT_ROOT` (default `~/Code/PyAutoLabs-wt`), the task it belongs to per `active.md`, and the branch and dirty state of every real worktree inside it.

A **PyAutoHeart** diagnostic — Heart owns the health/diagnostic surface; it
**reads** the PyAutoMind work registry (`active.md`) but never mutates it. The
execution-environment model is in PyAutoBrain `skills/WORKFLOW.md`.

This skill is **read-only**. It never creates, removes, or modifies worktrees. Use `/start_library` / `/ship_library` / the post-merge cleanup flow for mutations.

## Steps

### 1. Source the worktree helper

```bash
source admin_jammy/software/worktree.sh
```

This provides `worktree_list_claimed`, `worktree_root_path`, and the `PYAUTO_WT_ROOT` variable used below.

### 2. Enumerate worktree roots on disk

```bash
ls -1 "$PYAUTO_WT_ROOT" 2>/dev/null
```

If the directory doesn't exist or is empty, report "No active worktrees." and stop.

### 3. For each worktree root, inspect its contents

For every directory `$PYAUTO_WT_ROOT/<task>/`, identify:

- **Real worktrees** (entries that are a directory with a `.git` file — note: linked worktrees have `.git` as a FILE, not a dir):
  ```bash
  for entry in "$PYAUTO_WT_ROOT/<task>"/*; do
    [[ -L "$entry" ]] && continue
    [[ -e "$entry/.git" ]] || continue
    # $entry is a real worktree
  done
  ```
- For each real worktree, capture:
  - Branch: `git -C "$entry" branch --show-current`
  - Dirty? `git -C "$entry" status --porcelain | head -1` (non-empty = dirty)
  - Unpushed? `git -C "$entry" log @{u}.. --oneline 2>/dev/null | wc -l` (0 = up to date with origin, -1/error = no upstream)
- The activate script: does `$PYAUTO_WT_ROOT/<task>/activate.sh` exist?

### 4. Cross-reference with active.md

Run `worktree_list_claimed` and build a lookup of `task → list of claimed repos`.

For each worktree root on disk, find the matching task in the lookup. Flag mismatches:

- **Orphan worktree root:** directory exists but no entry in `active.md`. Might be leftover from a crashed session.
- **Missing worktree:** `active.md` lists a task with a `worktree:` field, but the directory is gone. Task state is stale.
- **Repo mismatch:** `active.md` says task X claims PyAutoFit, but the worktree root contains no real PyAutoFit worktree (or vice versa).

### 5. Display the dashboard

Group output by task. Example format:

```
Worktree Status
===============

Active Worktrees
----------------

jax-search-logging  (~/Code/PyAutoLabs-wt/jax-search-logging)
  issue: https://github.com/rhayes777/PyAutoFit/issues/42
  activate: source ~/Code/PyAutoLabs-wt/jax-search-logging/activate.sh
  repos:
    PyAutoFit    feature/jax-search-logging  clean     (3 commits unpushed)
    PyAutoArray  feature/jax-search-logging  2 dirty   (pushed)

psf-oversampling  (~/Code/PyAutoLabs-wt/psf-oversampling)
  issue: https://github.com/Jammy2211/PyAutoArray/issues/50
  activate: source ~/Code/PyAutoLabs-wt/psf-oversampling/activate.sh
  repos:
    PyAutoArray  feature/psf-oversampling  clean  (up to date)

Warnings
--------
ORPHAN: ~/Code/PyAutoLabs-wt/old-experiment exists but is not in active.md
  → inspect and remove with `worktree_remove old-experiment` if stale.

MISSING: active.md lists 'abandoned-task' with worktree ~/Code/PyAutoLabs-wt/abandoned-task, but the directory does not exist.
  → remove the stale entry from active.md.
```

If there are no warnings, omit the Warnings section entirely.

### 6. Suggest next actions

Only if warnings are present:

- **Orphan:** "Run `worktree_remove <task>` if this work is abandoned. Otherwise, re-register it in `active.md`."
- **Missing:** "Edit `PyAutoMind/active.md` to remove the stale entry, or re-create the worktree with `worktree_create <task> <repos...>` if the work is resuming."
- **Repo mismatch:** "Either the worktree needs more repos (`git worktree add` inside the root) or `active.md` needs correcting. Investigate before the next `ship_library` run."

## Notes

- This skill is read-only.
- It assumes `admin_jammy/software/worktree.sh` is sourceable from the current directory (run from the PyAutoLabs root, or pass `PYAUTO_MAIN` explicitly).
- "Dirty" means any uncommitted change, including untracked files.
- "Unpushed" is counted against the branch's upstream; a branch with no upstream is reported as "no upstream" rather than a commit count.

## Execution environments

Worktrees only exist in a `local-dev` environment (see PyAutoBrain
`skills/WORKFLOW.md`). In a `web-github` / `analysis-only` session there are no
local worktrees, so this skill degrades gracefully to a registry + GitHub view:

1. Read `PyAutoMind/active.md` and display each task's `status:` and `repos:`
   fields.
2. For a task with a `worktree:` field, note the worktree lives on the local-dev
   machine (not accessible here).
3. Check branch existence via the GitHub API per repo:
   ```bash
   gh api repos/<owner>/<repo>/branches/<branch> --jq '.name' 2>/dev/null
   ```
4. Display a simplified dashboard (registry + which branches exist on GitHub),
   omitting dirty/unpushed checks (those need a local worktree).

This is the same diagnostic with a different state source — there is no special
"mobile" mode.
