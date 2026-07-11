"""tests/test_workspace_testmode_timing.py — TEST_MODE script-timing classifier.

Stdlib-only (internals rule 4): the script runner is injected, so the suite
never runs real workspace scripts or imports the science stack.
"""

from __future__ import annotations

import importlib
import json

import pytest

SCRIPTS = [("autolens_workspace", "scripts/imaging/modeling/start_here.py")]


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.checks.workspace_testmode_timing as wt
    importlib.reload(wt)
    wt.HEART_STATE_DIR = tmp_path
    wt.HEART_WS_TIMINGS_DIR = tmp_path / "timings-ws-testmode"
    wt.HEART_WS_TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    return tmp_path, wt


def _runner(value):
    return lambda workspace, rel: value


def test_first_observation_has_no_baseline(tmp_state):
    _, wt = tmp_state
    s = wt.run(runner=_runner(20.0), scripts=SCRIPTS)
    assert s["new_scripts_no_baseline"] == 1
    assert s["red_count"] == 0 and s["yellow_count"] == 0
    assert s["scripts_measured"] == 1


def test_within_baseline_green(tmp_state):
    _, wt = tmp_state
    wt.run(runner=_runner(20.0), scripts=SCRIPTS)
    s = wt.run(runner=_runner(20.0), scripts=SCRIPTS)
    assert s["green_count"] == 1 and s["red_count"] == 0 and s["yellow_count"] == 0


def test_yellow_and_red_thresholds(tmp_state):
    _, wt = tmp_state
    wt.run(runner=_runner(20.0), scripts=SCRIPTS)
    s = wt.run(runner=_runner(40.0), scripts=SCRIPTS)  # 2.0×
    assert s["yellow_count"] == 1 and s["red_count"] == 0
    s2 = wt.run(runner=_runner(200.0), scripts=SCRIPTS)  # >3× vs updated median
    assert s2["red_count"] == 1


def test_script_unavailable(tmp_state):
    _, wt = tmp_state
    s = wt.run(runner=_runner(None), scripts=SCRIPTS)
    assert s["scripts_measured"] == 0
    assert s["scripts_unavailable"] == ["autolens_workspace/scripts/imaging/modeling/start_here.py"]


def test_rolling_window_caps_history(tmp_state):
    _, wt = tmp_state
    for d in [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]:
        wt.run(runner=_runner(d), scripts=SCRIPTS)
    files = list(wt.HEART_WS_TIMINGS_DIR.glob("*.json"))
    assert len(files) == 1
    assert len(json.loads(files[0].read_text())) == 7


def test_summary_records_test_mode(tmp_state):
    _, wt = tmp_state
    s = wt.run(runner=_runner(20.0), scripts=SCRIPTS)
    assert "PYAUTO_TEST_MODE" in s["mode"] and "SMALL_DATASETS" in s["mode"]
