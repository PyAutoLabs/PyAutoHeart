"""tests/test_profiling_drift.py — pinned-drift scan + readiness leg."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def drift_mod(tmp_path, monkeypatch):
    """Redirect HEART_STATE_DIR to a tmp dir and reload the check module."""
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod

    importlib.reload(state_mod)
    import heart.checks.profiling_drift as pd

    importlib.reload(pd)
    pd.HEART_STATE_DIR = tmp_path
    return tmp_path, pd


def _write_result(
    root: Path,
    rel: str,
    *,
    pinned_expected=None,
    pinned_drift=None,
    instrument="hst",
    version="2026.7.6.649",
    include_fields=True,
) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"instrument": instrument, "autolens_version": version}
    if include_fields:
        payload["pinned_expected"] = pinned_expected
        payload["pinned_drift"] = pinned_drift if pinned_drift is not None else []
    p.write_text(json.dumps(payload))
    return p


def test_clean_results_no_findings(drift_mod, tmp_path):
    state_dir, pd = drift_mod
    root = tmp_path / "results"
    _write_result(root, "runtime/imaging/mge/local_cpu_fp64.json", pinned_expected=27379.4)
    _write_result(root, "runtime/imaging/pixelization/local_cpu_fp64.json")  # no pin

    summary = pd.run(root)
    assert summary["observed"] is True
    assert summary["files_scanned"] == 2
    assert summary["pinned_checked"] == 1
    assert summary["drift_count"] == 0
    assert summary["findings"] == []
    # Persisted for the snapshot.
    assert json.loads((state_dir / "profiling_drift.json").read_text()) == summary


def test_drifted_result_is_a_finding(drift_mod, tmp_path):
    _, pd = drift_mod
    root = tmp_path / "results"
    _write_result(
        root,
        "runtime/imaging/mge/local_cpu_fp64.json",
        pinned_expected=27379.4,
        pinned_drift=[
            {"label": "eager", "expected": 27379.4, "got": 7170.9, "rel_diff": 0.738, "rtol": 1e-4}
        ],
    )

    summary = pd.run(root)
    assert summary["drift_count"] == 1
    (finding,) = summary["findings"]
    assert finding["path"] == "runtime/imaging/mge/local_cpu_fp64.json"
    assert finding["instrument"] == "hst"
    assert finding["drift"][0]["label"] == "eager"


def test_json_without_drift_fields_ignored(drift_mod, tmp_path):
    """Old artifacts (comparison.json, probe JSONs) carry no pinned fields."""
    _, pd = drift_mod
    root = tmp_path / "results"
    _write_result(root, "runtime/imaging/mge/comparison.json", include_fields=False)
    (root / "runtime/imaging/mge/broken.json").write_text("{not json")

    summary = pd.run(root)
    assert summary["files_scanned"] == 0
    assert summary["drift_count"] == 0


def test_absent_repo_not_observed(drift_mod, tmp_path):
    _, pd = drift_mod
    summary = pd.run(tmp_path / "nonexistent")
    assert summary["observed"] is False
    assert summary["drift_count"] == 0


def test_readiness_yellow_on_drift():
    from heart import readiness

    snap = {
        "ts": "2026-07-08T12:00:00+00:00",
        "repos": {},
        "profiling_drift": {
            "observed": True,
            "files_scanned": 9,
            "pinned_checked": 3,
            "drift_count": 1,
            "findings": [
                {
                    "path": "runtime/imaging/mge/local_cpu_fp64.json",
                    "instrument": "hst",
                    "drift": [{"label": "eager", "expected": 1.0, "got": 2.0}],
                }
            ],
        },
    }
    v = readiness.compute(snap, libraries=[])
    assert v["verdict"] in ("yellow", "red")
    assert any("profiling drift" in r for r in v["yellow_reasons"])


def test_readiness_caps_drift_reasons_at_five():
    from heart import readiness

    findings = [
        {"path": f"runtime/imaging/mge/cfg{i}.json", "drift": [{"label": "eager"}]}
        for i in range(8)
    ]
    snap = {
        "ts": "2026-07-08T12:00:00+00:00",
        "repos": {},
        "profiling_drift": {"observed": True, "drift_count": 8, "findings": findings},
    }
    v = readiness.compute(snap, libraries=[])
    drift_reasons = [r for r in v["yellow_reasons"] if "profiling drift" in r]
    assert len(drift_reasons) == 6  # 5 findings + the "+3 more" line
    assert any("+3 more" in r for r in drift_reasons)


def test_readiness_clean_drift_adds_nothing():
    from heart import readiness

    snap = {
        "ts": "2026-07-08T12:00:00+00:00",
        "repos": {},
        "profiling_drift": {"observed": True, "drift_count": 0, "findings": []},
    }
    v = readiness.compute(snap, libraries=[])
    assert not any("profiling drift" in r for r in v["yellow_reasons"])
