"""tests/test_ci_status.py — per-required-workflow CI roll-up logic."""

from __future__ import annotations

import json

from heart.checks import ci_status as ci

HEAD = "a" * 40
OLD = "b" * 40


def _run(workflow, conclusion, status="completed", sha=HEAD, created="2026-06-29T00:00:00Z",
         event="push", url="u"):
    return {
        "workflowName": workflow, "name": "commit msg", "conclusion": conclusion,
        "status": status, "headSha": sha, "createdAt": created, "event": event, "url": url,
    }


# --- latest_per_workflow ---------------------------------------------------

def test_latest_per_workflow_picks_newest_per_workflow():
    runs = [
        _run("Smoke Tests", "failure", created="2026-06-01T00:00:00Z"),
        _run("Smoke Tests", "success", created="2026-06-29T00:00:00Z"),  # newer wins
        _run("Navigator Check", "success", created="2026-06-29T00:00:00Z"),
    ]
    latest = ci.latest_per_workflow(runs)
    assert set(latest) == {"Smoke Tests", "Navigator Check"}
    assert latest["Smoke Tests"]["conclusion"] == "success"


def test_latest_per_workflow_drops_pull_request_events():
    runs = [_run("Smoke Tests", "failure", event="pull_request")]
    assert ci.latest_per_workflow(runs) == {}


def test_latest_per_workflow_handles_empty_and_garbage():
    assert ci.latest_per_workflow([]) == {}
    assert ci.latest_per_workflow([None, {"event": "push"}]) == {}  # no workflowName


# --- rollup ----------------------------------------------------------------

def _wf(conclusion, status="completed", on_head=True):
    return {"conclusion": conclusion, "status": status, "on_head": on_head, "created_at": "t"}


def test_rollup_red_smoke_with_green_url_is_failure():
    """The headline gate: a red required smoke is FAILURE even if a non-required
    url-check is green — url is not in the required set so it cannot mask it."""
    workflows = {
        "Smoke Tests": _wf("failure"),
        "Navigator Check": _wf("success"),
        "url_check": _wf("success"),  # advisory, not required
    }
    out = ci.rollup(workflows, ["Smoke Tests", "Navigator Check"])
    assert out["conclusion"] == "failure"
    assert out["workflow"] == "Smoke Tests"


def test_rollup_all_required_success_on_head_is_success():
    workflows = {"Smoke Tests": _wf("success"), "Navigator Check": _wf("success")}
    assert ci.rollup(workflows, ["Smoke Tests", "Navigator Check"])["conclusion"] == "success"


def test_rollup_success_on_stale_sha_is_not_green():
    # Green conclusion but the run was not on HEAD → unknown, never success.
    workflows = {"Smoke Tests": _wf("success", on_head=False)}
    out = ci.rollup(workflows, ["Smoke Tests"])
    assert out["conclusion"] == ""
    assert out["status"] == "in_progress"


def test_rollup_in_progress_required_is_unknown():
    workflows = {"Smoke Tests": _wf(None, status="in_progress")}
    out = ci.rollup(workflows, ["Smoke Tests"])
    assert out["conclusion"] == "" and out["status"] == "in_progress"


def test_rollup_missing_required_workflow_is_unknown_not_green():
    workflows = {"Smoke Tests": _wf("success")}  # Navigator Check absent
    out = ci.rollup(workflows, ["Smoke Tests", "Navigator Check"])
    assert out["conclusion"] == ""


def test_rollup_skipped_is_not_a_failure():
    workflows = {"Smoke Tests": _wf("success"), "Navigator Check": _wf("skipped")}
    # skipped is a non-event, not a hard failure → not RED (stays unknown here,
    # because skipped != success so the all-green check fails).
    out = ci.rollup(workflows, ["Smoke Tests", "Navigator Check"])
    assert out["conclusion"] != "failure"


def test_rollup_advisory_group_reports_newest_run():
    # No required workflows → report the single newest run's conclusion.
    workflows = {
        "Build": {"conclusion": "success", "status": "completed", "created_at": "2026-06-01"},
        "Release": {"conclusion": "failure", "status": "completed", "created_at": "2026-06-29"},
    }
    assert ci.rollup(workflows, [])["conclusion"] == "failure"


def test_rollup_advisory_no_runs_is_empty():
    assert ci.rollup({}, [])["conclusion"] == ""


# --- build_sidecar ---------------------------------------------------------

def _cfg(tmp_path):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text(
        "required_workflows:\n"
        "  workspaces: ['Smoke Tests', 'Navigator Check']\n"
        "  libraries: ['Tests']\n"
    )
    return cfg


def test_build_sidecar_structures_workflows_and_rollup(tmp_path):
    cfg = _cfg(tmp_path)
    runs = [
        _run("Smoke Tests", "failure"),
        _run("Navigator Check", "success"),
    ]
    side = ci.build_sidecar("autolens_workspace", "workspaces", runs, HEAD, "T", config_path=cfg)
    assert side["conclusion"] == "failure"
    assert side["workflow"] == "Smoke Tests"
    assert side["head_sha"] == HEAD and side["sha"] == HEAD[:7]
    assert side["required"] == ["Smoke Tests", "Navigator Check"]
    assert side["workflows"]["Smoke Tests"]["conclusion"] == "failure"
    assert side["workflows"]["Smoke Tests"]["on_head"] is True
    assert side["group"] == "workspaces"


def test_build_sidecar_library_uses_tests_workflow(tmp_path):
    cfg = _cfg(tmp_path)
    # A library with a green Tests run plus an unrelated red workflow: only the
    # required Tests workflow gates, so the rollup is success.
    runs = [_run("Tests", "success"), _run("Docs Build", "failure")]
    side = ci.build_sidecar("PyAutoFit", "libraries", runs, HEAD, "T", config_path=cfg)
    assert side["conclusion"] == "success"


def test_build_sidecar_no_runs_is_empty_conclusion(tmp_path):
    cfg = _cfg(tmp_path)
    side = ci.build_sidecar("PyAutoLens", "libraries", [], "", "T", config_path=cfg)
    assert side["conclusion"] == ""
    assert side["workflows"] == {}


# --- main wiring -----------------------------------------------------------

def test_main_writes_sidecar(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    out = tmp_path / "autolens_workspace.ci_status.json"
    runs = json.dumps([_run("Smoke Tests", "failure"), _run("Navigator Check", "success")])
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(runs))
    # Uses the real config/repos.yaml (workspaces require Smoke Tests +
    # Navigator Check), so a red Smoke rolls up to FAILURE.
    rc = ci.main(["--name", "autolens_workspace", "--group", "workspaces",
                  "--head-sha", HEAD, "--ts", "T", "--out", str(out)])
    assert rc == 0
    side = json.loads(out.read_text())
    assert side["conclusion"] == "failure"
    assert "FAILURE" in capsys.readouterr().out
