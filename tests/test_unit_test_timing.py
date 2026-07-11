"""tests/test_unit_test_timing.py — slow-test regression classifier + parser.

Stdlib-only (internals rule 4): the pytest runner is injected, so the suite
never runs pytest-in-pytest or imports the science stack.
"""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.checks.unit_test_timing as ut
    importlib.reload(ut)
    ut.HEART_STATE_DIR = tmp_path
    ut.HEART_UNIT_TIMINGS_DIR = tmp_path / "timings-unit"
    ut.HEART_UNIT_TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    return tmp_path, ut


def _runner(mapping):
    """A runner that returns `mapping` (or None) for any repo."""
    return lambda repo_dir: mapping


def test_first_observation_has_no_baseline(tmp_state):
    _, ut = tmp_state
    s = ut.run(runner=_runner({"t.py::test_a": 5.0}), repos=["PyAutoFit"])
    assert s["new_tests_no_baseline"] == 1
    assert s["red_count"] == 0 and s["yellow_count"] == 0
    assert s["repos_measured"] == 1


def test_within_baseline_green(tmp_state):
    _, ut = tmp_state
    ut.run(runner=_runner({"t.py::test_a": 5.0}), repos=["PyAutoFit"])
    s = ut.run(runner=_runner({"t.py::test_a": 5.0}), repos=["PyAutoFit"])
    assert s["green_count"] == 1 and s["red_count"] == 0 and s["yellow_count"] == 0


def test_yellow_and_red_thresholds(tmp_state):
    _, ut = tmp_state
    ut.run(runner=_runner({"t.py::test_a": 5.0}), repos=["PyAutoFit"])
    s = ut.run(runner=_runner({"t.py::test_a": 10.0}), repos=["PyAutoFit"])  # 2.0×
    assert s["yellow_count"] == 1 and s["red_count"] == 0
    ut.run(runner=_runner({"t.py::test_b": 5.0}), repos=["PyAutoFit"])
    s = ut.run(runner=_runner({"t.py::test_b": 20.0}), repos=["PyAutoFit"])  # 4.0×
    assert s["red_count"] == 1


def test_repo_unavailable(tmp_state):
    _, ut = tmp_state
    s = ut.run(runner=_runner(None), repos=["PyAutoFit"])
    assert s["repos_measured"] == 0 and s["repos_unavailable"] == ["PyAutoFit"]


def test_rolling_window_caps_history(tmp_state):
    _, ut = tmp_state
    for d in [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9]:
        ut.run(runner=_runner({"t.py::test_a": d}), repos=["PyAutoFit"])
    files = list(ut.HEART_UNIT_TIMINGS_DIR.glob("*.json"))
    assert len(files) == 1
    assert len(json.loads(files[0].read_text())) == 7


def test_parse_durations_reads_call_lines_only(tmp_state):
    _, ut = tmp_state
    stdout = (
        "===== slowest 3 durations =====\n"
        "12.34s call test_autofit/test_x.py::test_slow\n"
        "1.20s setup test_autofit/test_x.py::test_slow\n"
        "0.98s call test_autofit/test_y.py::test_other\n"
        "\n===== short test summary =====\n"
    )
    parsed = ut.parse_durations(stdout)
    assert parsed == {
        "test_autofit/test_x.py::test_slow": 12.34,
        "test_autofit/test_y.py::test_other": 0.98,
    }
