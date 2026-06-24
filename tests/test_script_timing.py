"""tests/test_script_timing.py — regression classifier thresholds + idempotence."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect HEART_STATE_DIR to a tmp dir and reload heart.checks.script_timing."""
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.checks.script_timing as st
    # The script_timing module also has module-level constants — reload it.
    importlib.reload(st)
    # Override TIMINGS dir to live under the tmp dir.
    st.HEART_STATE_DIR = tmp_path
    st.HEART_TIMINGS_DIR = tmp_path / "timings"
    st.HEART_TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    return tmp_path, st


def _make_results_dir(root: Path, project: str, directory: str, results: list[dict]) -> Path:
    rdir = root / "results"
    rdir.mkdir(parents=True, exist_ok=True)
    safe_dir = directory.replace("/", "__")
    fname = f"{project}__scripts__{safe_dir}__script.json"
    (rdir / fname).write_text(json.dumps({
        "project": project,
        "directory": f"scripts/{directory}",
        "results": results,
    }))
    return rdir


def test_first_observation_has_no_baseline(tmp_state):
    tmp_path, st = tmp_state
    rdir = _make_results_dir(tmp_path, "autolens", "imaging", [
        {"file": "imaging/simulator.py", "status": "passed", "duration_seconds": 50.0},
    ])
    summary = st.run(rdir)
    assert summary["new_scripts_no_baseline"] == 1
    assert summary["red_count"] == 0
    assert summary["yellow_count"] == 0


def test_within_baseline_classified_green(tmp_state):
    tmp_path, st = tmp_state
    rdir = _make_results_dir(tmp_path, "autolens", "imaging", [
        {"file": "imaging/simulator.py", "status": "passed", "duration_seconds": 50.0},
    ])
    # Run twice to populate history; second call has 1-sample baseline = 50,
    # ratio = 50/50 = 1.0 → green.
    st.run(rdir)
    summary = st.run(rdir)
    assert summary["red_count"] == 0
    assert summary["yellow_count"] == 0
    assert summary["green_count"] == 1


def test_above_yellow_factor_classified_yellow(tmp_state):
    tmp_path, st = tmp_state
    rdir = _make_results_dir(tmp_path, "autolens", "imaging", [
        {"file": "imaging/simulator.py", "status": "passed", "duration_seconds": 50.0},
    ])
    st.run(rdir)
    rdir = _make_results_dir(tmp_path, "autolens", "imaging", [
        {"file": "imaging/simulator.py", "status": "passed", "duration_seconds": 100.0},
    ])
    summary = st.run(rdir)
    assert summary["yellow_count"] == 1
    assert summary["red_count"] == 0


def test_above_red_factor_classified_red(tmp_state):
    tmp_path, st = tmp_state
    rdir = _make_results_dir(tmp_path, "autolens", "imaging", [
        {"file": "imaging/simulator.py", "status": "passed", "duration_seconds": 50.0},
    ])
    st.run(rdir)
    rdir = _make_results_dir(tmp_path, "autolens", "imaging", [
        {"file": "imaging/simulator.py", "status": "passed", "duration_seconds": 200.0},
    ])
    summary = st.run(rdir)
    assert summary["red_count"] == 1
    assert summary["yellow_count"] == 0


def test_failed_scripts_excluded_from_baseline(tmp_state):
    tmp_path, st = tmp_state
    rdir = _make_results_dir(tmp_path, "autolens", "imaging", [
        {"file": "imaging/simulator.py", "status": "failed", "duration_seconds": 50.0},
    ])
    summary = st.run(rdir)
    # Failed scripts are skipped — no entry created.
    assert summary["total_scripts"] == 0


def test_rolling_window_caps_history_length(tmp_state):
    tmp_path, st = tmp_state
    for d in [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]:
        rdir = _make_results_dir(tmp_path, "autolens", "imaging", [
            {"file": "imaging/simulator.py", "status": "passed", "duration_seconds": d},
        ])
        st.run(rdir)
    # Default window is 7 → history should be trimmed.
    history_files = list(st.HEART_TIMINGS_DIR.glob("*.json"))
    assert len(history_files) == 1
    history = json.loads(history_files[0].read_text())
    assert len(history) == 7
    assert history[-1] == 19.0  # most recent
