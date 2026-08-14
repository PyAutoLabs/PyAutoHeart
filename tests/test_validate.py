"""tests/test_validate.py — release-validation artifact ingest logic."""

from __future__ import annotations

import importlib
import json

import pytest

from heart import validate


def _write(path, payload):
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return path


REHEARSAL = {
    "mode": "rehearsal",
    "index": "testpypi",
    "version": "2026.6.30.1.dev64501",
    "packages": ["autonerves", "autoarray", "autofit", "autogalaxy", "autolens"],
    "run_id": "645",
    "run_attempt": "1",
    "build_sha": "abc1234def5678",
}

SHAS = {
    "PyAutoNerves": "1" * 40,
    "PyAutoFit": "2" * 40,
    "PyAutoArray": "3" * 40,
    "PyAutoGalaxy": "4" * 40,
    "PyAutoLens": "5" * 40,
}

INTEGRATE = {
    "stage": "integrate",
    "status": "pass",
    "profile": "release",
    "run_url": "https://github.com/x/actions/runs/999",
    "commit_shas": SHAS,
    "summary": {"passed": 120, "failed": 0, "skipped": 3, "timeout": 0},
    "per_project": {
        "autolens_workspace": {"passed": 60, "failed": 0, "skipped": 1, "timeout": 0},
        "autolens_workspace_test": {"passed": 60, "failed": 0, "skipped": 2, "timeout": 0},
    },
    "failures": [],
}


# --- ingest: rehearsal only -------------------------------------------------


def test_ingest_rehearsal_only(tmp_path):
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    report = validate.ingest([tmp_path])
    assert report["schema_version"] == validate.SCHEMA_VERSION
    assert report["testpypi_version"] == "2026.6.30.1.dev64501"
    assert report["stages"]["rehearse"]["status"] == "pass"
    assert report["stages"]["rehearse"]["run_id"] == "645"
    # rehearsal artifact presence = build succeeded → release_ready True (pass axis)
    assert report["release_ready"] is True
    # no integration stage yet → no release profile (the gate keeps this YELLOW)
    assert report["profile"] is None
    # the build sha is recorded under PyAutoHands, not a library head
    assert report["commit_shas"].get("PyAutoHands") == "abc1234def5678"


def test_ingest_version_txt_fallback(tmp_path):
    _write(tmp_path / "testpypi_version.txt", "2026.7.1.1.dev70101\n")
    report = validate.ingest([tmp_path])
    assert report["testpypi_version"] == "2026.7.1.1.dev70101"


# --- ingest: full pipeline --------------------------------------------------


def test_ingest_full_pass(tmp_path):
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    _write(tmp_path / "commit_shas.json", SHAS)
    _write(tmp_path / "integrate.json", INTEGRATE)
    report = validate.ingest([tmp_path])
    assert report["release_ready"] is True
    assert report["profile"] == "release"
    assert report["commit_shas"]["PyAutoLens"] == "5" * 40
    assert report["totals"] == {"passed": 120, "failed": 0, "skipped": 3, "timeout": 0}
    assert report["per_project"]["autolens_workspace"]["passed"] == 60
    assert report["run_urls"]["integrate"].endswith("/999")
    assert report["stages"]["integrate"]["status"] == "pass"


# --- ingest: add_report() must not double-count a re-ingested stage --------


def test_ingest_report_plus_same_stage_does_not_double_count(tmp_path):
    """A prior validation_report.json alongside the raw stage that produced it
    (e.g. an artifacts dir that happens to include both) must not double the
    totals — Copilot review finding on PyAutoHeart#24: add_report() folded in
    totals/per_project/failures unconditionally, contradicting its own
    "idempotent re-ingest" docstring claim."""
    prior_report = {
        "schema_version": validate.SCHEMA_VERSION,
        "release_ready": True,
        "testpypi_version": "2026.6.30.1.dev64501",
        "profile": "release",
        "commit_shas": dict(SHAS),
        "stages": {"integrate": {"status": "pass", "profile": "release"}},
        "totals": {"passed": 120, "failed": 0, "skipped": 3, "timeout": 0},
        "per_project": {
            "autolens_workspace": {"passed": 60, "failed": 0, "skipped": 1, "timeout": 0},
        },
        "failures": [],
        "run_urls": {"integrate": "https://github.com/x/actions/runs/999"},
        "ts": "2026-06-30T12:00:00+00:00",
    }
    _write(tmp_path / "prior_report.json", prior_report)
    _write(tmp_path / "integrate.json", INTEGRATE)  # the SAME stage, re-ingested alongside it

    report = validate.ingest([tmp_path])
    # Must equal INTEGRATE's own totals, NOT double them.
    assert report["totals"] == {"passed": 120, "failed": 0, "skipped": 3, "timeout": 0}
    assert report["per_project"]["autolens_workspace"]["passed"] == 60
    assert report["failures"] == []


def test_ingest_report_alone_still_seeds_counts(tmp_path):
    """Re-ingesting ONLY a previously-emitted full report (no fresh stage
    artifacts) must still seed totals/per_project/failures from it — the
    ordinary "idempotent re-ingest" path the docstring describes."""
    prior_report = {
        "schema_version": validate.SCHEMA_VERSION,
        "release_ready": True,
        "testpypi_version": "2026.6.30.1.dev64501",
        "profile": "release",
        "commit_shas": dict(SHAS),
        "stages": {"integrate": {"status": "pass", "profile": "release"}},
        "totals": {"passed": 120, "failed": 0, "skipped": 3, "timeout": 0},
        "per_project": {
            "autolens_workspace": {"passed": 60, "failed": 0, "skipped": 1, "timeout": 0},
        },
        "failures": [{"project": "x", "script": "y.py"}],
        "run_urls": {"integrate": "https://github.com/x/actions/runs/999"},
        "ts": "2026-06-30T12:00:00+00:00",
    }
    _write(tmp_path / "prior_report.json", prior_report)

    report = validate.ingest([tmp_path])
    assert report["totals"] == {"passed": 120, "failed": 0, "skipped": 3, "timeout": 0}
    assert report["per_project"]["autolens_workspace"]["passed"] == 60
    assert report["failures"] == [{"project": "x", "script": "y.py"}]


def test_ingest_commit_shas_wrapper_form(tmp_path):
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    _write(tmp_path / "commit_shas.json", {"commit_shas": SHAS})
    report = validate.ingest([tmp_path])
    assert report["commit_shas"]["PyAutoFit"] == "2" * 40


# --- ingest: failure axis ---------------------------------------------------


def test_ingest_failed_stage_is_not_ready(tmp_path):
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    bad = dict(INTEGRATE, status="failure",
               summary={"passed": 100, "failed": 5, "skipped": 0, "timeout": 1},
               failures=[{"project": "autolens_workspace", "script": "x.py",
                          "log_url": "http://logs/1"}])
    _write(tmp_path / "integrate.json", bad)
    report = validate.ingest([tmp_path])
    assert report["release_ready"] is False
    assert report["stages"]["integrate"]["status"] == "fail"
    assert report["totals"]["failed"] == 5
    assert report["failures"][0]["script"] == "x.py"


def test_ingest_nothing_is_not_ready(tmp_path):
    # empty dir → no rehearse stage → not release_ready (nothing was built)
    report = validate.ingest([tmp_path])
    assert report["release_ready"] is False
    assert report["stages"] == {}


# --- ingest: explicit overrides ---------------------------------------------


def test_ingest_explicit_overrides(tmp_path):
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    report = validate.ingest(
        [tmp_path],
        profile="release",
        testpypi_version="9.9.9",
        commit_shas=SHAS,
    )
    assert report["profile"] == "release"
    assert report["testpypi_version"] == "9.9.9"
    assert report["commit_shas"]["PyAutoNerves"] == "1" * 40


def test_ingest_explicit_file_path_not_dir(tmp_path):
    p = _write(tmp_path / "rehearsal.json", REHEARSAL)
    report = validate.ingest([str(p)])
    assert report["stages"]["rehearse"]["status"] == "pass"


# --- helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "token,expected",
    [
        ("pass", "pass"), ("success", "pass"), ("passed", "pass"),
        ("failure", "fail"), ("failed", "fail"), ("timed_out", "fail"),
        ("skipped", "skip"), ("", "skip"), ("weird", "skip"),
    ],
)
def test_norm_status(token, expected):
    assert validate._norm_status(token) == expected


def test_classify(tmp_path):
    assert validate._classify("rehearsal.json", REHEARSAL) == "rehearsal"
    assert validate._classify("x.json", INTEGRATE) == "stage"
    assert validate._classify("commit_shas.json", SHAS) == "commit_shas"
    assert validate._classify("x.json", {"commit_shas": SHAS}) == "commit_shas"
    assert validate._classify("x.json", [1, 2]) == "unknown"


# --- run(): persistence -----------------------------------------------------


def test_run_persists_report_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.validate as v_mod
    importlib.reload(v_mod)

    src = tmp_path / "artifacts"
    src.mkdir()
    _write(src / "rehearsal.json", REHEARSAL)
    _write(src / "commit_shas.json", SHAS)
    _write(src / "integrate.json", INTEGRATE)

    report = v_mod.run([src])
    out = tmp_path / "validation_report.json"
    assert out.is_file()
    assert json.loads(out.read_text())["release_ready"] is True
    # no leftover temp files (atomic writes)
    assert [p for p in tmp_path.iterdir() if ".tmp" in p.name] == []
    # a history archive copy exists
    hist = list((tmp_path / "validation_history").glob("*.json"))
    assert len(hist) == 1
    assert json.loads(hist[0].read_text())["testpypi_version"] == report["testpypi_version"]

    # load() round-trips
    assert v_mod.load()["profile"] == "release"

    importlib.reload(state_mod)
    importlib.reload(v_mod)


# --- to_stage_report(): Build report.json -> Heart stage report ------------

AGGREGATE_PASS = {
    "ready": True,
    "summary": {"passed": 58, "failed": 0, "skipped": 2, "timeout": 0},
    "per_project": {
        "autolens_workspace": {"passed": 30, "failed": 0, "skipped": 1, "timeout": 0},
        "autolens_workspace_test": {"passed": 28, "failed": 0, "skipped": 1, "timeout": 0},
    },
    "failures": [],
}

AGGREGATE_FAIL = {
    "ready": False,
    "summary": {"passed": 55, "failed": 3, "skipped": 2, "timeout": 0},
    "per_project": {
        "autolens_workspace": {"passed": 30, "failed": 3, "skipped": 1, "timeout": 0},
    },
    "failures": [
        {"project": "autolens_workspace", "file": "scripts/imaging/start_here.py",
         "status": "failed", "error_message": "boom"},
    ],
}


def test_to_stage_report_malformed_summary_and_per_project_does_not_raise():
    # Copilot review finding on PyAutoHeart#25: summary/per_project were
    # accessed via `.get()`/`.items()` without an isinstance guard, so a
    # malformed (non-dict) shape from Build's aggregate_results.py would raise
    # instead of producing a safe default stage report.
    malformed = {
        "ready": True,
        "summary": ["not", "a", "dict"],
        "per_project": "also not a dict",
        "failures": "not a list either",
    }
    report = validate.to_stage_report(malformed, stage="integrate")
    assert report["summary"] == {"passed": 0, "failed": 0, "skipped": 0, "timeout": 0}
    assert report["per_project"] == {}
    assert report["failures"] == []
    assert report["status"] == "pass"


def test_to_stage_report_ready_must_be_strict_bool():
    # Copilot review finding on PyAutoHeart#25: `aggregate.get("ready")` was
    # used as a truthy check, so a stray non-bool value (e.g. the string
    # "false", which is truthy in Python) would incorrectly read as "pass".
    stringy = dict(AGGREGATE_PASS, ready="false")
    report = validate.to_stage_report(stringy, stage="integrate")
    assert report["status"] == "fail"


def test_to_stage_report_pass_shape():
    report = validate.to_stage_report(
        AGGREGATE_PASS, stage="integrate", profile="release",
        version="2026.6.30.1.dev64501", commit_shas=SHAS,
        run_url="https://github.com/x/actions/runs/999",
    )
    assert report["stage"] == "integrate"
    assert report["status"] == "pass"
    assert report["profile"] == "release"
    assert report["version"] == "2026.6.30.1.dev64501"
    assert report["run_url"] == "https://github.com/x/actions/runs/999"
    assert report["commit_shas"] == SHAS
    assert report["summary"] == {"passed": 58, "failed": 0, "skipped": 2, "timeout": 0}
    assert report["per_project"]["autolens_workspace"]["passed"] == 30
    assert report["failures"] == []


def test_to_stage_report_maps_file_to_script_and_project():
    report = validate.to_stage_report(AGGREGATE_FAIL, stage="integrate")
    assert report["status"] == "fail"
    assert report["failures"] == [
        {"project": "autolens_workspace", "script": "scripts/imaging/start_here.py"},
    ]


def test_to_stage_report_force_fail_from_verify_install():
    report = validate.to_stage_report(
        AGGREGATE_PASS, stage="integrate",
        extra_failures=[{"project": None, "script": "verify_install", "reason": "verify_install FAILED"}],
        force_fail=True,
    )
    assert report["status"] == "fail"
    assert any(f.get("script") == "verify_install" for f in report["failures"])


def test_to_stage_report_is_ingestable(tmp_path):
    """Round-trip: emit a stage report, then ingest it like the Release Agent would."""
    stage_report = validate.to_stage_report(
        AGGREGATE_PASS, stage="integrate", profile="release",
        version="2026.6.30.1.dev64501", commit_shas=SHAS,
        run_url="https://github.com/x/actions/runs/999",
    )
    _write(tmp_path / "integrate.json", stage_report)
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    report = validate.ingest([tmp_path])
    assert report["release_ready"] is True
    assert report["profile"] == "release"
    assert report["commit_shas"]["PyAutoLens"] == SHAS["PyAutoLens"]
    assert report["totals"] == {"passed": 58, "failed": 0, "skipped": 2, "timeout": 0}


# --- CLI: --emit-stage-report ------------------------------------------------


def test_cli_emit_stage_report_pass(tmp_path, capsys):
    agg_path = _write(tmp_path / "report.json", AGGREGATE_PASS)
    shas_path = _write(tmp_path / "commit_shas.json", SHAS)
    out_path = tmp_path / "stage_report.json"

    rc = validate.main([
        "--emit-stage-report", str(agg_path),
        "--stage", "integrate",
        "--profile", "release",
        "--testpypi-version", "2026.6.30.1.dev64501",
        "--commit-shas", str(shas_path),
        "--run-url", "https://github.com/x/actions/runs/999",
        "--out", str(out_path),
    ])
    assert rc == 0
    written = json.loads(out_path.read_text())
    assert written["stage"] == "integrate"
    assert written["status"] == "pass"
    assert written["profile"] == "release"
    assert written["commit_shas"] == SHAS


def test_cli_emit_stage_report_fail_exit_code(tmp_path):
    agg_path = _write(tmp_path / "report.json", AGGREGATE_FAIL)
    out_path = tmp_path / "stage_report.json"
    rc = validate.main(["--emit-stage-report", str(agg_path), "--out", str(out_path)])
    assert rc == 1
    assert json.loads(out_path.read_text())["status"] == "fail"


def test_cli_emit_stage_report_verify_install_forces_fail(tmp_path):
    agg_path = _write(tmp_path / "report.json", AGGREGATE_PASS)
    vi_path = _write(tmp_path / "verify_install.json", {"ready": False, "checks": []})
    out_path = tmp_path / "stage_report.json"
    rc = validate.main([
        "--emit-stage-report", str(agg_path),
        "--verify-install", str(vi_path),
        "--out", str(out_path),
    ])
    assert rc == 1
    written = json.loads(out_path.read_text())
    assert written["status"] == "fail"
    assert any(f.get("script") == "verify_install" for f in written["failures"])


def test_run_out_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.validate as v_mod
    importlib.reload(v_mod)

    _write(tmp_path / "rehearsal.json", REHEARSAL)
    custom = tmp_path / "custom.json"
    v_mod.run([tmp_path / "rehearsal.json"], out=custom)
    assert custom.is_file()

    importlib.reload(state_mod)
    importlib.reload(v_mod)


# --- verify_install: carried as evidence, not only as a veto ----------------
#
# Stage 3 has always run verify_install A-F against the wheels, but the sidecar
# was consulted only in the failure direction, so a PASS contributed nothing and
# the readiness leg reported "install verification not run" forever. These cover
# the carry + persist path that closes that gap.

VERIFY_INSTALL_PASS = {
    "ts": "2026-07-15T10:00:00+00:00",
    "ready": True,
    "version": "2026.7.15.1.dev66201",
    "check_b_version": "2026.7.15.1.dev66201",
    "index": "testpypi",
    "checks": [
        {"check": "A", "status": "PASS", "detail": "pip install + start_here.py"},
        {"check": "C", "status": "SKIP", "detail": "conda not on PATH"},
    ],
}


def test_normalize_verify_install_rejects_unusable_sidecars():
    # An absent or malformed result must never read as a pass — it has to leave
    # the leg saying "not run" rather than inventing evidence.
    assert validate.normalize_verify_install(None) is None
    assert validate.normalize_verify_install({}) is None
    assert validate.normalize_verify_install("nope") is None
    assert validate.normalize_verify_install({"checks": []}) is None


def test_normalize_verify_install_keeps_index_and_coerces_ready():
    vi = validate.normalize_verify_install(VERIFY_INSTALL_PASS)
    assert vi["ready"] is True
    assert vi["index"] == "testpypi"
    assert vi["check_b_version"] == "2026.7.15.1.dev66201"
    assert vi["version"] == "2026.7.15.1.dev66201"
    assert [c["check"] for c in vi["checks"]] == ["A", "C"]
    # Strict: only a real True is a pass (a stray truthy string is not).
    assert validate.normalize_verify_install({"ready": "false"})["ready"] is False
    # A pre-index sidecar stays unknown rather than being guessed at.
    assert validate.normalize_verify_install({"ready": True})["index"] is None


def test_to_stage_report_carries_passing_verify_install():
    report = validate.to_stage_report(
        AGGREGATE_PASS, stage="integrate", verify_install=VERIFY_INSTALL_PASS
    )
    # A pass is carried as evidence and does NOT flip the stage's pass/fail axis.
    assert report["status"] == "pass"
    assert report["verify_install"]["ready"] is True
    assert report["verify_install"]["index"] == "testpypi"


def test_to_stage_report_omits_verify_install_when_absent():
    report = validate.to_stage_report(AGGREGATE_PASS, stage="integrate")
    assert "verify_install" not in report


def test_cli_emit_stage_report_carries_verify_install(tmp_path):
    agg_path = _write(tmp_path / "report.json", AGGREGATE_PASS)
    vi_path = _write(tmp_path / "verify_install.json", VERIFY_INSTALL_PASS)
    out_path = tmp_path / "stage_report.json"
    rc = validate.main([
        "--emit-stage-report", str(agg_path),
        "--verify-install", str(vi_path),
        "--out", str(out_path),
    ])
    assert rc == 0
    written = json.loads(out_path.read_text())
    assert written["status"] == "pass"
    assert written["verify_install"]["index"] == "testpypi"


def test_run_persists_verify_install_sidecar_from_stage_artifact(tmp_path, monkeypatch):
    """The end-to-end gap this task closes: Stage 3 pass -> readiness sidecar."""
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.validate as v_mod
    importlib.reload(v_mod)

    src = tmp_path / "artifacts"
    src.mkdir()
    stage = dict(INTEGRATE)
    stage["verify_install"] = VERIFY_INSTALL_PASS
    _write(src / "integrate.json", stage)

    report = v_mod.run([src])

    sidecar = tmp_path / "verify_install.json"
    assert sidecar.is_file(), "ingest must write the sidecar readiness reads"
    written = json.loads(sidecar.read_text())
    assert written["ready"] is True
    assert written["index"] == "testpypi"
    # It stays out of validation_report.json — not part of that schema.
    assert "verify_install" not in report

    importlib.reload(state_mod)
    importlib.reload(v_mod)


def test_run_without_verify_install_leaves_sidecar_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.validate as v_mod
    importlib.reload(v_mod)

    src = tmp_path / "artifacts"
    src.mkdir()
    _write(src / "integrate.json", INTEGRATE)  # no verify_install block
    v_mod.run([src])
    assert not (tmp_path / "verify_install.json").exists()

    importlib.reload(state_mod)
    importlib.reload(v_mod)


def test_run_keeps_newest_verify_install_across_artifacts(tmp_path, monkeypatch):
    """Re-ingesting an older artifact must not walk the leg backwards."""
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.validate as v_mod
    importlib.reload(v_mod)

    src = tmp_path / "artifacts"
    src.mkdir()
    old = dict(INTEGRATE)
    old["verify_install"] = dict(VERIFY_INSTALL_PASS, ts="2026-07-01T00:00:00+00:00",
                                 ready=False)
    new = dict(INTEGRATE)
    new["stage"] = "integrate"
    new["verify_install"] = VERIFY_INSTALL_PASS  # ts 2026-07-15
    _write(src / "a_old.json", old)
    _write(src / "b_new.json", new)

    v_mod.run([src])
    written = json.loads((tmp_path / "verify_install.json").read_text())
    assert written["ts"] == "2026-07-15T10:00:00+00:00"
    assert written["ready"] is True

    importlib.reload(state_mod)
    importlib.reload(v_mod)


# --- validation_outcome: the fail/incomplete split --------------------------
#
# `release_ready` collapses "something failed" and "nothing was built" into one
# `false`. These pin the discriminator, and in particular that everything
# ambiguous or adverse lands on `fail` rather than being softened.


def test_outcome_integrate_only_green_is_incomplete(tmp_path):
    """The tick's auto-ingest shape: green, integrate-only, no rehearsal.

    This is the case that used to be graded a release FAILURE.
    """
    _write(tmp_path / "stage_report.json", dict(INTEGRATE))
    report = validate.ingest([tmp_path])
    assert report["validation_outcome"] == "incomplete"
    assert report["release_ready"] is False  # legacy boolean unchanged
    assert report["stages"]["integrate"]["status"] == "pass"
    assert report["failures"] == []


def test_outcome_rehearsal_plus_green_integrate_is_pass(tmp_path):
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    _write(tmp_path / "integrate.json", dict(INTEGRATE))
    report = validate.ingest([tmp_path])
    assert report["validation_outcome"] == "pass"
    assert report["release_ready"] is True


def test_outcome_failed_stage_is_fail(tmp_path):
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    _write(tmp_path / "integrate.json", dict(INTEGRATE, status="fail"))
    report = validate.ingest([tmp_path])
    assert report["validation_outcome"] == "fail"


def test_outcome_failed_counts_without_failed_stage_is_fail(tmp_path):
    """An artifact claiming `pass` while carrying failing counts must not soften.

    `release_ready()` never consulted `totals`, so the stage test alone would
    read this as "nothing failed" and, with no rehearsal, call it incomplete.
    """
    _write(tmp_path / "stage_report.json", dict(
        INTEGRATE, status="pass",
        summary={"passed": 100, "failed": 5, "skipped": 0, "timeout": 0},
    ))
    report = validate.ingest([tmp_path])
    assert report["totals"]["failed"] == 5
    assert report["stages"]["integrate"]["status"] == "pass"
    assert report["validation_outcome"] == "fail"


def test_outcome_timeout_counts_without_failed_stage_is_fail(tmp_path):
    _write(tmp_path / "stage_report.json", dict(
        INTEGRATE, status="pass",
        summary={"passed": 100, "failed": 0, "skipped": 0, "timeout": 2},
    ))
    report = validate.ingest([tmp_path])
    assert report["totals"]["timeout"] == 2
    assert report["validation_outcome"] == "fail"


def test_outcome_failures_list_without_failed_stage_is_fail(tmp_path):
    _write(tmp_path / "stage_report.json", dict(
        INTEGRATE, status="pass",
        failures=[{"project": "autolens_workspace", "script": "x.py"}],
    ))
    report = validate.ingest([tmp_path])
    assert report["failures"]
    assert report["validation_outcome"] == "fail"


def test_add_report_normalises_stage_status_synonyms(tmp_path):
    """A merged base report's `"failure"` must normalise to `"fail"`.

    `_norm_status` ran only in `add_stage`, so a synonym arriving through
    `add_report` kept a status that no "did a stage fail?" test would match.
    """
    _write(tmp_path / "validation_report.json", {
        "schema_version": 1,
        "release_ready": False,
        "stages": {"integrate": {"status": "failure"}},
        "totals": {"passed": 0, "failed": 0, "skipped": 0, "timeout": 0},
        "failures": [],
        "ts": "2026-06-01T00:00:00+00:00",
    })
    report = validate.ingest([tmp_path])
    assert report["stages"]["integrate"]["status"] == "fail"
    assert report["validation_outcome"] == "fail"


def test_outcome_legacy_report_without_discriminator_fails_closed(tmp_path):
    """`release_ready: false` and no `validation_outcome`, nothing else adverse.

    We cannot tell whether it failed or was merely incomplete, so it stays a
    failure — the gate must never soften evidence it cannot classify.
    """
    _write(tmp_path / "validation_report.json", {
        "schema_version": 1,
        "release_ready": False,
        "stages": {"integrate": {"status": "pass"}},
        "totals": {"passed": 10, "failed": 0, "skipped": 0, "timeout": 0},
        "failures": [],
        "ts": "2026-06-01T00:00:00+00:00",
    })
    report = validate.ingest([tmp_path])
    assert report["validation_outcome"] == "fail"


def test_outcome_nothing_ingested_is_incomplete(tmp_path):
    report = validate.ingest([tmp_path])
    assert report["validation_outcome"] == "incomplete"
    assert report["release_ready"] is False


# --- validation_outcome: the fail-closed edges -----------------------------
#
# Every case below reached `pass` or `incomplete` at some point during review.
# They are the reason the predicate is wider than "did a stage say fail".


def test_outcome_per_project_failures_with_clean_totals_is_fail(tmp_path):
    """per_project is merged independently of totals, so it can disagree."""
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    _write(tmp_path / "integrate.json", dict(
        INTEGRATE,
        summary={"passed": 10, "failed": 0, "skipped": 0, "timeout": 0},
        per_project={"autolens_workspace":
                     {"passed": 7, "failed": 3, "skipped": 0, "timeout": 0}},
        failures=[],
    ))
    report = validate.ingest([tmp_path])
    assert report["totals"]["failed"] == 0
    assert report["validation_outcome"] == "fail"


def test_outcome_per_project_timeouts_with_clean_totals_is_fail(tmp_path):
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    _write(tmp_path / "integrate.json", dict(
        INTEGRATE,
        summary={"passed": 10, "failed": 0, "skipped": 0, "timeout": 0},
        per_project={"autolens_workspace":
                     {"passed": 7, "failed": 0, "skipped": 0, "timeout": 2}},
        failures=[],
    ))
    assert validate.ingest([tmp_path])["validation_outcome"] == "fail"


def test_outcome_skipped_stage_is_incomplete_not_pass(tmp_path):
    """A stage that ran without passing is not evidence of passing."""
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    _write(tmp_path / "integrate.json", dict(INTEGRATE, status="skipped"))
    report = validate.ingest([tmp_path])
    assert report["stages"]["integrate"]["status"] == "skip"
    assert report["validation_outcome"] == "incomplete"


def test_outcome_unknown_status_token_is_incomplete_not_pass(tmp_path):
    """`_norm_status` folds unrecognised tokens to `skip`, never `fail`.

    So an adverse-sounding token Heart does not know must still not read as a
    pass just because the rehearsal succeeded.
    """
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    _write(tmp_path / "integrate.json", dict(INTEGRATE, status="completed_with_failures"))
    report = validate.ingest([tmp_path])
    assert report["stages"]["integrate"]["status"] == "skip"
    assert report["validation_outcome"] == "incomplete"


def test_outcome_contradicting_explicit_fields_fails_closed(tmp_path):
    """`validation_outcome: pass` beside `release_ready: false` is not trustworthy."""
    _write(tmp_path / "validation_report.json", {
        "schema_version": 1,
        "release_ready": False,
        "validation_outcome": "pass",
        "stages": {"rehearse": {"status": "pass"}, "integrate": {"status": "pass"}},
        "totals": {"passed": 5, "failed": 0, "skipped": 0, "timeout": 0},
        "failures": [],
        "ts": "2026-06-01T00:00:00+00:00",
    })
    assert validate.ingest([tmp_path])["validation_outcome"] == "fail"


def test_outcome_malformed_discriminator_fails_closed(tmp_path):
    """Present-but-unrecognised is a malformed artifact, not a legacy report."""
    _write(tmp_path / "validation_report.json", {
        "schema_version": 1,
        "release_ready": True,
        "validation_outcome": "PASS",          # wrong case → not a value we accept
        "stages": {"rehearse": {"status": "pass"}, "integrate": {"status": "pass"}},
        "totals": {"passed": 5, "failed": 0, "skipped": 0, "timeout": 0},
        "failures": [],
        "ts": "2026-06-01T00:00:00+00:00",
    })
    assert validate.ingest([tmp_path])["validation_outcome"] == "fail"


def test_outcome_absent_discriminator_with_legacy_true_still_passes(tmp_path):
    """The compatibility case: no field at all + a genuine legacy `true`."""
    _write(tmp_path / "validation_report.json", {
        "schema_version": 1,
        "release_ready": True,
        "stages": {"rehearse": {"status": "pass"}, "integrate": {"status": "pass"}},
        "totals": {"passed": 5, "failed": 0, "skipped": 0, "timeout": 0},
        "failures": [],
        "ts": "2026-06-01T00:00:00+00:00",
    })
    assert validate.ingest([tmp_path])["validation_outcome"] == "pass"


def test_force_fail_overrides_a_passing_artifact(tmp_path):
    """The producing run's own conclusion outranks what its artifact claims.

    A workflow can break outside anything the stage report captures, and that
    report is written by a step that may have run before the break.
    """
    _write(tmp_path / "stage_report.json", dict(INTEGRATE))
    assert validate.ingest([tmp_path])["validation_outcome"] == "incomplete"
    assert validate.ingest([tmp_path], force_fail=True)["validation_outcome"] == "fail"


def test_force_fail_beats_even_a_complete_passing_report(tmp_path):
    _write(tmp_path / "rehearsal.json", REHEARSAL)
    _write(tmp_path / "integrate.json", dict(INTEGRATE))
    assert validate.ingest([tmp_path])["validation_outcome"] == "pass"
    assert validate.ingest([tmp_path], force_fail=True)["validation_outcome"] == "fail"
