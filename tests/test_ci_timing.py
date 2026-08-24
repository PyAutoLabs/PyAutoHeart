"""tests/test_ci_timing.py — CI wall-clock timing, drift, and hang events.

Fake repo/workflow names throughout (the tenant firewall): instance facts live
in `config/repos.yaml`, which is the declared surface, never in test data.
"""

from __future__ import annotations

import json

from heart.checks import ci_timing as ct

REPO = "RepoA"
ACTIONS = "https://github.invalid/OwnerX/RepoA/actions"


def _run(
    workflow="Gate One",
    conclusion="success",
    *,
    created="2026-08-20T09:00:00Z",
    started="2026-08-20T09:00:00Z",
    updated="2026-08-20T09:10:00Z",
    event="push",
    branch="main",
    url="https://github.invalid/run/1",
    status="completed",
):
    """A run in the REST /actions/runs shape."""
    return {
        "name": workflow, "display_title": "some commit subject",
        "conclusion": conclusion, "status": status, "event": event,
        "head_branch": branch, "created_at": created, "run_started_at": started,
        "updated_at": updated, "html_url": url,
    }


def _cfg(tmp_path, extra: str = "") -> str:
    cfg = tmp_path / "repos.yaml"
    cfg.write_text(
        "required_workflows:\n"
        "  workspaces: ['Gate One', 'Gate Two']\n"
        "  libraries: ['Gate One']\n"
        "thresholds:\n"
        "  ci_timing:\n"
        "    yellow_factor: 1.5\n"
        "    min_delta_s: 120\n"
        "    history_cap: 30\n"
        + extra
    )
    return str(cfg)


# --- duration comes from run_started_at, never created_at -------------------

def test_duration_uses_run_started_at_not_created_at():
    """The re-attempt trap: a re-run keeps its ORIGINAL created_at, so
    created_at arithmetic reports multi-day durations for a ten-minute run."""
    reattempt = _run(
        created="2026-08-18T09:00:00Z",      # two days earlier — the trap
        started="2026-08-20T09:00:00Z",
        updated="2026-08-20T09:10:00Z",
    )
    assert ct.duration_s(ct.normalize_runs([reattempt])[0]) == 600.0


def test_reattempted_run_does_not_distort_the_median(tmp_path):
    payload = {"workflow_runs": [
        _run(updated="2026-08-20T09:10:00Z"),                     # 600s
        _run(created="2026-08-18T09:00:00Z",                      # re-attempt
             updated="2026-08-20T09:10:00Z", url="https://github.invalid/run/2"),
    ]}
    side = ct.build_sidecar(REPO, "libraries", "OwnerX", payload, "T",
                            config_path=_cfg(tmp_path))
    wf = side["workflows"]["Gate One"]
    assert wf["median_s"] == 600.0
    assert wf["max_s"] == 600.0          # never the multi-day created_at figure


def test_run_without_run_started_at_is_skipped():
    assert ct.duration_s({"run_started_at": "", "updated_at": "2026-08-20T09:10:00Z"}) is None


# --- medians over success runs only -----------------------------------------

def test_medians_are_success_only_and_split_by_event(tmp_path):
    payload = {"workflow_runs": [
        _run(updated="2026-08-20T09:10:00Z"),                                   # 600s push
        _run(updated="2026-08-20T09:05:00Z", event="pull_request", branch="f1"),  # 300s PR
        _run(updated="2026-08-20T09:15:00Z", event="pull_request", branch="f2"),  # 900s PR
        # A failure stops early; pooling it would make the gate look faster the
        # more it breaks.
        _run("Gate One", "failure", updated="2026-08-20T09:00:30Z"),
    ]}
    side = ct.build_sidecar(REPO, "libraries", "OwnerX", payload, "T",
                            config_path=_cfg(tmp_path))
    wf = side["workflows"]["Gate One"]
    assert wf["median_s"] == 600.0          # 300, 600, 900
    assert wf["pr_median_s"] == 600.0       # 300, 900
    assert wf["runs_counted"] == 3
    assert wf["conclusions"] == {"success": 3, "failure": 1, "cancelled": 0,
                                 "timed_out": 0}
    assert wf["actions_url"] == "https://github.com/OwnerX/RepoA/actions"


def test_queue_seconds_floor_at_zero():
    # A clock-skewed payload where the run "started" before it was created must
    # not produce a negative queue delay.
    assert ct.queue_s({"created_at": "2026-08-20T09:00:00Z",
                       "run_started_at": "2026-08-20T08:59:50Z"}) == 0.0
    assert ct.queue_s({"created_at": "2026-08-20T09:00:00Z",
                       "run_started_at": "2026-08-20T09:00:30Z"}) == 30.0


def test_queue_median_recorded(tmp_path):
    payload = {"workflow_runs": [
        _run(created="2026-08-20T08:59:00Z", started="2026-08-20T09:00:00Z",
             updated="2026-08-20T09:10:00Z"),
    ]}
    side = ct.build_sidecar(REPO, "libraries", "OwnerX", payload, "T",
                            config_path=_cfg(tmp_path))
    assert side["workflows"]["Gate One"]["queue_median_s"] == 60.0


# --- cancelled disambiguation ------------------------------------------------

def test_cancelled_pr_run_with_newer_same_branch_run_is_superseded(tmp_path):
    payload = {"workflow_runs": [
        _run("Gate One", "cancelled", event="pull_request", branch="feat/x",
             created="2026-08-20T09:00:00Z", updated="2026-08-20T09:01:00Z"),
        _run("Gate One", "success", event="pull_request", branch="feat/x",
             created="2026-08-20T09:30:00Z", started="2026-08-20T09:30:00Z",
             updated="2026-08-20T09:40:00Z"),
    ]}
    side = ct.build_sidecar(REPO, "libraries", "OwnerX", payload, "T",
                            config_path=_cfg(tmp_path))
    assert side["events"] == []                                   # benign
    assert side["workflows"]["Gate One"]["superseded"] == 1
    assert side["workflows"]["Gate One"]["conclusions"]["cancelled"] == 1


def test_cancelled_on_main_is_always_suspect(tmp_path):
    payload = {"workflow_runs": [
        _run("Gate One", "cancelled", branch="main",
             created="2026-08-20T09:00:00Z", updated="2026-08-20T09:50:00Z"),
        _run("Gate One", "success", branch="main",
             created="2026-08-20T10:00:00Z", started="2026-08-20T10:00:00Z",
             updated="2026-08-20T10:10:00Z"),
    ]}
    side = ct.build_sidecar(REPO, "libraries", "OwnerX", payload, "T",
                            config_path=_cfg(tmp_path))
    (event,) = side["events"]
    assert event["kind"] == "suspect_cancelled"
    assert side["workflows"]["Gate One"]["superseded"] == 0


def test_cancelled_pr_run_with_no_successor_is_suspect(tmp_path):
    payload = {"workflow_runs": [
        _run("Gate One", "cancelled", event="pull_request", branch="feat/y",
             created="2026-08-20T09:00:00Z", updated="2026-08-20T09:55:00Z",
             url="https://github.invalid/run/77"),
    ]}
    side = ct.build_sidecar(REPO, "libraries", "OwnerX", payload, "T",
                            config_path=_cfg(tmp_path))
    (event,) = side["events"]
    assert event["kind"] == "suspect_cancelled"
    assert event["run_url"] == "https://github.invalid/run/77"
    assert event["prompt"] == (
        "/bug kill timer: RepoA Gate One suspect_cancelled after 3300s on feat/y "
        "— https://github.invalid/run/77"
    )


def test_timed_out_is_always_an_event_with_its_prompt(tmp_path):
    payload = {"workflow_runs": [
        _run("Gate One", "timed_out", branch="main",
             started="2026-08-20T09:00:00Z", updated="2026-08-20T14:00:00Z",
             url="https://github.invalid/run/9"),
    ]}
    side = ct.build_sidecar(REPO, "libraries", "OwnerX", payload, "T",
                            config_path=_cfg(tmp_path))
    (event,) = side["events"]
    assert event["kind"] == "timed_out"
    assert event["duration_s"] == 18000.0
    assert event["head_branch"] == "main"
    assert event["at"] == "2026-08-20T09:00:00Z"
    assert event["prompt"] == (
        "/bug kill timer: RepoA Gate One timed_out after 18000s on main "
        "— https://github.invalid/run/9"
    )


# --- tracked workflow selection ---------------------------------------------

def test_extra_workflows_are_timed_beyond_the_required_set(tmp_path):
    cfg = _cfg(tmp_path,
               "performance:\n"
               "  extra_workflows:\n"
               "    - repo: RepoA\n"
               "      workflow: 'Nightly Thing'\n"
               "    - repo: RepoB\n"
               "      workflow: 'Other Thing'\n")
    assert ct.tracked_workflows("RepoA", "libraries", cfg) == ["Gate One", "Nightly Thing"]
    assert ct.tracked_workflows("RepoB", "libraries", cfg) == ["Gate One", "Other Thing"]
    assert ct.tracked_workflows("RepoA", "workspaces", cfg) == [
        "Gate One", "Gate Two", "Nightly Thing"]


def test_untracked_workflows_are_ignored(tmp_path):
    payload = {"workflow_runs": [_run("Some Other Workflow", "success")]}
    side = ct.build_sidecar(REPO, "libraries", "OwnerX", payload, "T",
                            config_path=_cfg(tmp_path))
    assert list(side["workflows"]) == ["Gate One"]
    assert side["workflows"]["Gate One"]["runs_counted"] == 0
    assert side["workflows"]["Gate One"]["median_s"] is None


# --- fetch errors ------------------------------------------------------------

def test_fetch_error_sidecar_records_the_reason_and_no_quiet(tmp_path):
    """"We could not ask" must never render as "all quiet"."""
    side = ct.build_sidecar(REPO, "libraries", "OwnerX",
                            {"workflow_runs": [_run()]}, "T",
                            config_path=_cfg(tmp_path), error="gh api exited 1")
    assert side["error"] == "gh api exited 1"
    assert side["workflows"] == {}
    assert side["events"] == []


def test_aggregate_reports_errored_repos(tmp_path):
    roll = ct.aggregate(
        [{"name": "RepoA", "error": "boom", "workflows": {}, "events": []}],
        {}, "2026-08-24", "T", ct.DEFAULT_CI_TIMING_THRESHOLDS)
    assert roll["errors"] == [{"repo": "RepoA", "error": "boom"}]
    assert roll["gates"] == []


# --- aggregate: drift needs BOTH gates ---------------------------------------

def _sidecar(median, workflow="Gate One", repo="RepoA"):
    return {
        "name": repo, "group": "libraries", "owner": "OwnerX", "error": "",
        "workflows": {workflow: {
            "median_s": median, "pr_median_s": median, "queue_median_s": 5.0,
            "max_s": median, "runs_counted": 6, "window_from": "2026-08-17T00:00:00Z",
            "conclusions": {"success": 6, "failure": 0, "cancelled": 0, "timed_out": 0},
            "superseded": 0, "actions_url": ACTIONS,
        }},
        "events": [],
    }


def _prev(history):
    return {"performance": {"history": history}}


def _hist(date, p50, key="RepoA/Gate One", runs=6):
    return {"date": date, "gates": {key: {"p50_s": p50, "runs": runs}}}


def test_drift_needs_ratio_and_absolute_delta():
    thr = {"yellow_factor": 1.5, "min_delta_s": 120, "history_cap": 30}
    # Both gates cleared: 600 -> 1200 is 2.0x and +600s.
    assert ct.classify_drift(1200.0, 600.0, thr)[0] == "warn"
    # Ratio alone (a fast gate jittering): 4.0x but only +30s.
    assert ct.classify_drift(40.0, 10.0, thr)[0] == "ok"
    # Absolute alone (a slow gate creeping): +300s but only 1.15x.
    assert ct.classify_drift(2300.0, 2000.0, thr)[0] == "ok"
    # No baseline at all → never a warn.
    assert ct.classify_drift(1200.0, None, thr)[0] == "ok"


def test_aggregate_flags_a_slowed_gate_with_its_prompt():
    prev = _prev([_hist("2026-08-22", 600.0), _hist("2026-08-23", 600.0)])
    roll = ct.aggregate([_sidecar(1200.0)], prev, "2026-08-24", "T",
                        {"yellow_factor": 1.5, "min_delta_s": 120, "history_cap": 30})
    (gate,) = roll["gates"]
    assert gate["state"] == "warn"
    assert gate["baseline_s"] == 600.0
    assert gate["prompt"] == (
        "/bug smoke gate RepoA: Gate One median wall-clock rose 600s → 1200s "
        f"vs its recent history — {ACTIONS}"
    )


def test_aggregate_ok_gate_carries_no_prompt():
    prev = _prev([_hist("2026-08-23", 600.0)])
    roll = ct.aggregate([_sidecar(620.0)], prev, "2026-08-24", "T",
                        ct.DEFAULT_CI_TIMING_THRESHOLDS)
    (gate,) = roll["gates"]
    assert gate["state"] == "ok"
    assert gate["prompt"] is None


def test_gates_are_never_fail_only_advisory():
    prev = _prev([_hist("2026-08-23", 10.0)])
    roll = ct.aggregate([_sidecar(9000.0)], prev, "2026-08-24", "T",
                        ct.DEFAULT_CI_TIMING_THRESHOLDS)
    assert {g["state"] for g in roll["gates"]} <= {"ok", "warn"}


# --- aggregate: history roll-forward -----------------------------------------

def test_history_rolls_forward_and_replaces_todays_entry():
    prev = _prev([_hist("2026-08-23", 600.0), _hist("2026-08-24", 111.0)])
    roll = ct.aggregate([_sidecar(650.0)], prev, "2026-08-24", "T",
                        ct.DEFAULT_CI_TIMING_THRESHOLDS)
    dates = [e["date"] for e in roll["history"]]
    assert dates == ["2026-08-23", "2026-08-24"]           # one entry per date
    assert roll["history"][-1]["gates"]["RepoA/Gate One"]["p50_s"] == 650.0
    # ...and the prior day is preserved verbatim.
    assert roll["history"][0]["gates"]["RepoA/Gate One"]["p50_s"] == 600.0


def test_history_re_render_is_idempotent():
    prev = _prev([_hist("2026-08-23", 600.0)])
    once = ct.aggregate([_sidecar(650.0)], prev, "2026-08-24", "T",
                        ct.DEFAULT_CI_TIMING_THRESHOLDS)
    twice = ct.aggregate([_sidecar(650.0)], {"performance": once}, "2026-08-24", "T",
                         ct.DEFAULT_CI_TIMING_THRESHOLDS)
    assert once["history"] == twice["history"]


def test_history_cap_is_honoured():
    prev = _prev([_hist(f"2026-07-{d:02d}", 600.0) for d in range(1, 31)])
    roll = ct.aggregate([_sidecar(600.0)], prev, "2026-08-24", "T",
                        {"yellow_factor": 1.5, "min_delta_s": 120, "history_cap": 5})
    assert len(roll["history"]) == 5
    assert roll["history"][-1]["date"] == "2026-08-24"


def test_missing_or_corrupt_prev_board_means_no_history():
    for prev in (None, {}, "not a board", {"performance": "nope"},
                 {"performance": {"history": "nope"}}):
        roll = ct.aggregate([_sidecar(600.0)], prev, "2026-08-24", "T",
                            ct.DEFAULT_CI_TIMING_THRESHOLDS)
        assert [e["date"] for e in roll["history"]] == ["2026-08-24"]
        assert roll["gates"][0]["state"] == "ok"     # no baseline → never a warn


def test_history_baseline_ignores_todays_entry():
    history = [_hist("2026-08-23", 600.0), _hist("2026-08-24", 5000.0)]
    assert ct.history_baseline(history, "RepoA/Gate One", "2026-08-24") == 600.0


def test_read_prev_board_degrades_on_bad_json(tmp_path):
    bad = tmp_path / "board.json"
    bad.write_text("<html>404</html>")
    assert ct.read_prev_board(bad) == {}
    assert ct.read_prev_board(tmp_path / "absent.json") == {}
    assert ct.read_prev_board("") == {}


# --- aggregate: events + summary ---------------------------------------------

def test_aggregate_concatenates_events_with_repo_context():
    side = _sidecar(600.0)
    side["events"] = [{"kind": "timed_out", "workflow": "Gate One",
                       "run_url": "u", "duration_s": 900.0, "head_branch": "main",
                       "at": "t", "prompt": "/bug kill timer: RepoA ..."}]
    roll = ct.aggregate([side], {}, "2026-08-24", "T", ct.DEFAULT_CI_TIMING_THRESHOLDS)
    assert roll["events"][0]["repo"] == "RepoA"
    assert roll["events"][0]["prompt"].startswith("/bug kill timer:")


def test_summary_line_counts_gates_slowdowns_and_events(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    prev = _prev([_hist("2026-08-23", 600.0)])
    side = _sidecar(1200.0)
    side["events"] = [{"kind": "timed_out", "workflow": "Gate One", "run_url": "u",
                       "duration_s": 1.0, "head_branch": "main", "at": "t",
                       "prompt": "p"}]
    roll = ct.aggregate([side], prev, "2026-08-24", "T",
                        ct.DEFAULT_CI_TIMING_THRESHOLDS)
    line = ct.summary_line(roll)
    assert "1 gates timed, 1 slowed, 1 hang events" in line


def test_repo_summary_line_flags_unavailable(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    line = ct.repo_summary_line({"name": "RepoA", "error": "gh api exited 1",
                                 "workflows": {}, "events": []})
    assert "UNAVAILABLE" in line


# --- normalize / robustness ---------------------------------------------------

def test_normalize_keeps_pull_request_runs_and_every_branch():
    """ci_status filters to main; this check must NOT — the PR gate is the point."""
    payload = {"workflow_runs": [
        _run(event="pull_request", branch="feat/x"),
        _run(event="push", branch="main"),
    ]}
    assert len(ct.normalize_runs(payload)) == 2


def test_normalize_handles_garbage():
    assert ct.normalize_runs(None) == []
    assert ct.normalize_runs({}) == []
    assert ct.normalize_runs({"workflow_runs": [None, "x"]}) == []


def test_thresholds_load_from_config(tmp_path):
    thr = ct.load_thresholds(_cfg(tmp_path))
    assert thr == {"yellow_factor": 1.5, "min_delta_s": 120, "history_cap": 30}
    assert ct.load_thresholds(tmp_path / "absent.yaml") == ct.DEFAULT_CI_TIMING_THRESHOLDS


def test_real_config_declares_the_ci_timing_thresholds():
    """The shipped policy file must carry the block the check reads."""
    thr = ct.load_thresholds()
    assert thr["yellow_factor"] >= 1.0 and thr["min_delta_s"] >= 1
    assert thr["history_cap"] >= 2


# --- main wiring --------------------------------------------------------------

def test_main_writes_sidecar_then_aggregates(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    per_repo = tmp_path / "per-repo"
    per_repo.mkdir()
    payload = json.dumps({"workflow_runs": [
        _run("Tests", "success", updated="2026-08-20T09:10:00Z"),
    ]})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload))
    rc = ct.main(["--name", "PyAutoFit", "--group", "libraries", "--owner", "OwnerX",
                  "--ts", "T", "--out", str(per_repo / "PyAutoFit.ci_timing.json")])
    assert rc == 0
    side = json.loads((per_repo / "PyAutoFit.ci_timing.json").read_text())
    # Uses the real config/repos.yaml: libraries gate on "Tests".
    assert side["workflows"]["Tests"]["median_s"] == 600.0

    out = tmp_path / "ci_timing.json"
    rc = ct.main(["--aggregate", "--per-repo-dir", str(per_repo), "--ts", "T",
                  "--today", "2026-08-24", "--out", str(out)])
    assert rc == 0
    roll = json.loads(out.read_text())
    assert roll["gates"][0]["repo"] == "PyAutoFit"
    assert roll["history"][-1]["date"] == "2026-08-24"
    assert "gates timed" in capsys.readouterr().out


def test_main_records_fetch_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    out = tmp_path / "RepoA.ci_timing.json"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    rc = ct.main(["--name", "RepoA", "--group", "libraries", "--ts", "T",
                  "--out", str(out), "--fetch-error", "gh api exited 1"])
    assert rc == 0
    side = json.loads(out.read_text())
    assert side["error"] == "gh api exited 1" and side["workflows"] == {}
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_main_treats_unparseable_stdin_as_fetch_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    out = tmp_path / "RepoA.ci_timing.json"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("not json at all"))
    rc = ct.main(["--name", "RepoA", "--group", "libraries", "--ts", "T",
                  "--out", str(out)])
    assert rc == 0
    assert json.loads(out.read_text())["error"]
    capsys.readouterr()
