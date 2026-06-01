"""tests/test_test_run.py — parse PyAutoBuild test-run report into Pulse JSON."""

from __future__ import annotations

import json

from pulse.checks import test_run as tr


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
