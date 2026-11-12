"""tests/test_dev_ship.py — the development-ship gate (PyAutoHeart#340).

Human ruling, 2026-11-12: a development ship is blocked only by CI on `main`
of the repos the branch touches and of their dependants; every other reason is
shown, not blocking; evidence older than 6 h is STALE and STALE never passes;
release gating is unchanged.

The repo names here are synthetic on purpose (a three-library chain, a side
library and two workspaces), so the scoping rules are tested independently of
the live policy file; `test_readiness.py` pins the live graph.
"""

from __future__ import annotations

import datetime
import json

import pytest

from heart import readiness

CORE, MID, TOP, SIDE = "CoreLib", "MidLib", "TopLib", "SideLib"
WS, OTHER_WS = "demo_workspace", "other_workspace"
LIBS = [CORE, MID, TOP, SIDE]
# CORE <- MID <- TOP ; SIDE stands alone.
DEPS = {MID: [CORE], TOP: [CORE, MID]}
ORGAN, BUILD, DEV_WS = "ToolRepo", "BuildRepo", "dev_workspace"
GROUPS = {**{lib: "libraries" for lib in LIBS}, WS: "workspaces", OTHER_WS: "workspaces",
          ORGAN: "organism", BUILD: "build_workflow", DEV_WS: "workspaces_developer"}
# Named in the policy file but not polled (an `excluded:` entry, say).
UNIVERSE = [*GROUPS, "UnpolledLib"]
REQ_WF = {"workspaces": ["Smoke Tests"]}

TS = "2026-11-12T12:00:00+00:00"
NOW = datetime.datetime(2026, 11, 12, 13, 0, tzinfo=datetime.timezone.utc)  # 1 h later


def _lib(conclusion="success", **repo_state):
    rs = {"branch": "main", "dirty_real": 0, "behind": 0}
    rs.update(repo_state)
    return {"ci_status": {"conclusion": conclusion, "head_sha": ""}, "repo_state": rs}


def _ws(conclusion="success"):
    return {"ci_status": {"group": "workspaces", "conclusion": conclusion,
                          "workflows": {"Smoke Tests": {"conclusion": conclusion}}}}


def snapshot(**repos):
    body = {lib: _lib() for lib in LIBS}
    body[WS] = _ws()
    body[OTHER_WS] = _ws()
    body.update(repos)
    return {"ts": TS, "repos": body}


# Each advisory repo's own CI (config/repos.yaml dev_ship.ci_workflows).
CIW = {ORGAN: ["Organ Tests"], BUILD: ["Organ Tests"], DEV_WS: ["Organ Tests"]}


def dev(snap, repos, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("ci_workflows", CIW)
    return readiness.dev_ship(snap, repos, libraries=LIBS, required_workflows=REQ_WF,
                              dependencies=DEPS, groups=GROUPS, **kw)


# --- scope -----------------------------------------------------------------

def test_scope_is_touched_plus_transitive_dependants():
    s = readiness.dev_scope([CORE], DEPS, known=list(GROUPS))
    assert s["touched"] == [CORE]
    assert s["dependants"] == [MID, TOP]
    assert s["enforced"] == [CORE, MID, TOP]
    # Upstream is not a dependant.
    assert readiness.dev_scope([TOP], DEPS, known=list(GROUPS))["dependants"] == []


def test_scope_names_match_case_insensitively_and_unknown_names_are_listed():
    s = readiness.dev_scope(["corelib, Demo_Workspace", "NotARepo"], DEPS, known=list(GROUPS))
    assert s["touched"] == [CORE, WS, "NotARepo"]
    assert s["not_polled"] == ["NotARepo"]


def test_an_in_repo_red_ci_blocks():
    v = dev(snapshot(**{MID: _lib("failure")}), [MID])
    assert v["verdict"] == "red" and v["pass"] is False
    assert v["red_reasons"] == [f"{MID}: CI failure"]


def test_an_out_of_repo_red_ci_does_not_block_but_is_shown():
    v = dev(snapshot(**{SIDE: _lib("failure")}), [MID])
    assert v["verdict"] == "green" and v["pass"] is True
    assert {"severity": "red", "text": f"{SIDE}: CI failure"} in v["shown_reasons"]
    assert v["organism_verdict"] == "red"


def test_a_dependants_red_ci_blocks():
    """Touching CORE: TOP is a (transitive) dependant, so its red CI blocks."""
    v = dev(snapshot(**{TOP: _lib("failure")}), [CORE])
    assert v["verdict"] == "red"
    assert v["red_reasons"] == [f"{TOP}: CI failure"]
    assert v["scope"]["dependants"] == [MID, TOP]


def test_an_upstream_libraries_red_ci_does_not_block():
    v = dev(snapshot(**{CORE: _lib("failure")}), [TOP])
    assert v["verdict"] == "green"


def test_a_touched_workspaces_red_smoke_blocks_and_another_workspaces_does_not():
    snap = snapshot(**{WS: _ws("failure"), OTHER_WS: _ws("failure")})
    v = dev(snap, [WS])
    assert v["verdict"] == "red"
    assert v["red_reasons"] == [f"{WS}: Smoke Tests failure on main"]
    assert {"severity": "red", "text": f"{OTHER_WS}: Smoke Tests failure on main"} \
        in v["shown_reasons"]


def test_release_scope_and_checkout_reasons_never_block_a_dev_ship():
    snap = snapshot(**{MID: _lib(branch="feature/x", dirty_real=3, behind=4)})
    snap["verify_install"] = {"ready": False, "index": "testpypi",
                              "checks": [{"check": "F", "status": "FAIL"}]}
    snap["validation_report"] = {"validation_outcome": "fail",
                                 "stages": {"integrate": {"status": "fail"}}}
    v = dev(snap, [MID])
    assert v["verdict"] == "green" and v["pass"] is True
    shown = {r["text"] for r in v["shown_reasons"]}
    assert f"{MID}: on branch feature/x (not main)" in shown
    assert f"{MID}: 4 commit(s) behind origin" in shown
    assert any(t.startswith("release validation FAILED") for t in shown)
    assert any(t.startswith("install verification FAILED") for t in shown)
    # Nothing is dropped: every release reason is shown or blocking.
    full = readiness.compute(snap, libraries=LIBS, required_workflows=REQ_WF)
    assert len(v["shown_reasons"]) + len(v["reasons"]) == len(full["reasons"])


def test_a_touched_repo_heart_does_not_poll_is_stale_never_green():
    v = dev(snapshot(), ["UnpolledLib", MID], universe=UNIVERSE)
    assert v["verdict"] == "stale" and v["pass"] is False
    assert v["scope"]["not_polled"] == ["UnpolledLib"]
    assert "not polled by Heart: UnpolledLib" in v["stale_reasons"]


def test_a_name_the_policy_never_mentions_is_a_usage_error():
    with pytest.raises(ValueError, match="CoreLibb"):
        dev(snapshot(), ["CoreLibb"], universe=UNIVERSE)


def test_no_touched_repos_is_stale_never_green():
    v = dev(snapshot(), [" , "])
    assert v["verdict"] == "stale" and v["pass"] is False


# --- STALE never passes ------------------------------------------------------

def test_in_scope_ci_unreadable_is_stale_and_does_not_pass():
    lib = _lib()
    lib["ci_status"] = {"error": "gh api 502"}
    v = dev(snapshot(**{MID: lib}), [MID])
    assert v["verdict"] == "stale" and v["pass"] is False
    assert v["stale_reasons"] == [f"{MID}: CI status unavailable (gh api 502)"]


def test_in_scope_library_missing_from_the_snapshot_is_stale():
    snap = snapshot()
    del snap["repos"][TOP]
    v = dev(snap, [CORE])  # TOP is a dependant of CORE
    assert v["verdict"] == "stale" and v["pass"] is False
    assert v["stale_reasons"] == [f"{TOP}: status unknown"]


def test_in_scope_workspace_not_observed_is_stale():
    snap = snapshot()
    del snap["repos"][WS]
    v = dev(snap, [WS])
    assert v["verdict"] == "stale" and v["pass"] is False
    assert v["stale_reasons"] == [f"{WS}: CI status on main not observed"]


def test_an_organism_wide_stale_verdict_is_shown_and_an_aged_one_never_passes():
    """The old dev gate passed an organism-wide STALE. Out-of-scope gaps are
    now shown; but the feed itself going stale (age) blocks."""
    snap = snapshot()  # no validation report / install run → organism STALE gaps
    fresh = dev(snap, [MID])
    assert fresh["verdict"] == "green"
    assert any(r["severity"] == "stale" for r in fresh["shown_reasons"])
    aged = dev(snap, [MID], now=NOW + datetime.timedelta(hours=7))
    assert aged["verdict"] == "stale" and aged["pass"] is False


def test_pass_is_only_ever_green():
    """No reason the gate enforces is yellow, and a stale verdict never passes."""
    snap = snapshot()
    snap["script_timing"] = {"red_count": 2}  # a release YELLOW: shown only
    v = dev(snap, [MID], now=NOW + datetime.timedelta(hours=10))
    assert v["verdict"] == "stale" and v["pass"] is False
    assert v["yellow_reasons"] == []
    assert {"severity": "yellow", "text": "2 script timing regression(s)"} in v["shown_reasons"]


# --- the 6 h feed-age rule ----------------------------------------------------

def test_default_maximum_feed_age_is_six_hours():
    assert readiness.DEV_MAX_FEED_AGE_HOURS == 6.0


@pytest.mark.parametrize("hours, verdict", [(0.0, "green"), (5.9, "green"),
                                            (6.0, "green"), (6.1, "stale"), (30, "stale")])
def test_feed_older_than_six_hours_is_stale(hours, verdict):
    now = datetime.datetime.fromisoformat(TS) + datetime.timedelta(hours=hours)
    v = dev(snapshot(), [MID], now=now)
    assert v["verdict"] == verdict
    assert v["feed_age_hours"] == pytest.approx(hours, abs=0.01)
    assert v["max_feed_age_hours"] == 6.0
    if verdict == "stale":
        assert "over the 6 h maximum" in v["stale_reasons"][-1]
        assert v["remedy"].startswith("re-check")


def test_feed_without_a_timestamp_is_stale():
    snap = snapshot()
    del snap["ts"]
    v = dev(snap, [MID])
    assert v["verdict"] == "stale"
    assert v["feed_age_hours"] is None


def test_the_maximum_age_is_configurable():
    later = NOW + datetime.timedelta(hours=2)  # feed is 3 h old
    assert dev(snapshot(), [MID], now=later, max_age_hours=2)["verdict"] == "stale"
    assert dev(snapshot(), [MID], now=later, max_age_hours=4)["verdict"] == "green"


def test_an_in_scope_red_outranks_an_aged_feed():
    v = dev(snapshot(**{MID: _lib("failure")}), [MID],
            now=NOW + datetime.timedelta(hours=12))
    assert v["verdict"] == "red"
    assert v["stale_reasons"]  # the age is still said


# --- release gating is unchanged ---------------------------------------------

def test_the_release_verdict_is_untouched_by_the_dev_gate():
    snap = snapshot(**{SIDE: _lib("failure")})
    before = json.dumps(snap, sort_keys=True)
    release = readiness.compute(snap, libraries=LIBS, required_workflows=REQ_WF)
    v = dev(snap, [MID])
    assert json.dumps(snap, sort_keys=True) == before  # no mutation
    assert release["verdict"] == "red"
    assert v["organism_verdict"] == release["verdict"]
    assert readiness.compute(snap, libraries=LIBS, required_workflows=REQ_WF) == release


# --- CLI ---------------------------------------------------------------------

def _cli(monkeypatch, snap):
    monkeypatch.setattr(readiness.state, "load", lambda: snap)
    monkeypatch.setattr(readiness, "load_dependencies", lambda *a, **k: DEPS)
    monkeypatch.setattr(readiness, "load_repo_groups", lambda *a, **k: GROUPS)
    monkeypatch.setattr(readiness, "load_known_repo_names", lambda *a, **k: UNIVERSE)
    monkeypatch.setattr(readiness, "load_release_gate_names", lambda *a, **k: LIBS)
    monkeypatch.setattr(readiness, "load_required_workflows", lambda *a, **k: REQ_WF)


def test_cli_dev_json_reports_the_scoped_verdict(monkeypatch, capsys):
    snap = snapshot(**{SIDE: _lib("failure")})
    snap["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _cli(monkeypatch, snap)
    assert readiness.main(["--dev", "--repos", f"{MID},{WS}", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["gate"] == "dev-ship"
    assert out["verdict"] == "green" and out["pass"] is True
    assert out["scope"]["touched"] == [MID, WS]
    assert out["organism_verdict"] == "red"


def test_cli_repos_implies_dev_and_text_render_names_the_scope(monkeypatch, capsys):
    snap = snapshot(**{TOP: _lib("failure")})
    snap["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _cli(monkeypatch, snap)
    assert readiness.main(["--repos", CORE, "--no-color"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"DEV SHIP  {CORE} (+ dependants {MID}, {TOP})")
    assert "RED" in out and f"{TOP}: CI failure" in out


def test_cli_dev_without_repos_is_a_usage_error(monkeypatch):
    _cli(monkeypatch, snapshot())
    with pytest.raises(SystemExit) as exc:
        readiness.main(["--dev", "--json"])
    assert exc.value.code == 2


def test_cli_dev_refuses_a_release_profile(monkeypatch):
    _cli(monkeypatch, snapshot())
    with pytest.raises(SystemExit):
        readiness.main(["--repos", MID, "--profile", "release-ci"])


def test_cli_unscoped_call_still_prints_the_release_verdict(monkeypatch, capsys):
    release = {"verdict": "red", "red_reasons": ["x"], "reasons": ["x"]}
    monkeypatch.setattr(readiness, "load_verdict", lambda: release)
    assert readiness.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == release


# --- review findings (PyAutoHeart#340 review, 2026-11-12) --------------------

def _organ(conclusion="success", status="completed", on_head=True, rolled=None, ts=None):
    ci = {"group": "organism", "conclusion": rolled if rolled is not None else conclusion,
          "workflows": {"Organ Tests": {"conclusion": conclusion, "status": status,
                                        "on_head": on_head}}}
    if ts:
        ci["ts"] = ts
    return {"ci_status": ci}


@pytest.mark.parametrize("repo", [ORGAN, BUILD, DEV_WS])
def test_red_main_ci_on_a_touched_repo_of_any_group_blocks(repo):
    """Finding 1: compute never gates organ / build-workflow / developer-
    workspace CI, but red CI on main of a touched one is the branch's."""
    v = dev(snapshot(**{repo: _organ("failure")}), [repo])
    assert v["verdict"] == "red" and v["pass"] is False
    assert v["red_reasons"] == [f"{repo}: Organ Tests failure on main"]


def test_an_untouched_organs_red_ci_does_not_block():
    v = dev(snapshot(**{ORGAN: _organ("failure")}), [MID])
    assert v["verdict"] == "green"


def _two_runs(tests_on_head, tests_concl="failure"):
    return {"ci_status": {"group": "organism", "conclusion": "failure", "workflows": {
        "Organ Tests": {"conclusion": tests_concl, "status": "completed",
                        "on_head": tests_on_head, "created_at": "2026-11-12T09:00:00Z"},
        "Organ Lint": {"conclusion": "success", "status": "completed",
                       "on_head": True, "created_at": "2026-11-12T10:00:00Z"}}}}


def test_an_organ_judged_on_its_ci_runs_on_head():
    ciw = {ORGAN: ["Organ Tests", "Organ Lint"]}
    # A failing CI run on an older commit is not main's state ...
    assert dev(snapshot(**{ORGAN: _two_runs(False)}), [ORGAN], ci_workflows=ciw)["verdict"] == "green"
    # ... a failing CI run on the HEAD is.
    assert dev(snapshot(**{ORGAN: _two_runs(True)}), [ORGAN], ci_workflows=ciw)["verdict"] == "red"
    # With no CI run on HEAD, the newest CI run decides.
    snap = snapshot(**{ORGAN: _organ("failure", on_head=False)})
    assert dev(snap, [ORGAN])["verdict"] == "red"


def test_an_ops_workflow_never_blocks_but_is_shown():
    """Review finding: a scheduled board/sweep failing must not block a ship."""
    def organ(on_head):
        return {"ci_status": {"group": "organism", "conclusion": "failure", "workflows": {
            "Organ Tests": {"conclusion": "success", "status": "completed",
                            "on_head": on_head, "created_at": "2026-11-12T08:00:00Z"},
            "Organ Board": {"conclusion": "failure", "status": "completed",
                            "on_head": on_head, "created_at": "2026-11-12T11:00:00Z"}}}}
    on = dev(snapshot(**{ORGAN: organ(True)}), [ORGAN])
    assert on["verdict"] == "green"
    assert {"severity": "red", "text": f"{ORGAN}: Organ Board failure on main "
            "(ops workflow — not CI, not blocking)"} in on["shown_reasons"]
    # Nothing on HEAD: the newest CI run decides, not the newer ops run.
    assert dev(snapshot(**{ORGAN: organ(False)}), [ORGAN])["verdict"] == "green"


def test_an_advisory_repo_without_declared_ci_is_stale_and_empty_means_no_ci():
    snap = snapshot(**{ORGAN: _organ("success")})
    v = dev(snap, [ORGAN], ci_workflows={})
    assert v["verdict"] == "stale"
    assert v["stale_reasons"][0].startswith(f"{ORGAN}: no CI workflows declared")
    assert dev(snap, [ORGAN], ci_workflows={ORGAN: []})["verdict"] == "green"


def test_declared_ci_with_no_recorded_run_is_stale():
    snap = snapshot(**{ORGAN: _organ("success")})
    v = dev(snap, [ORGAN], ci_workflows={ORGAN: ["Other Tests"]})
    assert v["verdict"] == "stale"
    assert v["stale_reasons"] == [f"{ORGAN}: no run of its CI (Other Tests) recorded"]


def test_an_organ_with_ci_unfinished_or_unobserved_is_stale():
    v = dev(snapshot(**{ORGAN: _organ("", status="in_progress")}), [ORGAN])
    assert v["verdict"] == "stale"
    assert v["stale_reasons"] == [f"{ORGAN}: CI on main has no conclusion yet"]
    v = dev(snapshot(), [ORGAN])  # polled, but absent from the snapshot
    assert v["verdict"] == "stale"
    assert v["stale_reasons"] == [f"{ORGAN}: CI status on main not observed"]


def test_a_future_snapshot_beyond_clock_skew_is_stale_not_fresh():
    """Finding 3: a negative age used to clamp to 0 and read as fresh."""
    now = datetime.datetime.fromisoformat(TS)
    assert readiness.feed_age_hours(TS, now - datetime.timedelta(minutes=4)) == 0.0
    assert readiness.feed_age_hours(TS, now - datetime.timedelta(minutes=6)) is None
    assert dev(snapshot(), [MID], now=now - datetime.timedelta(minutes=4))["verdict"] == "green"
    v = dev(snapshot(), [MID], now=now - datetime.timedelta(hours=1))
    assert v["verdict"] == "stale" and v["feed_age_hours"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 0, 6.5, "x"])
def test_max_feed_age_must_be_finite_positive_and_at_most_six(bad):
    """Finding 4."""
    with pytest.raises(ValueError):
        readiness.check_max_age_hours(bad)
    with pytest.raises(ValueError):
        dev(snapshot(), [MID], max_age_hours=bad)


def test_max_feed_age_accepts_up_to_the_ceiling():
    assert readiness.check_max_age_hours("6") == 6.0
    assert readiness.check_max_age_hours(0.5) == 0.5


@pytest.mark.parametrize("bad", ["nan", "inf", "-2", "0", "12"])
def test_cli_rejects_a_bad_max_feed_age(monkeypatch, bad):
    _cli(monkeypatch, snapshot())
    with pytest.raises(SystemExit) as exc:
        readiness.main(["--repos", MID, "--max-feed-age-hours", bad])
    assert exc.value.code == 2


def test_cli_unknown_repo_name_is_a_usage_error(monkeypatch):
    _cli(monkeypatch, snapshot())
    with pytest.raises(SystemExit) as exc:
        readiness.main(["--repos", "MidLibb", "--json"])
    assert exc.value.code == 2


def test_in_scope_library_with_no_conclusion_is_stale():
    """Finding 5: a library mid-run (conclusion empty) used to read GREEN."""
    lib = _lib("")
    lib["ci_status"]["status"] = "in_progress"
    v = dev(snapshot(**{MID: lib}), [MID])
    assert v["verdict"] == "stale" and v["pass"] is False
    assert v["stale_reasons"] == [f"{MID}: CI on main has no conclusion yet (in_progress)"]


def test_in_scope_workspace_required_workflow_with_no_conclusion_is_stale():
    ws = _ws()
    ws["ci_status"]["workflows"]["Smoke Tests"]["conclusion"] = None
    v = dev(snapshot(**{WS: ws}), [WS])
    assert v["verdict"] == "stale"
    assert v["stale_reasons"] == [f"{WS}: Smoke Tests on main has no conclusion yet"]


def test_in_scope_evidence_is_aged_by_its_own_collection_time():
    """Finding 5: a fresh aggregation over an old sidecar is not fresh evidence."""
    old = (NOW - datetime.timedelta(hours=8)).isoformat()
    lib = _lib()
    lib["ci_status"]["ts"] = old
    v = dev(snapshot(**{MID: lib}), [MID])  # snapshot itself is 1 h old
    assert v["verdict"] == "stale"
    assert v["stale_reasons"][0].startswith(f"{MID}: CI evidence 8.0 h old")
    lib["ci_status"]["ts"] = (NOW - datetime.timedelta(hours=2)).isoformat()
    assert dev(snapshot(**{MID: lib}), [MID])["verdict"] == "green"
    # Out-of-scope aged evidence does not block.
    side = _lib()
    side["ci_status"]["ts"] = old
    assert dev(snapshot(**{SIDE: side}), [MID])["verdict"] == "green"


def test_the_cli_names_the_local_snapshot_it_judged_and_its_age(monkeypatch, capsys):
    """Finding 6: the reader must know it is the dev box's local state.json,
    not the published cloud feed the event triggers refresh."""
    snap = snapshot()
    snap["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _cli(monkeypatch, snap)
    readiness.main(["--repos", MID, "--no-color"])
    out = capsys.readouterr().out
    assert f"judged: local snapshot {readiness.state.HEART_STATE_FILE}, age 0.0 h" in out
    readiness.main(["--repos", MID, "--json"])
    j = json.loads(capsys.readouterr().out)
    assert j["source"] == f"local snapshot {readiness.state.HEART_STATE_FILE}"
    assert j["feed_age_hours"] == pytest.approx(0.0, abs=0.01)


def test_release_scope_workflows_in_an_organ_never_count_as_its_own_ci():
    """Release Integrate failing on an organ's main HEAD is the release
    question (shown in the release verdict), not the organ's own CI."""
    ci = {"group": "organism", "conclusion": "failure", "workflows": {
        "Release Integrate": {"conclusion": "failure", "status": "completed", "on_head": True,
                              "created_at": "2026-11-12T11:00:00Z"},
        "Organ Tests": {"conclusion": "success", "status": "completed", "on_head": True,
                        "created_at": "2026-11-12T10:00:00Z"}}}
    snap = snapshot(**{ORGAN: {"ci_status": ci}})
    assert dev(snap, [ORGAN], release_scope=["Release Integrate"])["verdict"] == "green"
    # Even if someone declared it as CI, release scope is still the ruling's
    # "shown, not enforced" — but declaration is explicit, so it would count.
    assert dev(snap, [ORGAN], release_scope=[],
               ci_workflows={ORGAN: ["Organ Tests", "Release Integrate"]})["verdict"] == "red"
    v = dev(snap, [ORGAN], release_scope=["Release Integrate"])
    assert not any("Release Integrate" in r["text"] for r in v["shown_reasons"])
    # With nothing on HEAD, the newest non-release run decides, not the rollup.
    for e in ci["workflows"].values():
        e["on_head"] = False
    assert dev(snap, [ORGAN], release_scope=["Release Integrate"])["verdict"] == "green"
