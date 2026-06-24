"""tests/test_test_run.py — parse PyAutoBuild test-run report into Heart JSON."""

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
    monkeypatch.setattr(tr, "_cloud_verdict", lambda: {
        "ready": False, "ts": "2026-06-20T00:00:00Z", "run_id": 7, "url": "U"})
    out = tr.run(results_dir=tmp_path, fetch_cloud=True)
    assert out["ready"] is False                  # cloud is authoritative
    assert out["ts"] == "2026-06-20T00:00:00Z"
    assert out["source"] == "cloud"
    assert out["passed"] == 5                      # detail retained from local report
