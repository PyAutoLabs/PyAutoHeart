"""tests/test_release_run.py — the release-channel freshness decision.

`decide()` is the pure core (PyAutoHeart#128): given the persisted
validation_report, the per-run-id sidecar cache, and the latest cloud run
record, it must refresh exactly when the completed cloud run is newer than the
ingested evidence — never regressing a fresher local ingest, never
re-downloading an already-ingested run, and treating a failed rehearsal as
evidence to ingest, not a gap to skip.
"""

from __future__ import annotations

from heart.checks import release_run as rr


def _run(run_id=7, status="completed", conclusion="success",
         created="2026-07-30T05:16:28Z"):
    return {"databaseId": run_id, "status": status, "conclusion": conclusion,
            "createdAt": created, "url": f"https://example/runs/{run_id}"}


def test_no_runs():
    assert rr.decide(None, None, None)["action"] == "no-runs"


def test_in_progress_never_ingests():
    d = rr.decide(None, None, _run(status="in_progress", conclusion=None))
    assert d["action"] == "in-progress"


def test_fresh_completed_run_with_no_local_report_ingests():
    d = rr.decide(None, None, _run())
    assert d["action"] == "ingest" and d["run_id"] == 7


def test_failed_rehearsal_still_ingests():
    # release_ready=false is evidence, not an evidence gap.
    d = rr.decide(None, None, _run(conclusion="failure"))
    assert d["action"] == "ingest"


def test_already_ingested_run_is_cached():
    d = rr.decide(None, {"last_ingested_run_id": 7}, _run())
    assert d["action"] == "cached"


def test_fresher_local_ingest_is_never_regressed():
    current = {"ts": "2026-07-30T17:53:15+00:00"}          # local ingest AFTER the run
    d = rr.decide(current, None, _run(created="2026-07-30T05:16:28Z"))
    assert d["action"] == "local-fresher"


def test_newer_cloud_run_beats_older_local_report():
    current = {"ts": "2026-07-29T17:53:15+00:00"}
    d = rr.decide(current, None, _run(created="2026-07-30T05:16:28Z"))
    assert d["action"] == "ingest"


def test_unparseable_timestamps_fail_toward_ingest():
    # Freshness unknown → refresh (ingest is idempotent; staleness is the harm).
    d = rr.decide({"ts": "not-a-time"}, None, _run())
    assert d["action"] == "ingest"


def test_new_run_id_supersedes_old_cache():
    d = rr.decide({"ts": "2026-07-29T00:00:00+00:00"},
                  {"last_ingested_run_id": 6}, _run(run_id=7))
    assert d["action"] == "ingest"
