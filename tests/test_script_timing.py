"""tests/test_script_timing.py — regression classifier thresholds, run-identity
dedup, baseline provenance and orphan migration.

The dedup tests are the load-bearing ones: the tick re-reads the same
``run_logs/latest`` on every cycle, and the check used to append those same
durations until the window held seven copies of one observation. "Same run
re-ticked" must therefore leave the window's length alone.

Repo/workspace names here are deliberately fictional (``workspace_a``) — this
suite is organ code under the tenant firewall.
"""

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


def _make_results_dir(
    root: Path,
    project: str,
    directory: str,
    results: list[dict],
    run: str = "run_1",
) -> Path:
    """Write a run_all-shaped results dir for one run.

    ``run`` names the directory, which is the run identity the check reads —
    two calls with the same ``run`` are the same run observed twice.
    """
    rdir = root / "run_logs" / run
    rdir.mkdir(parents=True, exist_ok=True)
    safe_dir = directory.replace("/", "__")
    fname = f"{project}__scripts__{safe_dir}__script.json"
    (rdir / fname).write_text(json.dumps({
        "project": project,
        "directory": f"scripts/{directory}",
        "results": results,
    }))
    return rdir


def _observe(tmp_path, st, duration: float, run: str, file: str = "imaging/simulator.py"):
    """Run one observation of ``file`` at ``duration`` under run id ``run``."""
    rdir = _make_results_dir(tmp_path, "workspace_a", "imaging", [
        {"file": file, "status": "passed", "duration_seconds": duration},
    ], run=run)
    return st.run(rdir)


def _only_history(st) -> list[dict]:
    files = list(st.HEART_TIMINGS_DIR.glob("*.json"))
    assert len(files) == 1, [f.name for f in files]
    return json.loads(files[0].read_text())


def test_first_observation_has_no_baseline(tmp_state):
    tmp_path, st = tmp_state
    summary = _observe(tmp_path, st, 50.0, "run_1")
    assert summary["new_scripts_no_baseline"] == 1
    assert summary["red_count"] == 0
    assert summary["yellow_count"] == 0


def test_within_baseline_classified_green(tmp_state):
    tmp_path, st = tmp_state
    # Three distinct prior runs are the floor for a verdict; the fourth is
    # compared against their median (50) → ratio 1.0 → green.
    for run in ("run_1", "run_2", "run_3"):
        _observe(tmp_path, st, 50.0, run)
    summary = _observe(tmp_path, st, 50.0, "run_4")
    assert summary["red_count"] == 0
    assert summary["yellow_count"] == 0
    assert summary["green_count"] == 1
    assert summary["building_count"] == 0


def test_above_yellow_factor_classified_yellow(tmp_state):
    tmp_path, st = tmp_state
    for run in ("run_1", "run_2", "run_3"):
        _observe(tmp_path, st, 50.0, run)
    summary = _observe(tmp_path, st, 100.0, "run_4")
    assert summary["yellow_count"] == 1
    assert summary["red_count"] == 0


def test_above_red_factor_classified_red(tmp_state):
    tmp_path, st = tmp_state
    for run in ("run_1", "run_2", "run_3"):
        _observe(tmp_path, st, 50.0, run)
    summary = _observe(tmp_path, st, 200.0, "run_4")
    assert summary["red_count"] == 1
    assert summary["yellow_count"] == 0


def test_failed_scripts_excluded_from_baseline(tmp_state):
    tmp_path, st = tmp_state
    rdir = _make_results_dir(tmp_path, "workspace_a", "imaging", [
        {"file": "imaging/simulator.py", "status": "failed", "duration_seconds": 50.0},
    ])
    summary = st.run(rdir)
    # Failed scripts are skipped — no entry created.
    assert summary["total_scripts"] == 0


def test_rolling_window_caps_history_length(tmp_state):
    tmp_path, st = tmp_state
    for i, d in enumerate([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]):
        _observe(tmp_path, st, d, f"run_{i}")
    # Default window is 7 → history should be trimmed.
    history = _only_history(st)
    assert len(history) == 7
    assert history[-1]["duration_s"] == 19.0  # most recent
    # Ten distinct runs in, seven distinct runs stored — no repetition.
    assert len({e["run_id"] for e in history}) == 7


def test_same_run_reticked_does_not_grow_window(tmp_state):
    """The defect: re-reading run_logs/latest each tick filled the window."""
    tmp_path, st = tmp_state
    _observe(tmp_path, st, 10.0, "run_1")
    _observe(tmp_path, st, 20.0, "run_2")
    assert len(_only_history(st)) == 2

    # Same run observed four more times — the window must not grow.
    for _ in range(4):
        _observe(tmp_path, st, 20.0, "run_2")
    history = _only_history(st)
    assert len(history) == 2
    assert [e["run_id"] for e in history] == ["run_1", "run_2"]


def test_same_run_retick_replaces_the_newest_entry(tmp_state):
    tmp_path, st = tmp_state
    _observe(tmp_path, st, 10.0, "run_1")
    _observe(tmp_path, st, 20.0, "run_2")
    # A corrected duration for the same run overwrites, it does not append.
    _observe(tmp_path, st, 25.0, "run_2")
    history = _only_history(st)
    assert [e["duration_s"] for e in history] == [10.0, 25.0]


def test_legacy_identical_window_collapses_to_one_entry(tmp_state):
    """Seven copies of one number were never seven runs — collapse them."""
    tmp_path, st = tmp_state
    slug = st.slug_for("workspace_a", "scripts/imaging", "imaging/simulator.py")
    (st.HEART_TIMINGS_DIR / slug).write_text(json.dumps([45.99] * 7))

    summary = _observe(tmp_path, st, 46.0, "run_1")
    history = _only_history(st)
    assert [e["duration_s"] for e in history] == [45.99, 46.0]
    assert history[0]["run_id"] == ""  # legacy entry, provenance unknown
    # One prior sample is below the floor — building, not a verdict.
    assert summary["building_count"] == 1
    assert summary["green_count"] == 0


def test_legacy_mixed_float_history_still_classifies(tmp_state):
    """Real accumulation predating provenance keeps its baseline."""
    tmp_path, st = tmp_state
    slug = st.slug_for("workspace_a", "scripts/imaging", "imaging/simulator.py")
    (st.HEART_TIMINGS_DIR / slug).write_text(json.dumps([10.0, 12.0, 11.0]))

    summary = _observe(tmp_path, st, 11.0, "run_1")
    assert summary["green_count"] == 1
    assert summary["building_count"] == 0
    assert summary["green_count"] + summary["yellow_count"] + summary["red_count"] == 1
    history = _only_history(st)
    assert len(history) == 4


def test_baseline_floor_needs_three_distinct_runs(tmp_state):
    tmp_path, st = tmp_state
    first = _observe(tmp_path, st, 50.0, "run_1")
    assert first["new_scripts_no_baseline"] == 1
    assert first["building_count"] == 0

    second = _observe(tmp_path, st, 50.0, "run_2")
    assert second["building_count"] == 1
    assert second["green_count"] == 0

    third = _observe(tmp_path, st, 50.0, "run_3")
    assert third["building_count"] == 1
    assert third["green_count"] == 0

    fourth = _observe(tmp_path, st, 50.0, "run_4")
    assert fourth["building_count"] == 0
    assert fourth["green_count"] == 1


def test_unambiguous_orphan_is_migrated_and_history_continues(tmp_state):
    """A moved script keeps its baseline when exactly one orphan matches."""
    tmp_path, st = tmp_state
    old_slug = "workspace_a__scripts__jax_grad__lp.json"
    (st.HEART_TIMINGS_DIR / old_slug).write_text(json.dumps([
        {"duration_s": 40.0, "run_id": "old_1", "ts": ""},
        {"duration_s": 41.0, "run_id": "old_2", "ts": ""},
        {"duration_s": 39.0, "run_id": "old_3", "ts": ""},
    ]))

    summary = _observe(tmp_path, st, 40.0, "run_1", file="scripts/imaging/jax_grad/lp.py")
    new_slug = "workspace_a__scripts__imaging__jax_grad__lp.json"

    assert not (st.HEART_TIMINGS_DIR / old_slug).exists()
    assert (st.HEART_TIMINGS_DIR / new_slug).is_file()
    assert summary["migrated_count"] == 1
    assert summary["migrated"] == [{"from": old_slug, "to": new_slug}]
    assert summary["orphaned_count"] == 0
    # The baseline survived the move, so this run is classified, not "new".
    assert summary["green_count"] == 1
    assert summary["new_scripts_no_baseline"] == 0
    history = json.loads((st.HEART_TIMINGS_DIR / new_slug).read_text())
    assert [e["duration_s"] for e in history] == [40.0, 41.0, 39.0, 40.0]


def test_ambiguous_orphans_are_not_migrated_and_are_reported(tmp_state):
    """Two candidates means we do not know — report, never guess."""
    tmp_path, st = tmp_state
    orphans = [
        "workspace_a__scripts__jax_grad__lp.json",
        "workspace_a__scripts__point_source__jax_grad__lp.json",
    ]
    for name in orphans:
        (st.HEART_TIMINGS_DIR / name).write_text(json.dumps([40.0, 41.0, 39.0]))

    summary = _observe(tmp_path, st, 40.0, "run_1", file="scripts/imaging/jax_grad/lp.py")

    for name in orphans:
        assert (st.HEART_TIMINGS_DIR / name).is_file()
    assert summary["migrated_count"] == 0
    assert summary["orphaned_count"] == 2
    assert sorted(summary["orphaned"]) == sorted(orphans)
    # No baseline was adopted, so this is a first observation.
    assert summary["new_scripts_no_baseline"] == 1


def test_history_entries_carry_run_provenance(tmp_state):
    """Entry shape on disk: duration_s + run_id + ts, run_id resolved through
    the `latest` symlink to the real run dir."""
    tmp_path, st = tmp_state
    rdir = _make_results_dir(tmp_path, "workspace_a", "imaging", [
        {"file": "imaging/simulator.py", "status": "passed", "duration_seconds": 50.0},
    ], run="2026-08-24_120000")
    latest = tmp_path / "run_logs" / "latest"
    latest.symlink_to(rdir, target_is_directory=True)

    summary = st.run(latest)
    history = _only_history(st)
    assert len(history) == 1
    entry = history[0]
    assert set(entry) == {"duration_s", "run_id", "ts"}
    assert isinstance(entry["duration_s"], float)
    assert entry["run_id"] == "2026-08-24_120000"
    assert entry["ts"]
    assert summary["run_id"] == "2026-08-24_120000"


def test_history_is_written_atomically(tmp_state):
    """House rule: state writes go through heart.state.atomic_write_json."""
    tmp_path, st = tmp_state
    import heart.state as state_mod

    written: list[str] = []
    original = state_mod.atomic_write_json

    def spy(path, payload):
        written.append(Path(path).name)
        original(path, payload)

    state_mod.atomic_write_json = spy
    try:
        _observe(tmp_path, st, 50.0, "run_1")
    finally:
        state_mod.atomic_write_json = original

    slug = st.slug_for("workspace_a", "scripts/imaging", "imaging/simulator.py")
    assert slug in written
    assert "script_timing.json" in written
    # No half-written temp files left behind.
    assert not list(st.HEART_TIMINGS_DIR.glob("*.tmp"))


def test_summary_carries_the_new_counters(tmp_state):
    tmp_path, st = tmp_state
    summary = _observe(tmp_path, st, 50.0, "run_1")
    for key in ("building_count", "migrated_count", "orphaned_count",
                "migrated", "orphaned", "new_scripts_no_baseline",
                "red_count", "yellow_count", "green_count", "total_scripts"):
        assert key in summary
