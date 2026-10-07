# Review Release: Triage Release Readiness

Review the latest PyAutoHands release evidence, ask PyAutoHeart for the
authoritative readiness verdict, and route the human to release, refresh, fix,
or investigate. Build artifacts explain what ran; they never determine whether
the organism is ready.

A **PyAutoHeart** skill: Build executes, Heart judges, and the Brain release
conductor coordinates any subsequent release action.

## Steps

### 1. Fetch the latest release run

List recent completed and in-progress runs:

```bash
gh run list --workflow=release.yml --repo PyAutoLabs/PyAutoHands --limit 5 \
  --json databaseId,status,conclusion,createdAt,url
```

Use the most recent completed run by default. If the newest run is still in
progress, report that and let the user choose whether to wait or inspect the
previous completed run.

### 2. Read the build evidence

Read job conclusions from the selected run and fetch failed logs when needed:

```bash
gh api --paginate \
  'repos/PyAutoLabs/PyAutoHands/actions/runs/<run-id>/jobs?per_page=100' \
  --jq '.jobs[] | {id, name, status, conclusion}'
gh run view <run-id> --repo PyAutoLabs/PyAutoHands --log-failed
```

Use the Actions jobs API because the workspace's supported `gh 2.4.0` does not
expose `jobs` through `gh run view --json`.

Classify the run mode from job names and conclusions before presenting any next
action. Matrix suffixes may be present in the displayed job names.

- **Rehearsal**: `rehearsal_version` succeeded and every `release` and
  `release_workspaces` job was skipped or absent.
- **Live**: at least one `release` or `release_workspaces` job has a conclusion
  other than `skipped`, whether it passed, failed, or was cancelled.
- **Unknown**: neither pattern is established. This includes an upstream failure
  that skipped both terminal paths. Investigate; do not dispatch.

The current workflow does not publish an aggregate `release-report` artifact.
Do not invent per-script totals or tracebacks that are absent from the jobs and
logs. Treat the available run data as evidence only; never derive `READY` or
`NOT READY` from job conclusions.

### 3. Ask Heart for the verdict

Run the canonical readiness entrypoint after reading the build evidence:

```bash
pyauto-heart readiness --json
```

Display:

```text
Release Readiness Report
========================

Heart verdict: GREEN / STALE / YELLOW / RED
Reasons: <verbatim Heart reasons>
Build run: <URL>
Run mode: rehearsal / live / unknown
Build jobs: <successful / failed / skipped / cancelled counts>
```

The Heart verdict is authoritative even when it differs from the selected
build's conclusion. Releases require GREEN. Never acknowledge YELLOW or infer
GREEN inside this skill.

**Then read the freeze window.** A validation run is a window in which the
library `main` branches should not move, and `pre_build` sets a flag saying so:

```bash
pyauto-heart freeze --show          # exit 3 = still frozen, 0 = clear/expired
```

The ingest (`pyauto-heart validate --ingest`) clears it on its own, so a freeze
still standing here means the run ended without one — a rehearsal that never
ingested, a failed live run, or a dispatch that never completed. Report the
line, then clear it, because reviewing the run is the end of the window either
way:

```bash
pyauto-heart freeze --clear
```

Never clear it while the run is still **in progress** (step 1) — that is the
one case where the window is genuinely still open. An `expired` reading needs
no action beyond the clear; it means nobody closed the window and time did.

### 4. Explain adverse evidence

For each build failure, show the failing job and the error detail actually
present in `--log-failed`. Include a file, traceback tail, or recent-PR
correlation only when the logs establish it. Group repeated failures by likely
locus: library source, workspace, environment, timeout, or release workflow.

The release workflow does not emit `ai-analysis` issues or per-script skip
reports. Do not search for or present either as evidence for this run. Report
only job conclusions and details present in the selected run's logs.

### 5. Route by the Heart verdict

- **GREEN + successful rehearsal**: present the evidence and ask the human
  whether to invoke the Brain `$release` skill (`/release` or `/build` in
  Claude, depending on the requested mode). Manual releases remain
  `human-required`.
- **Live run**: report whether that release completed or failed. Never offer to
  dispatch another release from review of a live run. **On a live run that
  completed successfully, clear the pending-release chain** — see step 6.
- **Unknown mode**: investigate the job graph. Do not release.
- **STALE**: list the exact evidence Heart requires refreshing and route to the
  corresponding validation command. Do not release.
- **YELLOW / RED**: route each reason and build failure to a fix or
  investigation. Do not offer a release override.

For fixes, use `$start-library` or `$start-workspace` (`/start_library` or
`/start_workspace` in Claude) after filing a concise PyAutoMind prompt through
`$intake` (`/intake` in Claude). Environment and workflow failures normally
target PyAutoHands; source or script failures target the owning library or
workspace.

If the user chooses investigation, show the full traceback, relevant source,
recent file history, and correlated PR diff until the failure has a defensible
locus. Heart remains the final readiness authority after any fix or refresh.

### 6. Clear the pending-release chain (successful live run only)

This is the one step in the organism that establishes a release **actually
published** — `pre_build` dispatches, `/build` coordinates, and neither knows
the outcome. So this is where the merged-but-unreleased chain is cleared. Run
it only for a **live** run whose `release` / `release_workspaces` jobs
succeeded; never for a rehearsal, an unknown mode, or a failed live run.

One verb does the whole sweep, from the PyAutoMind checkout (or a Mind
worktree — the shared canonical checkout may hold another session's edits):

```bash
python3 scripts/lifecycle.py clear-released --version <v>        # dry run: the plan
python3 scripts/lifecycle.py clear-released --version <v> --apply --remove-labels
```

`<v>` is the run's published version (the bare tag, e.g. `2026.10.4.1`;
`latest` resolves the newest GitHub release). It clears **only what the tag
contains** — a PR whose merge commit is an ancestor of tag `<v>` in its
library — never "every merged labelled PR", because PRs merged after the
dispatch are still unreleased:

1. **Mind — the link.** Deletes those `- pending-release: <lib>@<pr-url>`
   lines from `active.md` rows and `complete/` records, drops a record's
   `- release-gate: <lib>` once none of that library's links remain, and
   regenerates the dashboard (`--apply`).
2. **GitHub — the label.** Drops the `pending-release` label from every
   released published-set PR, including labelled PRs the ledger never linked
   (`--remove-labels`; PyAutoHands' release workflow normally already did this
   half after the publish, so expect mostly no-ops).
3. **Workspace gates.** A `- release-gate: <lib>` on a live `active.md`
   workspace row is the human's to lift: tell the user which tasks the release
   unblocks (they are now free to merge) and delete those lines.
4. **Confirm and push.** `python3 scripts/lifecycle.py check` (add `--network`
   to have it ask GitHub) must report no `pending-release` line the release
   contains, and the regenerated `dashboard.md` must no longer list those PRs
   under **Pending release**. Commit and push Mind.

Only the published set carries the chain (PyAutoNerves, PyAutoFit, PyAutoArray,
PyAutoGalaxy, PyAutoLens — `PUBLISHED_REPOS` in `scripts/lifecycle.py`). The
schema and the division of labour are `PyAutoMind/REFERENCE.md` → "The
pending-release chain".

A release that was dispatched is not a release that published: nothing else may
clear these keys.
