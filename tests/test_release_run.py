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
    # local ingest AFTER the run. Carries `validation_outcome`, i.e. every report
    # written since the tri-state landed; a report predating it is re-folded once
    # (test_live_shape_migrates_integrate_only_report_written_by_the_old_code).
    current = {"ts": "2026-07-30T17:53:15+00:00", "validation_outcome": "pass"}
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


# --- how an ingested report is described in the tick line ------------------
#
# The artifact this check downloads is integrate-only, so it can never carry a
# rehearsal and its `release_ready` is false by construction. Reporting that as
# FAILED asserted a failure that had not happened.


def test_green_integrate_only_ingest_is_incomplete_not_failed():
    assert rr.resolve_outcome({
        "release_ready": False,
        "validation_outcome": "incomplete",
        "stages": {"integrate": {"status": "pass"}},
    }) == "incomplete"


def test_failed_ingest_still_reads_fail():
    assert rr.resolve_outcome({
        "release_ready": False,
        "validation_outcome": "fail",
        "stages": {"integrate": {"status": "fail"}},
    }) == "fail"


def test_passing_ingest_reads_pass():
    assert rr.resolve_outcome({"release_ready": True, "validation_outcome": "pass"}) == "pass"


def test_legacy_ingest_without_discriminator_fails_closed():
    assert rr.resolve_outcome({"release_ready": False}) == "fail"
    assert rr.resolve_outcome({"release_ready": True}) == "pass"
    assert rr.resolve_outcome(None) == "fail"
    assert rr.resolve_outcome({}) == "fail"


# --- one-time re-ingest for reports predating validation_outcome -----------


def _report(ts="2026-07-30T06:00:00Z", **kw):
    return {"ts": ts, **kw}


def test_cached_run_is_reingested_once_when_report_predates_discriminator():
    """Otherwise the RED this fix removes would never clear on its own.

    The report was folded by the old code, so it has no `validation_outcome`;
    the gate fails closed on that and stays RED. The run id is already cached,
    so nothing would re-fold it.
    """
    d = rr.decide(_report(), {"last_ingested_run_id": 7}, _run(run_id=7))
    assert d["action"] == "ingest"


def test_report_holding_rehearsal_evidence_is_never_re_folded():
    """A manual multi-stage ingest must survive the schema migration.

    Its `rehearse` stage cannot be reproduced from this check's integrate-only
    artifact, so re-folding would discard the evidence a release drive collected
    and turn a `pass` into an `incomplete`.
    """
    d = rr.decide(
        _report(stages={"rehearse": {"status": "pass"}, "integrate": {"status": "pass"}}),
        {"last_ingested_run_id": 7},
        _run(run_id=7),
    )
    assert d["action"] == "cached"


def test_cached_still_holds_once_the_report_carries_the_discriminator():
    d = rr.decide(_report(validation_outcome="incomplete"),
                  {"last_ingested_run_id": 7}, _run(run_id=7))
    assert d["action"] == "cached"


def test_local_fresher_still_holds_once_the_report_carries_the_discriminator():
    d = rr.decide(_report(ts="2026-08-01T00:00:00Z", validation_outcome="pass"),
                  None, _run(run_id=7))
    assert d["action"] == "local-fresher"


def test_live_shape_migrates_integrate_only_report_written_by_the_old_code():
    """The exact state this fix has to clear on the dev box.

    An integrate-only report, ingested after its own run (so "fresher" than it),
    run id already cached, and no discriminator — the RED would otherwise persist
    until some unrelated future run came along.
    """
    d = rr.decide(
        _report(ts="2026-08-14T15:53:14+00:00",
                stages={"integrate": {"status": "pass"}}),
        {"last_ingested_run_id": 31769743408},
        _run(run_id=31769743408, created="2026-08-14T04:23:59Z"),
    )
    assert d["action"] == "ingest"


def test_absent_report_is_unaffected_by_the_schema_check():
    """No report at all is the ordinary first-ingest path, not a stale schema."""
    assert rr.decide(None, None, _run(run_id=7))["action"] == "ingest"
    assert rr.decide({}, {"last_ingested_run_id": 7}, _run(run_id=7))["action"] == "cached"


def test_in_progress_still_never_ingests_even_with_a_stale_schema():
    d = rr.decide(_report(), {"last_ingested_run_id": 7},
                  _run(run_id=7, status="in_progress", conclusion=None))
    assert d["action"] == "in-progress"
