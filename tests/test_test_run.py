"""tests/test_test_run.py — parse PyAutoHands test-run report into Heart JSON."""

from __future__ import annotations

import json

from heart.checks import test_run as tr


def test_from_report_extracts_ready_counts_and_stale_parked():
    report = {
        "ready": False,
        "run_label": "2026-05-29T09-15-47Z",
        "summary": {"passed": 592, "failed": 20, "skipped": 71},
        "per_project": {"autolens": {"passed": 220, "failed": 3}},
        "slow_skips": [
            {"workspace": "autolens_workspace", "pattern": "group/slam", "category": "slow",
             "is_stale": True, "age_days": 49},
            {"workspace": "autolens_workspace", "pattern": "x", "category": "slow",
             "is_stale": False, "age_days": 3},
        ],
        "needs_fix_skips": [
            {"workspace": "autofit_workspace", "pattern": "y", "category": "needs_fix",
             "is_stale": True, "age_days": 60},
        ],
    }
    out = tr._from_report(report)
    assert out["ready"] is False
    assert out["passed"] == 592 and out["failed"] == 20 and out["skipped"] == 71
    assert out["run_label"] == "2026-05-29T09-15-47Z"
    assert out["source"] == "report"
    # only the two stale entries counted
    assert out["parked_stale_count"] == 2
    assert {p["pattern"] for p in out["parked_stale"]} == {"group/slam", "y"}


def test_from_report_missing_summary_is_zeroed():
    out = tr._from_report({"ready": True})
    assert out["ready"] is True
    assert out["passed"] == 0 and out["failed"] == 0
    assert out["parked_stale_count"] == 0


def test_run_reads_report_json(tmp_path):
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": True, "run_label": "R1", "summary": {"passed": 5, "failed": 0, "skipped": 1},
    }))
    out = tr.run(results_dir=tmp_path)
    assert out["ready"] is True
    assert out["source"] == "report"
    assert out["passed"] == 5


def test_run_falls_back_to_per_job_when_no_report(tmp_path):
    (tmp_path / "autolens__scripts__imaging__script.json").write_text(json.dumps({
        "project": "autolens", "summary": {"passed": 10, "failed": 2, "skipped": 0},
    }))
    (tmp_path / "autofit__scripts__model__script.json").write_text(json.dumps({
        "project": "autofit", "summary": {"passed": 4, "failed": 0, "skipped": 1},
    }))
    out = tr.run(results_dir=tmp_path)
    assert out["ready"] is None              # unknown from per-job data
    assert out["source"] == "per-job"
    assert out["passed"] == 14 and out["failed"] == 2 and out["skipped"] == 1
    assert out["per_project"]["autolens"]["failed"] == 2


def test_run_empty_dir_returns_empty(tmp_path):
    out = tr.run(results_dir=tmp_path)
    assert out == {}


def test_cloud_verdict_parses_completed_run(monkeypatch):
    import types
    monkeypatch.setattr(tr.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout=json.dumps([{"conclusion": "success", "status": "completed",
                            "createdAt": "2026-06-23T00:00:00Z", "databaseId": 42, "url": "u"}])))
    v = tr._cloud_verdict()
    assert v["ready"] is True and v["run_id"] == 42 and v["ts"] == "2026-06-23T00:00:00Z"


def test_cloud_verdict_in_progress_is_unknown(monkeypatch):
    import types
    monkeypatch.setattr(tr.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout=json.dumps([{"conclusion": None, "status": "in_progress",
                            "createdAt": "t", "databaseId": 1, "url": "u"}])))
    assert tr._cloud_verdict()["ready"] is None


def test_cloud_verdict_no_runs_is_none(monkeypatch):
    import types
    monkeypatch.setattr(tr.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="[]"))
    assert tr._cloud_verdict() is None


def test_run_cloud_overrides_ready_keeps_local_detail(monkeypatch, tmp_path):
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": True, "run_label": "local", "summary": {"passed": 5, "failed": 0}}))
    monkeypatch.setattr(tr, "_server_verdict", lambda: {
        "ready": False, "ts": "2026-06-20T00:00:00Z", "run_id": 7, "url": "U"})
    out = tr.run(results_dir=tmp_path, fetch_cloud=True)
    assert out["ready"] is False                  # cloud is authoritative
    assert out["ts"] == "2026-06-20T00:00:00Z"
    assert out["source"] == "cloud"
    assert out["passed"] == 5                      # detail retained from local report


# --- server-first signal (finding 3): report absent, server green --------------

def test_run_server_green_with_no_local_report_is_ready(monkeypatch, tmp_path):
    """The mobile case: no local report.json at all, but the server (MCP/gh)
    reports green → ready True, NOT unknown/None."""
    monkeypatch.setattr(tr, "_server_verdict", lambda: {
        "ready": True, "ts": "2026-06-25T00:00:00Z", "run_id": 9, "url": "U"})
    out = tr.run(results_dir=tmp_path, fetch_cloud=True)  # empty dir → no report
    assert out["ready"] is True
    assert out["source"] == "cloud"
    assert out["ts"] == "2026-06-25T00:00:00Z"
    assert out["cloud_url"] == "U"


def test_agent_supplied_verdict_works_without_gh(monkeypatch, tmp_path):
    """`gh` absent → _cloud_verdict None, but a Brain/MCP-written file is used."""
    vfile = tmp_path / "cloud_validation.json"
    vfile.write_text(json.dumps({"ready": True, "ts": "2026-06-26T00:00:00Z", "run_id": 11}))
    monkeypatch.setattr(tr, "VALIDATION_FILE", vfile)
    monkeypatch.setattr(tr, "_cloud_verdict", lambda: None)  # no gh
    v = tr._server_verdict()
    assert v is not None and v["ready"] is True and v["run_id"] == 11


def test_agent_supplied_verdict_normalises_raw_run(monkeypatch, tmp_path):
    """The file may hold a raw Actions run record (conclusion/status)."""
    vfile = tmp_path / "cloud_validation.json"
    vfile.write_text(json.dumps({"conclusion": "failure", "status": "completed",
                                 "createdAt": "t", "id": 5, "html_url": "h"}))
    monkeypatch.setattr(tr, "VALIDATION_FILE", vfile)
    v = tr._agent_supplied_verdict()
    assert v["ready"] is False and v["run_id"] == 5 and v["url"] == "h"


def test_server_verdict_prefers_agent_file_over_gh(monkeypatch, tmp_path):
    vfile = tmp_path / "cloud_validation.json"
    vfile.write_text(json.dumps({"ready": False, "ts": "t", "run_id": 1}))
    monkeypatch.setattr(tr, "VALIDATION_FILE", vfile)
    monkeypatch.setattr(tr, "_cloud_verdict", lambda: {"ready": True, "ts": "t2", "run_id": 2, "url": "u"})
    assert tr._server_verdict()["run_id"] == 1     # file wins


def test_agent_supplied_verdict_absent_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "VALIDATION_FILE", tmp_path / "nope.json")
    assert tr._agent_supplied_verdict() is None


# --- state-dir isolation (the 2026-07-15 clobber incident) ---------------------

def test_run_writes_nothing_to_state_dir(tmp_path):
    """run() must be side-effect-free: the write lives in main() only, so tests
    (and any library caller) can never clobber live Heart state."""
    import os
    from pathlib import Path
    state_dir = Path(os.environ["HEART_STATE_DIR"])
    before = set(state_dir.glob("**/*")) if state_dir.exists() else set()
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": True, "run_label": "R1", "summary": {"passed": 1}}))
    tr.run(results_dir=tmp_path)
    after = set(state_dir.glob("**/*")) if state_dir.exists() else set()
    assert after == before


def test_main_persists_summary_to_state_dir(monkeypatch, tmp_path):
    """The tick path (python -m heart.checks.test_run) must still persist."""
    import os
    from pathlib import Path
    monkeypatch.setattr(tr, "_server_verdict", lambda: None)  # hermetic
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": True, "run_label": "R1", "summary": {"passed": 1}}))
    assert tr.main(["test_run", str(tmp_path)]) == 0
    written = json.loads((Path(os.environ["HEART_STATE_DIR"]) / "test_run.json").read_text())
    assert written["ready"] is True and written["passed"] == 1


# --- entrypoint wiring + disagreement (PyAutoHeart#83 finding A) ----------------

def test_main_consults_the_server_verdict(monkeypatch, tmp_path):
    """The tick entrypoint must fetch the cloud verdict — the old inference
    (fetch_cloud from `results_dir is None`) silently disabled it forever."""
    import os
    from pathlib import Path
    monkeypatch.setattr(tr, "_server_verdict", lambda: {
        "ready": False, "ts": "2026-07-16T00:00:00Z", "run_id": 3, "url": "U"})
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": True, "run_label": "local", "summary": {"passed": 5}}))
    assert tr.main(["test_run", str(tmp_path)]) == 0
    written = json.loads((Path(os.environ["HEART_STATE_DIR"]) / "test_run.json").read_text())
    assert written["source"] == "cloud"
    assert written["cloud_ready"] is False


def test_local_and_cloud_must_agree_to_be_green(monkeypatch, tmp_path):
    # local False + cloud True → NOT green, disagreement surfaced (the mirror
    # of the local-green/cloud-red hole; neither side wins silently).
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": False, "run_label": "local", "summary": {"passed": 1, "failed": 2}}))
    monkeypatch.setattr(tr, "_server_verdict", lambda: {
        "ready": True, "ts": "t", "run_id": 4, "url": "u"})
    out = tr.run(results_dir=tmp_path, fetch_cloud=True)
    assert out["ready"] is False
    assert "disagreement" in out
    assert out["cloud_ready"] is True


def test_cloud_in_progress_keeps_local_verdict(monkeypatch, tmp_path):
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": True, "run_label": "local", "summary": {"passed": 1}}))
    monkeypatch.setattr(tr, "_server_verdict", lambda: {
        "ready": None, "ts": "t", "run_id": 5, "url": "u"})
    out = tr.run(results_dir=tmp_path, fetch_cloud=True)
    assert out["ready"] is True
    assert out["cloud_ready"] is None
    assert "disagreement" not in out


def test_run_without_explicit_fetch_never_touches_network(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tr, "_server_verdict", lambda: calls.append(1))
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": True, "summary": {"passed": 1}}))
    tr.run(results_dir=tmp_path)
    assert calls == []


def test_surface_is_carried_from_report_to_sidecar(tmp_path):
    """The leg must be able to state what the run measured (#83 §5.3)."""
    surface = {
        "projects": ["autolens", "howtolens"],
        "shards": ["autolens/imaging"],
        "run_types": ["script"],
        "env_profiles": ["env_vars.yaml"],
        "script_count": 42,
    }
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": True, "run_label": "R1", "summary": {"passed": 42},
        "surface": surface,
    }))
    out = tr.run(results_dir=tmp_path)
    assert out["surface"] == surface


def test_surface_absent_from_old_report_is_none_not_a_crash(tmp_path):
    # Reports predating the surface block must still parse.
    (tmp_path / "report.json").write_text(json.dumps({
        "ready": True, "run_label": "old", "summary": {"passed": 1}}))
    out = tr.run(results_dir=tmp_path)
    assert out["surface"] is None
