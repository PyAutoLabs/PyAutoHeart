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

Download the selected run's report:

```bash
gh run download <run-id> --repo PyAutoLabs/PyAutoBuild \
  --name release-report --dir /tmp/release-report
```

Read `release-report.json` and `release-report.md`. If the artifact is absent,
fall back to job conclusions:

```bash
gh run view <run-id> --repo PyAutoLabs/PyAutoBuild --json jobs \
  --jq '.jobs[] | {name, conclusion}'
```

Treat this report as evidence only. Do not derive `READY` or `NOT READY` from
its pass/fail totals.

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
Build evidence: <passed / failed / skipped / timeout counts>
```

The Heart verdict is authoritative even when it differs from the selected
build's conclusion. Releases require GREEN. Never acknowledge YELLOW or infer
GREEN inside this skill.

### 4. Explain adverse evidence

For each build failure, show its file, classification, short error, relevant
traceback tail, and recent-PR correlation. Group repeated failures by likely
locus: library source, workspace, environment, timeout, or release workflow.

If the build filed an `ai-analysis` issue, inspect its comments as additional
context:

```bash
gh issue list --repo PyAutoLabs/PyAutoBuild --label ai-analysis --limit 5 \
  --json number,title,url,comments
gh api repos/PyAutoLabs/PyAutoBuild/issues/<number>/comments --jq '.[].body'
```

Also list skipped scripts that appear stale or risky. GUI-only skips may remain
informational; skips for known bugs or missing capabilities require an open,
current reason.

### 5. Route by the Heart verdict

- **GREEN**: present the evidence and ask the human whether to invoke the Brain
  `release` skill (`/release` or `/build` in Claude, depending on the requested
  mode). Manual releases remain `human-required`.
- **STALE**: list the exact evidence Heart requires refreshing and route to the
  corresponding validation command. Do not release.
- **YELLOW / RED**: route each reason and build failure to a fix or
  investigation. Do not offer a release override.

For fixes, use the current harness's `start-library` or `start-workspace` skill
(`/start_library` or `/start_workspace` in Claude) after filing a concise
PyAutoMind prompt through `intake`. Environment and workflow failures normally
target PyAutoBuild; source or script failures target the owning library or
workspace.

If the user chooses investigation, show the full traceback, relevant source,
recent file history, and correlated PR diff until the failure has a defensible
locus. Heart remains the final readiness authority after any fix or refresh.
