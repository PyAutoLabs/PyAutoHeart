# Review Release: Triage Release Readiness

Review the latest PyAutoBuild release evidence, ask PyAutoHeart for the
authoritative readiness verdict, and route the human to release, refresh, fix,
or investigate. Build artifacts explain what ran; they never determine whether
the organism is ready.

A **PyAutoHeart** skill: Build executes, Heart judges, and the Brain release
conductor coordinates any subsequent release action.

## Steps

### 1. Fetch the latest release run

List recent completed and in-progress runs:

```bash
gh run list --workflow=release.yml --repo PyAutoLabs/PyAutoBuild --limit 5 \
  --json databaseId,status,conclusion,createdAt,url
```

Use the most recent completed run by default. If the newest run is still in
progress, report that and let the user choose whether to wait or inspect the
previous completed run.

### 2. Read the build evidence

Read job conclusions from the selected run and fetch failed logs when needed:

```bash
gh api --paginate \
  'repos/PyAutoLabs/PyAutoBuild/actions/runs/<run-id>/jobs?per_page=100' \
  --jq '.jobs[] | {id, name, status, conclusion}'
gh run view <run-id> --repo PyAutoLabs/PyAutoBuild --log-failed
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
  dispatch another release from review of a live run.
- **Unknown mode**: investigate the job graph. Do not release.
- **STALE**: list the exact evidence Heart requires refreshing and route to the
  corresponding validation command. Do not release.
- **YELLOW / RED**: route each reason and build failure to a fix or
  investigation. Do not offer a release override.

For fixes, use `$start-library` or `$start-workspace` (`/start_library` or
`/start_workspace` in Claude) after filing a concise PyAutoMind prompt through
`$intake` (`/intake` in Claude). Environment and workflow failures normally
target PyAutoBuild; source or script failures target the owning library or
workspace.

If the user chooses investigation, show the full traceback, relevant source,
recent file history, and correlated PR diff until the failure has a defensible
locus. Heart remains the final readiness authority after any fix or refresh.
