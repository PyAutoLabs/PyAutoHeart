"""tests/test_import_time.py — import-cost regression classifier.

Stdlib-only (internals rule 4): the measurer is injected, so the suite never
imports the science/JAX stack — it exercises the rolling-baseline + classifier
logic with fixed durations.
"""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect HEART_STATE_DIR to a tmp dir and reload heart.checks.import_time."""
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.checks.import_time as it
    importlib.reload(it)
    it.HEART_STATE_DIR = tmp_path
    it.HEART_IMPORT_TIMINGS_DIR = tmp_path / "timings-import"
    it.HEART_IMPORT_TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    return tmp_path, it


def _fixed(value):
    """A measurer that always reports `value` seconds (or None)."""
    return lambda pkg: value


def test_first_observation_has_no_baseline(tmp_state):
    _, it = tmp_state
    summary = it.run(measurer=_fixed(1.0), packages=["autolens"])
    assert summary["new_packages_no_baseline"] == 1
    assert summary["red_count"] == 0 and summary["yellow_count"] == 0
    assert summary["packages_measured"] == 1


def test_within_baseline_classified_green(tmp_state):
    _, it = tmp_state
    it.run(measurer=_fixed(1.0), packages=["autolens"])
    summary = it.run(measurer=_fixed(1.0), packages=["autolens"])
    assert summary["green_count"] == 1
    assert summary["red_count"] == 0 and summary["yellow_count"] == 0


def test_above_yellow_factor_classified_yellow(tmp_state):
    _, it = tmp_state
    it.run(measurer=_fixed(1.0), packages=["autolens"])
    summary = it.run(measurer=_fixed(2.0), packages=["autolens"])  # ratio 2.0 > 1.5
    assert summary["yellow_count"] == 1 and summary["red_count"] == 0


def test_above_red_factor_classified_red(tmp_state):
    _, it = tmp_state
    it.run(measurer=_fixed(1.0), packages=["autolens"])
    summary = it.run(measurer=_fixed(4.0), packages=["autolens"])  # ratio 4.0 > 3.0
    assert summary["red_count"] == 1 and summary["yellow_count"] == 0


def test_unavailable_package_excluded(tmp_state):
    _, it = tmp_state
    summary = it.run(measurer=_fixed(None), packages=["autolens"])
    assert summary["packages_measured"] == 0
    assert summary["packages_unavailable"] == ["autolens"]
    # No history file is written for an unmeasurable package.
    assert not (it.HEART_IMPORT_TIMINGS_DIR / "autolens.json").is_file()


def test_rolling_window_caps_history_length(tmp_state):
    _, it = tmp_state
    for d in [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9]:
        it.run(measurer=_fixed(d), packages=["autolens"])
    history_files = list(it.HEART_IMPORT_TIMINGS_DIR.glob("*.json"))
    assert len(history_files) == 1
    history = json.loads(history_files[0].read_text())
    assert len(history) == 7  # default window
    assert history[-1] == 1.9


def test_summary_sidecar_written(tmp_state):
    tmp_path, it = tmp_state
    it.run(measurer=_fixed(1.0), packages=["autolens"])
    assert (tmp_path / "import_time.json").is_file()
