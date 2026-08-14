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


def test_migration_never_re_folds_a_report_carrying_adverse_evidence():
    """A legacy RED that never reached the rehearsal must not be softened.

    Such a report has no `rehearse` stage and no discriminator, so the rehearsal
    guard alone would let the migration overwrite it with a green integrate-only
    artifact — converting a real failure into an evidence gap.
    """
    # ONE adverse signal only — a combined fixture would still pass with any
    # single check removed.
    legacy_red = {
        "ts": "2026-08-14T20:00:00+00:00",
        "release_ready": False,
        "stages": {"unit": {"status": "fail"}},
    }
    d = rr.decide(legacy_red, {"last_ingested_run_id": 7}, _run(run_id=7))
    assert d["action"] == "cached"


def test_migration_blocked_by_failing_totals_alone():
    d = rr.decide(
        {"ts": "2026-08-14T20:00:00+00:00", "release_ready": False,
         "stages": {"integrate": {"status": "pass"}},
         "totals": {"passed": 9, "failed": 1, "skipped": 0, "timeout": 0}},
        {"last_ingested_run_id": 7}, _run(run_id=7),
    )
    assert d["action"] == "cached"


def test_migration_blocked_by_a_failures_list_alone():
    d = rr.decide(
        {"ts": "2026-08-14T20:00:00+00:00", "release_ready": False,
         "stages": {"integrate": {"status": "pass"}},
         "failures": [{"project": "x", "script": "y.py"}]},
        {"last_ingested_run_id": 7}, _run(run_id=7),
    )
    assert d["action"] == "cached"


def test_migration_survives_a_malformed_stages_or_totals_field():
    """Defensive: a corrupt report must not crash the tick.

    Asserts the exact action — accepting either would pass with the guard gone.
    No rehearsal, nothing adverse readable, no discriminator → migrate.
    """
    d = rr.decide(
        {"ts": "2026-08-14T20:00:00+00:00", "stages": "not-a-dict", "totals": None},
        {"last_ingested_run_id": 7}, _run(run_id=7),
    )
    assert d["action"] == "ingest"


def test_decide_survives_a_report_that_is_not_an_object():
    """`validate.load()` returns whatever JSON was on disk, not always a dict."""
    for junk in ("a string", ["a", "list"], 42):
        assert rr.decide(junk, None, _run(run_id=7))["action"] in (
            "ingest", "cached", "local-fresher",
        )


def test_resolve_outcome_reads_incomplete_over_the_legacy_boolean():
    assert rr.resolve_outcome({"release_ready": False,
                               "validation_outcome": "incomplete"}) == "incomplete"


def test_migration_blocked_by_timeouts_alone():
    d = rr.decide(
        {"ts": "2026-08-14T20:00:00+00:00",
         "stages": {"integrate": {"status": "pass"}},
         "totals": {"passed": 9, "failed": 0, "skipped": 0, "timeout": 1}},
        {"last_ingested_run_id": 7}, _run(run_id=7),
    )
    assert d["action"] == "cached"


def test_migration_blocked_by_per_project_counts_alone():
    """`per_project` is merged independently of `totals`, so it can be the only
    adverse signal — and the guard must use the same definition of "adverse"
    that `validate._has_adverse_evidence` does."""
    d = rr.decide(
        {"ts": "2026-08-14T20:00:00+00:00",
         "stages": {"integrate": {"status": "pass"}},
         "totals": {"passed": 9, "failed": 0, "skipped": 0, "timeout": 0},
         "per_project": {"autolens_workspace":
                         {"passed": 6, "failed": 1, "skipped": 0, "timeout": 0}}},
        {"last_ingested_run_id": 7}, _run(run_id=7),
    )
    assert d["action"] == "cached"


def test_migration_not_triggered_by_a_present_but_malformed_discriminator():
    """Readiness grades a malformed discriminator RED on purpose.

    Treating it as "predates the schema" would let the migration overwrite the
    very report that RED rests on.
    """
    d = rr.decide(
        {"ts": "2026-08-14T20:00:00+00:00", "validation_outcome": "FAILED",
         "stages": {"integrate": {"status": "pass"}}},
        {"last_ingested_run_id": 7}, _run(run_id=7),
    )
    assert d["action"] == "cached"


def test_tick_forces_fail_when_the_run_conclusion_is_not_success(monkeypatch, tmp_path):
    """Pins the tick -> validate.run hop, not just `ingest(force_fail=True)`.

    Reverting either the tick's argument or `run`'s threading of it must fail
    here; asserting on `ingest` alone would not catch that.
    """
    import json as _json
    from heart import validate

    (tmp_path / "stage_report.json").write_text(_json.dumps({
        "stage": "integrate", "status": "pass", "profile": "release",
        "summary": {"passed": 50, "failed": 0, "skipped": 0, "timeout": 0},
        "failures": [],
    }))

    seen = {}
    real_run = validate.run

    def spy(sources, **kw):
        seen.update(kw)
        return real_run(sources, **kw)

    # `main()` imports these inside the function body, so patch the modules.
    monkeypatch.setattr(validate, "run", spy)
    monkeypatch.setattr(validate, "load", lambda: None)
    monkeypatch.setattr(rr, "latest_run", lambda: _run(run_id=99, conclusion="failure"))
    monkeypatch.setattr(rr, "download_stage_report",
                        lambda run_id, dest: tmp_path / "stage_report.json")
    monkeypatch.setattr(rr, "_read_json", lambda p: None)

    rr.main([])
    assert seen.get("force_fail") is True


def test_tick_does_not_force_fail_on_a_successful_run(monkeypatch, tmp_path):
    import json as _json
    from heart import validate

    (tmp_path / "stage_report.json").write_text(_json.dumps({
        "stage": "integrate", "status": "pass", "profile": "release",
        "summary": {"passed": 50, "failed": 0, "skipped": 0, "timeout": 0},
        "failures": [],
    }))
    seen = {}
    real_run = validate.run

    def spy(sources, **kw):
        seen.update(kw)
        return real_run(sources, **kw)

    monkeypatch.setattr(validate, "run", spy)
    monkeypatch.setattr(validate, "load", lambda: None)
    monkeypatch.setattr(rr, "latest_run", lambda: _run(run_id=98, conclusion="success"))
    monkeypatch.setattr(rr, "download_stage_report",
                        lambda run_id, dest: tmp_path / "stage_report.json")
    monkeypatch.setattr(rr, "_read_json", lambda p: None)

    rr.main([])
    assert seen.get("force_fail") is False


def test_migration_blocked_by_a_stage_status_synonym():
    """The guard must normalise statuses the way the ingest does.

    A stored `"failure"` folds to `fail` in the accumulator, so matching only
    the literal token here made such a report look benign and let a green
    artifact overwrite it.
    """
    for token in ("failure", "timed_out", "error"):
        d = rr.decide(
            {"ts": "2026-08-14T20:00:00+00:00", "release_ready": False,
             "stages": {"integrate": {"status": token}}},
            {"last_ingested_run_id": 7}, _run(run_id=7),
        )
        assert d["action"] == "cached", token
