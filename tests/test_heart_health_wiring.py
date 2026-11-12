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


def test_the_cloud_step_uses_the_minted_app_token_without_fallback():
    """Cross-repo artifact downloads need a token with actions:read on THAT
    repo. The job mints an owner-wide read-only App token for it; there is no
    fallback to the repo-scoped token — a missing credential fails the job."""
    _, job = _job()
    mint = next(s for s in job["steps"] if s.get("id") == "app_token")
    assert mint["uses"].startswith("actions/create-github-app-token@")
    assert mint["with"]["permission-actions"] == "read"
    assert "repositories" not in mint["with"]
    token = _cloud_step(job)["env"]["GH_TOKEN"]
    assert token == "${{ steps.app_token.outputs.token }}"
    assert "||" not in token


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


# --- the unit-timings ingest is wired the same way (#206) --------------------

def test_the_cloud_step_runs_the_unit_timings_ingest():
    """The libraries' per-test artifact needs the same daily ingest the
    workspaces' per-script one gets, or the two board rows go back to sleep."""
    _, job = _job()
    assert "bash heart/checks/unit_timings.sh" in _cloud_step(job)["run"]
    assert (CHECKS / "unit_timings.sh").is_file()


def test_the_append_step_records_the_unit_slice():
    _, job = _job()
    body = job["steps"][_step_index(job, "Append today's observations")]["run"]
    assert '--unit-timings "$HEART_STATE_DIR/unit_timings.json"' in body


def test_unit_timings_reads_the_record_and_never_a_previous_board():
    """The committed record is the ONLY baseline for this check: the published
    board never carried per-test rows, so there is nothing to fall back to."""
    body = (CHECKS / "unit_timings.sh").read_text()
    assert '--record-dir "$HEART_HOME/timings"' in body
    assert "--prev-board" not in body


def test_the_snapshot_folds_in_the_unit_timings_rollup():
    state_py = (Path(__file__).resolve().parent.parent / "heart" / "state.py").read_text()
    assert '"unit_timings": _read_json_or_default(' in state_py
    assert '"unit_timings.json"' in state_py


def test_cloud_reads_validation_before_aggregation():
    _, job = _job()
    body = _cloud_step(job)['run']
    assert 'python -m heart.checks.test_run' in body
    assert 'python -m heart.checks.cloud_validation' in body
    assert _step_index(job, 'Run cloud-safe checks') < _step_index(job, 'Aggregate snapshot')


# --- event triggers refresh the feed (PyAutoHeart#340) ----------------------

def _triggers(name):
    # PyYAML parses the bare `on:` key as boolean True.
    return yaml.safe_load((WORKFLOWS / name).read_text())[True]


def test_the_daily_cron_stays_as_the_backstop():
    on = _triggers("heart-health.yml")
    assert on["schedule"] == [{"cron": "0 5 * * *"}]
    assert "workflow_dispatch" in on


def test_a_heart_refresh_dispatch_and_a_release_rehearsal_refresh_the_feed():
    on = _triggers("heart-health.yml")
    assert on["repository_dispatch"] == {"types": ["heart-refresh"]}
    assert on["workflow_run"]["workflows"] == ["Release Integrate"]
    assert on["workflow_run"]["types"] == ["completed"]
    # The watched workflow exists under that exact display name.
    names = {yaml.safe_load(p.read_text()).get("name") for p in WORKFLOWS.glob("*.yml")}
    assert "Release Integrate" in names


def test_bursts_of_events_coalesce_rather_than_cancel_a_publish():
    data, _ = _job()
    assert data["concurrency"]["cancel-in-progress"] is False


def test_library_ci_on_main_sends_the_heart_refresh_event():
    """The sender lives in the reusable library gate every library calls, so a
    library merge refreshes the feed with no edit in the library repos."""
    jobs = yaml.safe_load((WORKFLOWS / "lib-tests.yml").read_text())["jobs"]
    job = jobs["heart-refresh"]
    assert set(job["needs"]) == {"unittest", "unittest-nojax"}
    cond = job["if"]
    assert "always()" in cond
    assert "github.event_name == 'push'" in cond
    assert "github.ref == 'refs/heads/main'" in cond
    # Never reds the library's required check.
    assert job["continue-on-error"] is True
    mint = next(s for s in job["steps"] if s.get("id") == "app_token")
    assert mint["with"]["permission-contents"] == "write"
    send = job["steps"][-1]["run"]
    assert "/PyAutoHeart/dispatches" in send
    assert "event_type=heart-refresh" in send
    # The event type sent is the one heart-health.yml listens for.
    assert "heart-refresh" in _triggers("heart-health.yml")["repository_dispatch"]["types"]
