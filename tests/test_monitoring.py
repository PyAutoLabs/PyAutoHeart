"""Monitoring and repair must account for the same complete evidence set."""
import datetime as dt
from types import SimpleNamespace

import pytest

from heart import dashboard, monitoring, readiness

TS = "2026-10-02T10:00:00+00:00"
NOW = dt.datetime.fromisoformat(TS)


def board(sections=(), **kw):
    return SimpleNamespace(ts=TS, sections=list(sections), verdict="green", blockers=[], **kw)


def assess(b, s, families=(), repos=()):
    return monitoring.assess(b, s, now=NOW, families=families, repos=repos)


def test_fresh_green_complete_inventory_scores_100():
    sec = dashboard.Section("script_timing", "Scripts", "ok", "1 compared")
    m = assess(board([sec]), {"script_timing": {"ts": TS, "green_count": 1}}, families=["script_timing"])
    assert m["score"] == 100 and m["complete"] and m["status"] == "green"
    assert not m["findings"]


@pytest.mark.parametrize("state", ["warn", "fail", "unobserved", "info"])
def test_every_non_green_section_prevents_100(state):
    sec = dashboard.Section("custom", "Custom check", state, "Inspect")
    m = assess(board([sec]), {})
    assert m["score"] < 100 and not m["complete"]
    assert all(c["action"]["payload"] for c in m["findings"])


def test_expected_missing_check_and_missing_repository_are_not_success():
    m = assess(board(), {}, families=["url_check"], repos=["Example"])
    assert {c["family"] for c in m["findings"]} == {"url_check", "ci_status", "repo_state", "open_prs"}
    assert all(c["status"] == "grey" for c in m["findings"])


def test_aggregate_timestamp_does_not_rejuvenate_old_sidecar():
    sec = dashboard.Section("script_timing", "Scripts", "ok", "1 compared")
    m = assess(board([sec]), {"script_timing": {"ts": "2026-09-01T00:00:00Z", "green_count": 1}}, families=["script_timing"])
    assert m["findings"][0]["status"] == "stale"
    assert m["score"] < 100


def test_adverse_old_evidence_remains_adverse():
    sec = dashboard.Section("script_timing", "Scripts", "fail", "regression")
    m = assess(board([sec]), {"script_timing": {"ts": "2026-09-01T00:00:00Z", "red_count": 1}}, families=["script_timing"])
    assert m["status"] == "red"


def test_zero_compared_tests_is_evidence_gap():
    sec = dashboard.Section("unit_test_timing", "Tests", "ok", "0 tracked tests within baseline")
    m = assess(board([sec]), {"unit_test_timing": {"repos_measured": 5, "green_count": 0}}, families=["unit_test_timing"])
    assert m["findings"][0]["status"] == "grey"


def test_permanent_exclusions_are_explicitly_inapplicable():
    sec = dashboard.Section("no_run_census", "Skips", "info", "Permanent exclusions")
    m = assess(board([sec]), {"no_run_census": {"ts": TS, "totals": {"permanent": 3}, "rows": []}}, families=["no_run_census"])
    assert m["complete"] and m["score"] == 100
    assert m["checks"][0]["applicability_reason"]
    assert m["checks"][0]["applicable"] is False


def test_full_skip_rows_beyond_display_limit_and_stable_ids():
    rows = [{"repo": "Example", "entry": f"script{i}.py", "marker": "NEEDS_FIX", "prompt": f"Repair script {i}"} for i in range(30)]
    s = {"no_run_census": {"ts": TS, "rows": rows}}
    first = assess(board(), s, families=["no_run_census"])
    s["no_run_census"]["rows"] = list(reversed(rows))
    second = assess(board(), s, families=["no_run_census"])
    assert {c["id"] for c in first["findings"]} == {c["id"] for c in second["findings"]}
    assert any(c["action"]["payload"] == "Repair script 29" for c in first["findings"])


def test_nested_non_gating_workflow_failure_is_not_hidden_by_rollup():
    obs = {"ts": TS, "status": "completed", "conclusion": "success",
           "workflows": {"Optional": {"status": "completed", "conclusion": "failure"}}}
    m = assess(board(), {"repos": {"Example": {"ci_status": obs}}}, repos=["Example"])
    assert any(c["status"] == "red" and "Optional" in c["subject"] for c in m["findings"])
    assert not any(c["status"] == "grey" and c["family"] == "ci_status" for c in m["findings"])


def test_monitoring_does_not_change_release_gate_and_prompt_uses_complete_scope(monkeypatch):
    monkeypatch.setattr(dashboard, "REPO_OWNERS", {"SomeHeart": "SomeOrg"})
    monkeypatch.setattr(dashboard, "PAGES_URL", "https://someorg.github.io/SomeHeart/")
    s = {"ts": TS, "ci_timing": {"ts": TS, "events": [{"repo": "Example", "kind": "suspect_cancelled"}]}}
    before = readiness.compute(s)
    b = dashboard.build_board(s, {"verdict": "green", "score": 100}, now=NOW)
    assert b.score == 100 and b.verdict == "green"
    assert b.monitoring["score"] < 100 and b.monitoring["status"] == "red"
    assert readiness.compute(s) == before
    assert "improve SomeOrg health" in b.fix_plan["prompt"]
    assert "--scope dashboard" in b.fix_plan["prompt"]
    assert "monitoring.complete" in b.fix_plan["prompt"]
    assert len(b.fix_plan["prompt"]) < 45000
    doc = dashboard.to_dict(b)
    assert doc["monitoring"]["findings"] == b.monitoring["findings"]
    assert "Monitoring RED" in dashboard.badge_endpoint(b)["message"]
    assert dashboard.to_state(b)["status"] == "green"  # release gate feed compatibility
    assert "monitoring RED" in dashboard.to_state(b)["headline"]


def test_published_local_inventory_preserves_individual_findings():
    sec = dashboard.Section("worktree_drift", "Drift", "fail", "dirty", observed_ago="observed just now")
    item = {"id": "drift:task30", "subject": "task30", "status": "red", "summary": "dirty", "observed_at": TS}
    m = monitoring.assess(board([sec]), {}, devbox={"ts": TS, "sections": {"worktree_drift": {"monitoring_checks": [item]}}}, now=NOW, families=["worktree_drift"], repos=[])
    assert any(c["id"] == "drift:task30" for c in m["findings"])


@pytest.mark.parametrize("family,counter", [("script_timing", "new_scripts_no_baseline"), ("script_timing", "building_count"), ("unit_test_timing", "new_tests_no_baseline"), ("import_time", "new_packages_no_baseline")])
def test_partial_comparison_coverage_cannot_earn_100(family, counter):
    sec = dashboard.Section(family, "Timing", "ok", "one green")
    m = assess(board([sec]), {family: {"ts": TS, "green_count": 1, counter: 20}}, families=[family])
    assert not m["complete"] and m["score"] < 100
    assert any(c["subject"] == counter for c in m["findings"])


def test_undated_observation_is_not_refreshed_by_aggregate():
    sec = dashboard.Section("script_timing", "Scripts", "ok", "1 compared")
    m = assess(board([sec]), {"ts": TS, "script_timing": {"green_count": 1}}, families=["script_timing"])
    assert m["findings"][0]["status"] == "grey"
    assert m["findings"][0]["observed_at"] is None


def test_raw_local_repo_observations_close_cloud_gaps():
    obs = {"ts": TS, "branch": "main", "dirty_real": 0, "ahead": 0, "behind": 0}
    snapshot = {"repos": {"Example": {"ci_status": {"ts": TS, "conclusion": "success"}, "open_prs": {"ts": TS, "open_count": 0}}}}
    m = monitoring.assess(board(), snapshot, devbox={"ts": TS, "repo_observations": {"Example": {"repo_state": obs}}}, now=NOW, families=[], repos=["Example"])
    assert m["complete"] and m["score"] == 100
    obs["behind"] = 2
    m = monitoring.assess(board(), snapshot, devbox={"ts": TS, "repo_observations": {"Example": {"repo_state": obs}}}, now=NOW, families=[], repos=["Example"])
    assert not m["complete"]


def test_legacy_green_devbox_summary_requires_complete_evidence():
    sec = dashboard.Section("script_timing", "Timing", "ok", "one green", observed_ago="just now")
    m = monitoring.assess(board([sec]), {}, devbox={"ts": TS, "sections": {"script_timing": {"state": "ok"}}}, now=NOW, families=["script_timing"], repos=[])
    assert any("complete inventory" in c["summary"] for c in m["findings"])


def test_private_findings_survive_publication_without_private_paths():
    from heart.publish import _public_monitoring
    item = {"id": "worktree_drift:/home/private/tree", "family": "worktree_drift", "status": "red", "subject": "/home/private/tree", "observed_at": TS}
    public = _public_monitoring(item)
    assert public["status"] == "red"
    assert "/home/private" not in str(public)
    assert public["action"]["payload"]


def test_orphaned_baselines_are_not_lost_and_family_is_charged_once():
    sec = dashboard.Section("script_timing", "Timing", "ok", "one green")
    data = {"ts": TS, "green_count": 1, "orphaned_count": 2, "orphaned": [{"reason": "old baseline one"}, {"reason": "old baseline two"}]}
    m = assess(board([sec]), {"script_timing": data}, families=["script_timing"])
    assert len([c for c in m["findings"] if "old baseline" in c["summary"]]) == 2
    assert m["score"] == 95


def test_fresh_published_complete_timing_survives_empty_cloud_slice():
    sec = dashboard.Section("script_timing", "Scripts", "ok", "all compared", observed_ago="just now")
    item = {"id": "script_timing:coverage", "subject": "Scripts", "status": "green", "summary": "all compared", "observed_at": TS}
    m = monitoring.assess(board([sec]), {"script_timing": {}}, devbox={"ts": TS, "sections": {"script_timing": {"monitoring_checks": [item]}}}, now=NOW, families=["script_timing"], repos=[])
    assert m["complete"] and m["score"] == 100


def test_source_run_timestamp_survives_reaggregation(tmp_path):
    import json
    import os
    from heart.checks import script_timing
    path = tmp_path / "example__script.json"
    path.write_text(json.dumps({"project": "Example", "directory": "scripts", "results": [{"status": "passed", "file": "x.py", "duration_seconds": 2}]}))
    old = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp()
    os.utime(path, (old, old))
    rows = script_timing.scan_latest_results(tmp_path)
    assert rows[0]["observed_at"].startswith("2026-09-01")


@pytest.mark.parametrize("stored", [None, [], [None], [{"id": "manifest_drift:coverage", "status": "green"}]])
def test_malformed_published_inventory_cannot_establish_coverage(stored):
    m = monitoring.assess(board(), {}, devbox={"ts": TS, "sections": {
        "manifest_drift": {"state": "ok", "monitoring_checks": stored}}},
        now=NOW, families=["manifest_drift"], repos=[])
    assert not m["complete"] and m["score"] < 100


def test_published_item_without_timestamp_cannot_inherit_envelope_time():
    item = {"id": "manifest_drift:coverage", "subject": "manifest", "status": "green", "summary": "clean"}
    m = monitoring.assess(board(), {}, devbox={"ts": TS, "sections": {
        "manifest_drift": {"monitoring_checks": [item]}}}, now=NOW, families=["manifest_drift"], repos=[])
    assert m["findings"][0]["status"] == "grey"
    assert m["findings"][0]["observed_at"] is None
