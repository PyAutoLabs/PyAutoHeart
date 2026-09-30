"""Release impact comes from readiness; local advice never invents a gate."""

import datetime
import json

from heart import dashboard, fix, publish, readiness

TS = "2026-06-01T00:00:00+00:00"
NOW = datetime.datetime(2026, 6, 1, 0, 1, tzinfo=datetime.timezone.utc)


def repo(group, **checkout):
    return {"ci_status": {"group": group, "conclusion": "success", "ts": TS},
            "repo_state": {"group": group, "branch": "main", **checkout}}


def board_for(repos, **slices):
    snapshot = {"ts": TS, "repos": repos, **slices}
    verdict = readiness.compute(snapshot, libraries=["LibA"],
                                required_workflows={"workspaces": ["Smoke Tests"]})
    return dashboard.build_board(snapshot, verdict, now=NOW), verdict


def entries(board, subject):
    return [e for section in board.sections for e in section.entries if e["subject"] == subject]


def test_only_configured_library_checkout_is_release_red():
    b, v = board_for({"LibA": repo("libraries", behind=2),
                      "LibB": repo("libraries", behind=2),
                      "WorkspaceA": repo("workspaces", behind=2)})
    a, other, ws = [next(e for e in entries(b, n) if e["key"] == "lib_behind")
                    for n in ("LibA", "LibB", "WorkspaceA")]
    assert a["state"] == "fail" and a["affects_release"]
    assert all(e["state"] == "warn" and not e["affects_release"] for e in (other, ws))
    assert v["red_reasons"] == ["LibA: 2 commit(s) behind origin"]
    blocker = next(e for e in b.blockers if e["severity"] == "red")
    assert "pull --ff-only" in blocker["prompt"] and "/bug" not in blocker["prompt"]
    assert "not a task worktree" in blocker["prompt"]
    assert blocker["run_url"] is None


def test_workflow_impact_respects_required_list_and_pending_is_advisory():
    ws = repo("workspaces")
    ws["ci_status"].update(conclusion="failure", workflows={
        "Smoke Tests": {"conclusion": "success"},
        "Optional": {"conclusion": "failure"}})
    b, _ = board_for({"WorkspaceA": ws})
    assert all(not e["affects_release"] and e["state"] != "fail" for e in entries(b, "WorkspaceA"))
    ws["ci_status"]["workflows"]["Smoke Tests"] = {
        "conclusion": "failure", "url": "https://example.invalid/smoke"}
    b, _ = board_for({"WorkspaceA": ws})
    e = next(e for e in entries(b, "WorkspaceA") if e["state"] == "fail")
    assert e["affects_release"] and e["evidence"].endswith("/smoke")
    ws["ci_status"] = {"group": "workspaces", "conclusion": "in_progress"}
    b, _ = board_for({"WorkspaceA": ws})
    assert all(e["state"] != "fail" and not e["affects_release"] for e in entries(b, "WorkspaceA"))


def test_old_pr_is_warning_not_failure_and_recent_pr_is_information():
    body = repo("libraries")
    body["open_prs"] = {"open_count": 1, "max_age_days": 40}
    b, _ = board_for({"LibA": body})
    e = next(e for e in entries(b, "LibA") if e["key"] == "open_pr")
    assert e["state"] == "warn" and e["affects_release"]
    body["open_prs"]["max_age_days"] = 2
    b, _ = board_for({"LibA": body})
    e = next(e for e in entries(b, "LibA") if e["key"] == "open_pr")
    assert e["state"] == "info" and not e["affects_release"]


def test_dirty_and_branch_remedies_are_checkout_diagnostics():
    b, _ = board_for({"LibA": repo("libraries", branch="feature/work", dirty_real=2)})
    reasons = [r for r in b.blockers if r["severity"] == "red"]
    assert len(reasons) == 2
    assert all(r["command"] == "pyauto-heart fix dirty LibA" for r in reasons)
    assert all(r["prompt"].startswith("/health") for r in reasons)


def test_fix_dirty_on_clean_wrong_branch_emits_preserving_plan(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(fix.state, "load", lambda: {"repos": {"LibA": repo("libraries", branch="work")}})
    monkeypatch.setattr(fix._workspace, "canonical_root", lambda: tmp_path)
    monkeypatch.setattr(fix._workspace, "repo_path", lambda root, name: root / "group" / name)
    assert fix.fix_dirty("LibA") == 0
    out = capsys.readouterr().out
    assert "Current branch: work" in out and "preserve this branch" in out
    assert str(tmp_path / "group" / "LibA") in out


def test_score_breakdown_reproduces_caps_and_floor():
    repos = {f"Lib{i}": repo("libraries", branch="work", dirty_real=3, behind=4) for i in range(5)}
    v = readiness.compute({"ts": TS, "repos": repos}, libraries=list(repos))
    penalties = {p["key"]: p for p in v["penalties"]}
    assert penalties["lib_behind"] == {"key": "lib_behind", "count": 5, "weight": 20, "cap": 40, "points": 40}
    assert v["score"] == max(0, 100-sum(p["points"] for p in v["penalties"])) == 0
    html = dashboard.render({"ts": TS, "repos": repos}, v, fmt="html", now=NOW)
    assert "Why this score: 0/100" in html and "floor of 0" in html
    assert "Library checkout behind origin" in html


def test_old_score_snapshot_is_explicitly_unexplained():
    html = dashboard.render({"ts": TS}, {"verdict": "green", "score": 65}, fmt="html", now=NOW)
    assert "older verdict did not record a breakdown" in html


def test_drift_categories_paths_and_publication_privacy():
    snapshot = {"ts": TS, "repos": {}, "worktree_drift": {
        "dirty": [{"worktree": f"task{i}", "repo": "LibA", "dirty_files": 1} for i in range(12)],
        "missing": [{"task": "missing", "path": "/home/user/private/missing"}],
        "orphans": [{"path": "orphan-dir"}],
        "canonical_dirty": [{"repo": "LibB", "dirty_files": 2}]}}
    verdict = {"verdict": "green", "score": 100}
    board = dashboard.build_board(snapshot, verdict, now=NOW)
    drift = next(s for s in board.sections if s.key == "worktree_drift")
    assert len(drift.entries) == 15
    assert {e["key"] for e in drift.entries} == {"dirty", "missing", "orphan", "canonical_dirty"}
    assert all(not e["affects_release"] for e in drift.entries)
    assert "/home/user/private/missing" in board.fix_plan["prompt"]
    public = publish.build_devbox_board(snapshot, verdict)
    assert "/home/" not in json.dumps(public)
    cloud = dashboard.build_board({"ts": TS}, verdict, unobserved=dashboard.LOCAL_ONLY_FAMILIES,
                                   devbox=public, now=NOW)
    assert cloud.vantage == "cloud snapshot" and "dev box" in cloud.devbox_observed
    imported = next(s for s in cloud.sections if s.key == "worktree_drift")
    assert len(imported.entries) == 14  # private entry excluded; no display cap on others
    assert all(e["source"] == "published dev-box observation" for e in imported.entries)


def test_full_text_surfaces_keep_all_tiers_but_brief_does_not():
    snapshot = {"ts": TS}
    v = {"verdict": "red", "score": 0, "repository_reasons": [],
         "red_reasons": [f"failure {i}" for i in range(10)],
         "yellow_reasons": ["a warning"], "stale_reasons": ["a gap"]}
    for fmt in ("md", "term"):
        out = dashboard.render(snapshot, v, fmt=fmt, now=NOW)
        assert all(text in out for text in ("failure 9", "a warning", "a gap"))
    brief = dashboard.render(snapshot, v, fmt="md-brief", now=NOW)
    assert "a warning" not in brief and "a gap" not in brief


def test_validation_actions_and_json_contract_remain_separate():
    b, v = board_for({}, test_run={"ready": False, "failed": 1},
                     validation_report={"release_ready": False, "stages": {"integrate": {"status": "fail"}}})
    sections = {s.key: s for s in b.sections}
    assert sections["test_run"].action["payload"].startswith("/health")
    assert sections["release_validation"].action["payload"].startswith("/release")
    payload = dashboard.to_dict(b)
    assert payload["penalties"] == v["penalties"]
    assert all("entries" not in blocker for blocker in payload["blockers"])
    assert {r["severity"] for r in payload["blockers"]} <= {"red", "yellow", "stale"}


def test_large_inventory_discloses_rest_without_losing_entries():
    rows = [{"subject": f"Repo{i}", "reason": "behind origin", "state": "warn",
             "affects_release": False} for i in range(12)]
    html = dashboard._html_entries(rows)
    assert "Show 9 more findings" in html
    assert html.count('class="entry warn"') == 12
    assert html.index("Repo2") < html.index("Show 9 more findings") < html.index("Repo3")
