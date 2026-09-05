"""tests/test_heart_health_wiring.py — the daily cloud job stays wired.

The checks in `heart/checks/*.sh` are only observations if something runs them.
These tests parse heart-health.yml so a check silently dropped from the cloud
step — or a permission/token the artifact ingest needs going missing — fails
loudly here rather than as a board row that quietly stops updating.

PyYAML parses the bare `on:` key as boolean True, hence the `data[True]` reads
in the sibling wiring test; nothing here needs the triggers.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
CHECKS = Path(__file__).resolve().parent.parent / "heart" / "checks"


def _job():
    data = yaml.safe_load((WORKFLOWS / "heart-health.yml").read_text())
    return data, data["jobs"]["heart-health"]


def _cloud_step(job):
    for step in job["steps"]:
        if "Run cloud-safe checks" in step.get("name", ""):
            return step
    raise AssertionError("no cloud-safe checks step in heart-health.yml")


def test_the_cloud_step_runs_every_api_only_check():
    _, job = _job()
    body = _cloud_step(job)["run"]
    for script in ("ci_status.sh", "open_prs.sh", "ci_timing.sh",
                   "no_run_census.sh", "smoke_timings.sh"):
        assert f"bash heart/checks/{script}" in body
        assert (CHECKS / script).is_file()


def test_the_job_can_read_actions_artifacts():
    """The artifact list/download endpoints need `actions: read` — even for the
    repo's own artifacts, and the default grant does not include it."""
    data, _ = _job()
    assert data["permissions"]["actions"] == "read"


def test_the_cloud_step_prefers_the_timings_token_and_falls_back():
    """Cross-repo artifact downloads need a token with actions:read on THAT
    repo; the remedy is a secret, not a code change. Absent it the step still
    runs on the auto-provisioned token."""
    _, job = _job()
    token = _cloud_step(job)["env"]["GH_TOKEN"]
    assert "secrets.HEART_TIMINGS_TOKEN" in token
    assert "secrets.GITHUB_TOKEN" in token
    assert "||" in token


def test_the_snapshot_folds_in_the_smoke_timings_rollup():
    """A rollup nothing aggregates is a file, not a board row."""
    state_py = (Path(__file__).resolve().parent.parent / "heart" / "state.py").read_text()
    assert '"smoke_timings": _read_json_or_default(' in state_py
    assert '"smoke_timings.json"' in state_py


# --- the permanent timing record is appended and committed -------------------

def _step_index(job, needle):
    for i, step in enumerate(job["steps"]):
        if needle in step.get("name", ""):
            return i
    raise AssertionError(f"no step named like {needle!r} in heart-health.yml")


def test_the_record_append_sits_between_the_checks_and_the_aggregate():
    """Ordering is the whole safety argument: the checks' aggregate passes have
    already read the record for their baselines (and neither can be fooled by
    today's own line), so appending here is safe — and it lets the render
    census a record that already includes today."""
    _, job = _job()
    checks = _step_index(job, "Run cloud-safe checks")
    append = _step_index(job, "Append today's observations to the timing record")
    aggregate = _step_index(job, "Aggregate snapshot + compute readiness verdict")
    assert checks < append < aggregate
    body = job["steps"][append]["run"]
    assert "python -m heart.timings append" in body
    assert '--ci-timing "$HEART_STATE_DIR/ci_timing.json"' in body
    assert '--smoke-timings "$HEART_STATE_DIR/smoke_timings.json"' in body


def test_the_commit_step_carries_the_record_beside_the_readme_block():
    """A record nothing commits is a tempfile, not a record."""
    _, job = _job()
    step = job["steps"][_step_index(job, "timing record (own repo only")]
    body = step["run"]
    assert "git add README.md timings/" in body
    assert 'git status --porcelain README.md timings/' in body
    assert "docs(heart): daily board block + timing record [skip ci]" in body


def test_the_two_checks_read_the_record_before_the_previous_board():
    """The published board is the fallback; the committed record is the source
    of truth — a Pages gap must not lose the history any more."""
    for script in ("ci_timing.sh", "smoke_timings.sh"):
        body = (CHECKS / script).read_text()
        assert '--record-dir "$HEART_HOME/timings"' in body


def test_the_snapshot_folds_in_the_record_census():
    state_py = (Path(__file__).resolve().parent.parent / "heart" / "state.py").read_text()
    assert '"timings_record": _read_json_or_default(' in state_py
    assert '"timings_record.json"' in state_py
