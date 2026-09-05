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
