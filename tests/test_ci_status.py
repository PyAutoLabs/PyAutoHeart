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


# --- normalize_runs: REST payload + legacy gh shape ------------------------

def _rest_run(workflow, conclusion, status="completed", sha=HEAD,
              created="2026-06-29T00:00:00Z", event="push", branch="main"):
    """A run in the REST /actions/runs shape (snake_case).

    Mirrors the real payload: `name` is the workflow's display name and the
    commit subject lives in `display_title` — the opposite of newer `gh run
    list --json`, where `name` is the commit subject.
    """
    return {
        "name": workflow, "display_title": "some commit subject",
        "conclusion": conclusion, "status": status, "head_sha": sha,
        "head_branch": branch, "created_at": created, "event": event,
        "html_url": "u", "path": ".github/workflows/x.yml",
    }


def test_normalize_runs_reads_rest_payload():
    payload = {"workflow_runs": [_rest_run("Smoke Tests", "failure")]}
    runs = ci.normalize_runs(payload)
    assert len(runs) == 1
    # The workflow display name, not the commit subject.
    assert runs[0]["workflowName"] == "Smoke Tests"
    assert runs[0]["conclusion"] == "failure"
    assert runs[0]["headSha"] == HEAD
    assert runs[0]["url"] == "u"


def test_normalize_runs_still_accepts_legacy_gh_list():
    """The old `gh run list --json` shape must keep working unchanged."""
    runs = ci.normalize_runs([_run("Tests", "success")])
    assert runs[0]["workflowName"] == "Tests"
    assert runs[0]["conclusion"] == "success"


def test_normalize_runs_filters_other_branches():
    payload = {"workflow_runs": [
        _rest_run("Tests", "failure", branch="feature/x"),
        _rest_run("Tests", "success", branch="main"),
    ]}
    runs = ci.normalize_runs(payload)
    assert len(runs) == 1
    assert runs[0]["conclusion"] == "success"


def test_normalize_runs_handles_garbage():
    assert ci.normalize_runs(None) == []
    assert ci.normalize_runs({}) == []
    assert ci.normalize_runs({"workflow_runs": [None, "x"]}) == []


def test_build_sidecar_accepts_rest_payload(tmp_path):
    cfg = _cfg(tmp_path)
    payload = {"workflow_runs": [_rest_run("Tests", "success")]}
    side = ci.build_sidecar("PyAutoFit", "libraries", payload, HEAD, "T", config_path=cfg)
    assert side["conclusion"] == "success"


# --- fetch failure is not a pending run ------------------------------------

def test_fetch_error_is_unavailable_not_in_progress(tmp_path):
    """The regression this guards: a broken `gh` call used to be indistinguishable
    from CI genuinely still running, on every repo at once."""
    cfg = _cfg(tmp_path)
    side = ci.build_sidecar(
        "PyAutoLens", "libraries", {}, HEAD, "T", config_path=cfg,
        error="unknown flag: --branch",
    )
    assert side["status"] == "unavailable"
    assert side["status"] != "in_progress"
    assert side["conclusion"] == ""        # never green
    assert side["error"] == "unknown flag: --branch"


def test_fetch_error_never_reports_success(tmp_path):
    """Even if stale runs were somehow present, an errored fetch cannot be green."""
    cfg = _cfg(tmp_path)
    side = ci.build_sidecar(
        "PyAutoFit", "libraries", {"workflow_runs": [_rest_run("Tests", "success")]},
        HEAD, "T", config_path=cfg, error="boom",
    )
    assert side["conclusion"] != "success"
    assert side["status"] == "unavailable"


def test_no_error_key_is_empty_when_fetch_succeeds(tmp_path):
    cfg = _cfg(tmp_path)
    side = ci.build_sidecar("PyAutoFit", "libraries", [], HEAD, "T", config_path=cfg)
    assert side["error"] == ""


def test_summary_line_flags_unavailable(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    line = ci.summary_line({
        "name": "PyAutoLens", "conclusion": "", "status": "unavailable",
        "sha": "abc1234", "workflows": {}, "error": "unknown flag: --branch",
    })
    assert "UNAVAILABLE" in line
    assert "in_progress" not in line


def test_main_records_fetch_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    out = tmp_path / "PyAutoLens.ci_status.json"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    rc = ci.main(["--name", "PyAutoLens", "--group", "libraries",
                  "--head-sha", HEAD, "--ts", "T", "--out", str(out),
                  "--fetch-error", "unknown flag: --branch"])
    assert rc == 0
    side = json.loads(out.read_text())
    assert side["status"] == "unavailable"
    assert side["error"] == "unknown flag: --branch"
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_main_treats_unparseable_stdin_as_fetch_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    out = tmp_path / "PyAutoFit.ci_status.json"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("not json at all"))
    rc = ci.main(["--name", "PyAutoFit", "--group", "libraries",
                  "--head-sha", HEAD, "--ts", "T", "--out", str(out)])
    assert rc == 0
    side = json.loads(out.read_text())
    assert side["status"] == "unavailable"
    assert side["error"]
    capsys.readouterr()


def test_main_reads_real_rest_payload(tmp_path, monkeypatch, capsys):
    """End-to-end over the exact shape `gh api .../actions/runs` returns."""
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    out = tmp_path / "autolens_workspace.ci_status.json"
    payload = json.dumps({"total_count": 2, "workflow_runs": [
        _rest_run("Smoke Tests", "failure"),
        _rest_run("Navigator Check", "success"),
    ]})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload))
    rc = ci.main(["--name", "autolens_workspace", "--group", "workspaces",
                  "--head-sha", HEAD, "--ts", "T", "--out", str(out)])
    assert rc == 0
    side = json.loads(out.read_text())
    assert side["conclusion"] == "failure"
    assert side["workflow"] == "Smoke Tests"
    assert "FAILURE" in capsys.readouterr().out
