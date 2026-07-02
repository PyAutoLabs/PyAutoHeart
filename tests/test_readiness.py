"""tests/test_readiness.py — release-readiness verdict logic."""

from __future__ import annotations

import json

import pytest

from heart import readiness

LIBS = ["PyAutoConf", "PyAutoFit", "PyAutoArray", "PyAutoGalaxy", "PyAutoLens"]

# Deterministic 40-char main HEAD sha per library; the baseline validation
# report's commit_shas match these, so the release-validation gate stays GREEN.
SHAS = {lib: f"{i + 1:040x}" for i, lib in enumerate(LIBS)}


def _green_lib(sha: str = "") -> dict:
    return {
        "ci_status": {"conclusion": "success", "head_sha": sha},
        "repo_state": {"branch": "main", "dirty_real": 0, "behind": 0},
    }


def _green_validation_report(ts: str = "2026-06-01T00:00:00+00:00") -> dict:
    """A fresh, passing release-validation report matching the baseline HEADs."""
    return {
        "schema_version": 1,
        "release_ready": True,
        "testpypi_version": "2026.6.1.1.dev100",
        "profile": "release",
        "commit_shas": dict(SHAS),
        "stages": {
            "rehearse": {"status": "pass", "index": "testpypi", "version": "2026.6.1.1.dev100"},
            "integrate": {"status": "pass", "profile": "release"},
        },
        "totals": {"passed": 100, "failed": 0, "skipped": 0, "timeout": 0},
        "per_project": {},
        "failures": [],
        "run_urls": {},
        "ts": ts,
    }


def make_snapshot(**overrides) -> dict:
    """A fully-green baseline snapshot; override slices per test."""
    snap = {
        "ts": "2026-06-01T00:00:00+00:00",
        "repos": {lib: _green_lib(SHAS[lib]) for lib in LIBS},
        "script_timing": {"red_count": 0, "yellow_count": 0, "green_count": 10},
        "test_run": {"ready": True, "passed": 100, "failed": 0, "parked_stale_count": 0},
        "version_skew": {"workspaces": [{"workspace": "autolens_workspace", "status": "MATCH"}]},
        # fresh passing install verification (ts == snapshot ts → age 0, not stale)
        "verify_install": {"ready": True, "ts": "2026-06-01T00:00:00+00:00",
                           "version": "2026.6.1.1", "checks": []},
        # fresh passing release-validation rehearsal matching the current HEADs
        "validation_report": _green_validation_report(),
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


def test_test_run_failing_is_yellow_not_red():
    # Workspace scripts carry standing debt — failing validation is advisory.
    v = compute(make_snapshot(test_run={"ready": False, "failed": 9, "run_label": "x"}))
    assert v["verdict"] == "yellow"
    assert not v["red_reasons"]
    assert any("workspace validation not passing" in r and "9 failed" in r
               for r in v["yellow_reasons"])
    assert v["score"] == 85


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


# --- release-validation hard gate (M2) -----------------------------------


def test_validation_fresh_pass_matching_source_is_green():
    # baseline carries a fresh passing report whose commit_shas match the HEADs.
    v = compute(make_snapshot())
    assert v["verdict"] == "green"
    assert not any("release validation" in r for r in v["reasons"])


def test_validation_absent_is_yellow():
    snap = make_snapshot()
    del snap["validation_report"]
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert not v["red_reasons"]
    assert any("no release validation for current source" in r for r in v["yellow_reasons"])


def test_validation_empty_dict_is_yellow():
    v = compute(make_snapshot(validation_report={}))
    assert v["verdict"] == "yellow"
    assert any("no release validation" in r for r in v["yellow_reasons"])


def test_validation_failed_is_red():
    report = _green_validation_report()
    report["release_ready"] = False
    report["stages"]["integrate"] = {"status": "fail", "profile": "release"}
    v = compute(make_snapshot(validation_report=report))
    assert v["verdict"] == "red"
    assert any("release validation FAILED" in r and "integrate" in r for r in v["red_reasons"])
    assert v["score"] == 60  # validation_failed penalty 40


def test_validation_stale_by_sha_is_yellow():
    # A report whose commit_shas no longer match the current main HEADs is stale:
    # the source moved on since the rehearsal → caution, not a blocker, not green.
    snap = make_snapshot()
    snap["repos"]["PyAutoLens"]["ci_status"]["head_sha"] = "f" * 40  # HEAD moved
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert not v["red_reasons"]
    assert any("source moved since rehearsal" in r and "PyAutoLens" in r
               for r in v["yellow_reasons"])


def test_validation_wrong_profile_is_yellow():
    report = _green_validation_report()
    report["profile"] = "smoke"
    report["stages"]["integrate"] = {"status": "pass", "profile": "smoke"}
    v = compute(make_snapshot(validation_report=report))
    assert v["verdict"] == "yellow"
    assert any("profile 'smoke' is not 'release'" in r for r in v["yellow_reasons"])


def test_validation_stale_by_age_is_yellow():
    # passing + matching + release profile, but the rehearsal is >7d old.
    report = _green_validation_report(ts="2026-05-01T00:00:00+00:00")  # ~31d before snap ts
    v = compute(make_snapshot(validation_report=report))
    assert v["verdict"] == "yellow"
    assert any("release validation stale" in r for r in v["yellow_reasons"])


def test_validation_no_commit_shas_is_yellow():
    report = _green_validation_report()
    report["commit_shas"] = {}
    v = compute(make_snapshot(validation_report=report))
    assert v["verdict"] == "yellow"
    assert any("source unconfirmed" in r for r in v["yellow_reasons"])


def test_validation_missing_ts_is_yellow_not_silently_green():
    # Copilot review finding on PyAutoHeart#24: a missing/unparseable `ts`
    # made _age_days() return None, which the old `age is not None and ...`
    # check treated as "not stale" -> fell through to GREEN-eligible. Mirrors
    # the install_verification block's existing "age is None or age > ..." handling.
    report = _green_validation_report()
    del report["ts"]
    v = compute(make_snapshot(validation_report=report))
    assert v["verdict"] == "yellow"
    assert any("release validation stale" in r and "unknown" in r for r in v["yellow_reasons"])


def test_validation_partial_sha_confirmation_is_yellow_not_green():
    # Copilot review finding on PyAutoHeart#24: if some gated libraries match
    # and none explicitly mismatch, but at least one gated repo's current HEAD
    # is unknown (missing from the snapshot), the old logic still reached the
    # GREEN-eligible branch. An unknown must never be silently treated as green.
    report = _green_validation_report()
    snap = make_snapshot(validation_report=report)
    del snap["repos"]["PyAutoLens"]["ci_status"]["head_sha"]
    v = compute(snap)
    assert v["verdict"] == "yellow"
    assert any("partially unconfirmed" in r and "PyAutoLens" in r for r in v["yellow_reasons"])


def test_validation_ready_unknown_is_yellow():
    report = _green_validation_report()
    report["release_ready"] = None
    v = compute(make_snapshot(validation_report=report))
    assert v["verdict"] == "yellow"
    assert any("release validation status unknown" in r for r in v["yellow_reasons"])


def test_validation_failed_dominates_and_reds_first():
    report = _green_validation_report()
    report["release_ready"] = False
    report["stages"]["rehearse"] = {"status": "fail"}
    snap = make_snapshot(validation_report=report, script_timing={"red_count": 2})
    v = compute(snap)
    assert v["verdict"] == "red"
    assert v["reasons"][0] in v["red_reasons"]


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


REQ_WF = {
    "workspaces": ["Smoke Tests", "Navigator Check"],
    "workspaces_test": ["Smoke Tests"],
    "howto": ["Smoke Tests", "Navigator Check"],
    "libraries": ["Tests"],
}


def _ws_ci(group, **wf_conclusions):
    """A ci_status sidecar for a workspace repo with given workflow conclusions."""
    return {
        "group": group,
        "workflows": {
            name: {"conclusion": concl, "status": "completed", "on_head": True}
            for name, concl in wf_conclusions.items()
        },
    }


def _compute_ws(snap):
    return readiness.compute(snap, libraries=LIBS, required_workflows=REQ_WF)


def test_workspace_red_smoke_with_green_url_is_red():
    """Headline gate of the spec: a red workspace smoke_tests on main is a
    release blocker even though a (non-required) url_check is green."""
    snap = make_snapshot()
    snap["repos"]["autolens_workspace"] = {
        "ci_status": _ws_ci(
            "workspaces",
            **{"Smoke Tests": "failure", "Navigator Check": "success", "url_check": "success"},
        )
    }
    v = _compute_ws(snap)
    assert v["verdict"] == "red"
    assert any("autolens_workspace" in r and "Smoke Tests" in r for r in v["red_reasons"])


def test_workspace_all_required_green_is_green():
    snap = make_snapshot()
    snap["repos"]["autolens_workspace"] = {
        "ci_status": _ws_ci("workspaces", **{"Smoke Tests": "success", "Navigator Check": "success"})
    }
    v = _compute_ws(snap)
    assert v["verdict"] == "green"


def test_howto_navigator_failure_is_red():
    snap = make_snapshot()
    snap["repos"]["HowToLens"] = {
        "ci_status": _ws_ci("howto", **{"Smoke Tests": "success", "Navigator Check": "failure"})
    }
    v = _compute_ws(snap)
    assert v["verdict"] == "red"
    assert any("HowToLens" in r and "Navigator Check" in r for r in v["red_reasons"])


def test_workspace_test_smoke_failure_is_red():
    snap = make_snapshot()
    snap["repos"]["autolens_workspace_test"] = {
        "ci_status": _ws_ci("workspaces_test", **{"Smoke Tests": "failure"})
    }
    assert _compute_ws(snap)["verdict"] == "red"


def test_workspace_in_progress_required_is_not_red():
    # In-progress / not-concluded required workflow is unknown, never a hard RED
    # (mirrors the library gate which does not RED on an empty conclusion).
    snap = make_snapshot()
    snap["repos"]["autolens_workspace"] = {
        "ci_status": _ws_ci("workspaces", **{"Smoke Tests": "", "Navigator Check": "success"})
    }
    v = _compute_ws(snap)
    assert v["verdict"] == "green"
    assert not v["red_reasons"]


def test_workspace_skipped_required_is_not_red():
    # `skipped` is a non-event (e.g. path filter), not a failure.
    snap = make_snapshot()
    snap["repos"]["autolens_workspace"] = {
        "ci_status": _ws_ci("workspaces", **{"Smoke Tests": "skipped", "Navigator Check": "success"})
    }
    assert _compute_ws(snap)["verdict"] != "red"


def test_workspace_ci_fallback_to_rolled_conclusion():
    # Pre-structured sidecar (no `workflows` dict) → fall back to top-level
    # rolled-up conclusion.
    snap = make_snapshot()
    snap["repos"]["autogalaxy_workspace"] = {
        "ci_status": {"group": "workspaces", "conclusion": "failure"}
    }
    assert _compute_ws(snap)["verdict"] == "red"


def test_library_group_not_double_gated_by_workspace_loop():
    # A library carrying a ci_status.group of "libraries" must not be processed
    # by the workspace loop (it is gated by the library loop only).
    snap = make_snapshot()
    snap["repos"]["PyAutoLens"]["ci_status"] = {"group": "libraries", "conclusion": "success",
                                                "head_sha": SHAS["PyAutoLens"],
                                                "workflows": {"Tests": {"conclusion": "success",
                                                              "status": "completed", "on_head": True}}}
    v = _compute_ws(snap)
    assert v["verdict"] == "green"


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
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.readiness as r_mod
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
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    import heart.state as state_mod
    importlib.reload(state_mod)
    import heart.readiness as r_mod
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
