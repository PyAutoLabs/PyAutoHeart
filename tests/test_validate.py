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
    "packages": ["autoconf", "autoarray", "autofit", "autogalaxy", "autolens"],
    "run_id": "645",
    "run_attempt": "1",
    "build_sha": "abc1234def5678",
}

SHAS = {
    "PyAutoConf": "1" * 40,
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
    # the build sha is recorded under PyAutoBuild, not a library head
    assert report["commit_shas"].get("PyAutoBuild") == "abc1234def5678"


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
    assert report["commit_shas"]["PyAutoConf"] == "1" * 40


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
