"""tests/test_readiness.py — release-readiness verdict logic."""

from __future__ import annotations

import json

import pytest

from pulse import readiness

LIBS = ["PyAutoConf", "PyAutoFit", "PyAutoArray", "PyAutoGalaxy", "PyAutoLens"]


def _green_lib() -> dict:
    return {
        "ci_status": {"conclusion": "success"},
        "repo_state": {"branch": "main", "dirty_real": 0, "behind": 0},
    }


def make_snapshot(**overrides) -> dict:
    """A fully-green baseline snapshot; override slices per test."""
    snap = {
        "ts": "2026-06-01T00:00:00+00:00",
        "repos": {lib: _green_lib() for lib in LIBS},
        "script_timing": {"red_count": 0, "yellow_count": 0, "green_count": 10},
        "test_run": {"ready": True, "passed": 100, "failed": 0, "parked_stale_count": 0},
        "version_skew": {"workspaces": [{"workspace": "autolens_workspace", "status": "MATCH"}]},
        # fresh passing install verification (ts == snapshot ts → age 0, not stale)
        "verify_install": {"ready": True, "ts": "2026-06-01T00:00:00+00:00",
                           "version": "2026.6.1.1", "checks": []},
    }
    snap.update(overrides)
    return snap


def compute(snap):
    return readiness.compute(snap, libraries=LIBS)


def test_all_green_snapshot_is_green():
    v = compute(make_snapshot())
    assert v["verdict"] == "green"
    assert v["score"] == 100
    assert v["red_reasons"] == [] and v["yellow_reasons"] == []


def test_one_library_ci_failing_is_red():
    snap = make_snapshot()
    snap["repos"]["PyAutoLens"]["ci_status"]["conclusion"] = "failure"
    v = compute(snap)
    assert v["verdict"] == "red"
    assert any("PyAutoLens" in r and "CI" in r for r in v["red_reasons"])
    assert v["reasons"][0] in v["red_reasons"]  # reds first
    assert v["score"] == 70


def test_test_run_stale_is_yellow():
    # ready but ~31 days before the snapshot ts → stale caution, not a blocker.
    snap = make_snapshot(test_run={"ready": True, "ts": "2026-05-01T00:00:00+00:00",
                                   "parked_stale_count": 0})
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert any("test run stale" in r for r in v["yellow_reasons"])


def test_test_run_fresh_ready_is_green():
    snap = make_snapshot(test_run={"ready": True, "ts": "2026-06-01T00:00:00+00:00",
                                   "parked_stale_count": 0})
    v = compute(snap)
    assert v["verdict"] == "green"


def test_test_run_not_ready_is_red():
    v = compute(make_snapshot(test_run={"ready": False, "run_label": "x"}))
    assert v["verdict"] == "red"
    assert any("test run not ready" in r for r in v["red_reasons"])
    assert v["score"] == 60


def test_version_skew_ahead_is_red():
    snap = make_snapshot(version_skew={"workspaces": [
        {"workspace": "autolens_workspace", "pinned": "2026.6.1.1", "installed": "2026.5.1.1", "status": "AHEAD"}
    ]})
    v = compute(snap)
    assert v["verdict"] == "red"
    assert any("AHEAD" in r for r in v["red_reasons"])
    assert v["score"] == 75


def test_version_skew_mismatch_is_red():
    snap = make_snapshot(version_skew={"workspaces": [
        {"workspace": "autolens_workspace", "pinned": "2026.6.1.2",
         "version_txt": "2026.1.1.1", "installed": "2026.6.1.2", "status": "MISMATCH"}
    ]})
    v = compute(snap)
    assert v["verdict"] == "red"
    assert any("general.yaml" in r and "version.txt" in r for r in v["red_reasons"])
    assert v["score"] == 75


def test_version_skew_bad_is_red():
    snap = make_snapshot(version_skew={"workspaces": [
        {"workspace": "autolens_workspace", "pinned": "not.a.version",
         "installed": "2026.6.1.2", "status": "BAD"}
    ]})
    v = compute(snap)
    assert v["verdict"] == "red"
    assert any("unparseable" in r for r in v["red_reasons"])


def test_version_skew_unknown_is_yellow():
    snap = make_snapshot(version_skew={"workspaces": [
        {"workspace": "autolens_workspace", "library": "PyAutoLens",
         "pinned": "2026.6.1.1", "installed": None, "status": "UNKNOWN"}
    ]})
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert any("version unknown" in r for r in v["yellow_reasons"])


def test_install_verification_failed_is_red():
    snap = make_snapshot(verify_install={
        "ready": False, "ts": "2026-06-01T00:00:00+00:00",
        "checks": [{"check": "A", "status": "PASS"}, {"check": "B", "status": "FAIL"}],
    })
    v = compute(snap)
    assert v["verdict"] == "red"
    assert any("install verification FAILED" in r and "B" in r for r in v["red_reasons"])
    assert v["score"] == 60


def test_install_verification_stale_is_yellow():
    snap = make_snapshot(verify_install={
        "ready": True, "ts": "2026-05-01T00:00:00+00:00",  # ~31d before snapshot ts
        "checks": [],
    })
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert any("install verification stale" in r for r in v["yellow_reasons"])


def test_install_verification_not_run_is_yellow():
    snap = make_snapshot()
    snap.pop("verify_install")
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert any("install verification not run" in r for r in v["yellow_reasons"])


def test_install_verification_fresh_pass_is_green():
    # baseline already carries a fresh passing verify_install → stays green.
    v = compute(make_snapshot())
    assert v["verdict"] == "green"
    assert not any("install" in r for r in v["reasons"])


def test_library_off_main_is_red():
    snap = make_snapshot()
    snap["repos"]["PyAutoFit"]["repo_state"]["branch"] = "feature/x"
    v = compute(snap)
    assert v["verdict"] == "red"
    assert v["score"] == 85


def test_library_dirty_is_red():
    snap = make_snapshot()
    snap["repos"]["PyAutoFit"]["repo_state"]["dirty_real"] = 3
    v = compute(snap)
    assert v["verdict"] == "red"
    assert v["score"] == 85


def test_library_behind_is_red():
    snap = make_snapshot()
    snap["repos"]["PyAutoArray"]["repo_state"]["behind"] = 2
    v = compute(snap)
    assert v["verdict"] == "red"
    assert v["score"] == 80


def test_only_timing_regressions_is_yellow():
    v = compute(make_snapshot(script_timing={"red_count": 2, "yellow_count": 5}))
    assert v["verdict"] == "yellow"
    assert v["red_reasons"] == []
    assert v["score"] == 85


def test_old_open_pr_is_yellow():
    snap = make_snapshot()
    snap["repos"]["PyAutoArray"]["open_prs"] = {"open_count": 1, "max_age_days": 10}
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert any("open PR" in r for r in v["yellow_reasons"])


def test_version_skew_behind_is_yellow():
    snap = make_snapshot(version_skew={"workspaces": [
        {"workspace": "autolens_workspace", "installed": "2026.6.1.1", "status": "BEHIND"}
    ]})
    v = compute(snap)
    assert v["verdict"] == "yellow"


def test_parked_stale_is_yellow():
    v = compute(make_snapshot(test_run={"ready": True, "parked_stale_count": 3}))
    assert v["verdict"] == "yellow"
    assert any("parked" in r for r in v["yellow_reasons"])


def test_missing_test_run_is_yellow_unknown_not_crash():
    snap = make_snapshot()
    del snap["test_run"]
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert any("unknown" in r for r in v["yellow_reasons"])
    assert v["score"] == 90


def test_red_dominates_yellow():
    snap = make_snapshot(script_timing={"red_count": 3})
    snap["repos"]["PyAutoLens"]["ci_status"]["conclusion"] = "failure"
    v = compute(snap)
    assert v["verdict"] == "red"
    assert v["red_reasons"] and v["yellow_reasons"]
    assert v["reasons"][0] in v["red_reasons"]


def test_missing_library_is_yellow_unknown():
    snap = make_snapshot()
    del snap["repos"]["PyAutoConf"]
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert any("PyAutoConf" in r and "unknown" in r for r in v["yellow_reasons"])


def test_empty_snapshot_not_green_no_crash():
    v = readiness.compute({}, libraries=LIBS)
    assert v["verdict"] == "yellow"   # unknowns, never green on no data
    assert v["score"] < 100
    json.dumps(v)


def test_score_clamped_to_zero_floor():
    snap = make_snapshot(test_run={"ready": False})
    for lib in LIBS:
        snap["repos"][lib]["ci_status"]["conclusion"] = "failure"
        snap["repos"][lib]["repo_state"] = {"branch": "x", "dirty_real": 9, "behind": 9}
    v = compute(snap)
    assert v["verdict"] == "red"
    assert v["score"] >= 0


def test_score_caps_prevent_single_gate_zeroing():
    # All 5 libs behind → behind penalty capped at 40 → score 60, not 0.
    snap = make_snapshot()
    for lib in LIBS:
        snap["repos"][lib]["repo_state"]["behind"] = 5
    v = compute(snap)
    assert v["score"] == 60


def test_legacy_dirty_files_field_counts():
    snap = make_snapshot()
    snap["repos"]["PyAutoFit"]["repo_state"] = {"branch": "main", "dirty_files": 4}
    v = compute(snap)
    assert v["verdict"] == "red"  # fallback path


def test_malformed_version_skew_is_skipped():
    for bad in (None, [], {"workspaces": None}, {"workspaces": ["x"]}):
        v = compute(make_snapshot(version_skew=bad))
        assert v["verdict"] in ("green", "yellow", "red")  # no crash


def test_run_writes_release_ready_json(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("PULSE_STATE_DIR", str(tmp_path))
    import pulse.state as state_mod
    importlib.reload(state_mod)
    import pulse.readiness as r_mod
    importlib.reload(r_mod)
    # seed a state.json
    (tmp_path / "state.json").write_text(json.dumps(make_snapshot()))
    v = r_mod.run()
    out = tmp_path / "release_ready.json"
    assert out.is_file()
    assert json.loads(out.read_text())["verdict"] == v["verdict"]
    assert [p for p in tmp_path.iterdir() if ".tmp" in p.name] == []
    # restore modules for other tests
    importlib.reload(state_mod)
    importlib.reload(r_mod)


def test_run_with_no_state_cache_still_writes(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("PULSE_STATE_DIR", str(tmp_path))
    import pulse.state as state_mod
    importlib.reload(state_mod)
    import pulse.readiness as r_mod
    importlib.reload(r_mod)
    v = r_mod.run()
    assert (tmp_path / "release_ready.json").is_file()
    assert v["verdict"] == "yellow"
    importlib.reload(state_mod)
    importlib.reload(r_mod)


def test_render_block_no_color_is_plain(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    lines = readiness.render_block(compute(make_snapshot()))
    text = "\n".join(lines)
    assert "RELEASE READINESS" in text
    assert "GREEN" in text
    assert "\033[" not in text
