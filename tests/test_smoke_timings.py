"""tests/test_smoke_timings.py — per-script CI timings: selection, parse, drift.

Fake repo/owner/entry names throughout (the tenant firewall): instance facts
live in `config/repos.yaml`, which is the declared surface, never in test data.
"""

from __future__ import annotations

import json

from heart.checks import smoke_timings as smt

REPO = "RepoA"
OWNER = "OwnerX"
RUN_URL = f"https://github.com/{OWNER}/{REPO}/actions/runs/7"
PREV_RUN_URL = f"https://github.com/{OWNER}/{REPO}/actions/runs/6"


def _artifact(name="smoke-timings-3.12", *, id_=11, created="2026-09-01T10:00:00Z",
              expired=False, run_id=7, branch="feat/x", sha="abc123"):
    """One row of the REST /actions/artifacts payload."""
    return {
        "id": id_, "name": name, "expired": expired, "created_at": created,
        "archive_download_url": "https://ci.invalid/artifacts/%s/zip" % id_,
        "workflow_run": {"id": run_id, "head_branch": branch, "head_sha": sha},
    }


def _selected(**kw):
    """The SELECTED artifact dict (what the shell leg hands build_sidecar)."""
    (picked,) = smt.select_artifacts([_artifact(**kw)])
    return picked


def _dataset(entries, *, schema="smoke_timings/1", python="3.12", profile="smoke"):
    return json.dumps({
        "schema": schema, "project": "ProjA", "directory": "imaging",
        "run_type": "scripts", "env_profile": profile, "python": python,
        "ts": "2026-09-01T09:55:00Z", "entries": entries,
        "legs": [{"project": "ProjA", "directory": "imaging"}],
    })


def _entry(entry="imaging/x.py", *, status="passed", seconds=12.0, cap=600.0,
           exit_code=0, kind="script"):
    return {"entry": entry, "kind": kind, "status": status, "seconds": seconds,
            "cap_s": cap, "exit_code": exit_code}


def _extracted(tmp_path, entries, *, sub="art", filename="smoke_timings.json", **kw):
    """An extracted-artifact directory holding one dataset file."""
    d = tmp_path / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(_dataset(entries, **kw))
    return d


def _cfg(tmp_path, factor=2.0, delta=5, top_n=5) -> str:
    cfg = tmp_path / "repos.yaml"
    cfg.write_text(
        "thresholds:\n"
        "  smoke_timings:\n"
        f"    slow_factor: {factor}\n"
        f"    min_delta_s: {delta}\n"
        f"    top_n: {top_n}\n"
    )
    return str(cfg)


# --- artifact selection: the PR gate's legs, newest, non-expired -------------

def test_selection_keeps_the_newest_artifact_per_python_leg():
    listing = {"artifacts": [
        _artifact(id_=1, created="2026-08-30T10:00:00Z", run_id=5),
        _artifact(id_=2, created="2026-09-01T10:00:00Z", run_id=7),
        _artifact("smoke-timings-3.11", id_=3, created="2026-09-01T10:00:00Z", run_id=7),
    ]}
    picked = smt.select_artifacts(listing)
    assert [a["python"] for a in picked] == ["3.11", "3.12"]
    by_py = {a["python"]: a for a in picked}
    assert by_py["3.12"]["id"] == 2 and by_py["3.12"]["run_id"] == 7
    assert by_py["3.12"]["head_branch"] == "feat/x"
    # The selector is pure: no URL host is composed here.
    assert all("run_url" not in a for a in picked)


def test_selection_skips_expired_artifacts():
    listing = {"artifacts": [
        _artifact(id_=1, created="2026-09-02T10:00:00Z", expired=True),
        _artifact(id_=2, created="2026-09-01T10:00:00Z"),
    ]}
    # The newest one's blob is gone; metadata is not data.
    assert [a["id"] for a in smt.select_artifacts(listing)] == [2]


def test_selection_ignores_the_weekly_and_foreign_artifact_names():
    """The weekly sweep is a different channel with a different cadence."""
    listing = {"artifacts": [
        _artifact("smoke-timings-scripts-ProjA-imaging", id_=1),
        _artifact("smoke-timings-notebooks-ProjA-imaging", id_=2),
        _artifact("results-ProjA-imaging", id_=3),
        _artifact("smoke-timings-3.12", id_=4),
    ]}
    assert [a["id"] for a in smt.select_artifacts(listing)] == [4]


def test_selection_accepts_a_bare_list_and_tolerates_garbage():
    assert [a["id"] for a in smt.select_artifacts([_artifact()])] == [11]
    assert smt.select_artifacts(None) == []
    assert smt.select_artifacts("nope") == []
    assert smt.select_artifacts({"artifacts": "nope"}) == []
    assert smt.select_artifacts({"artifacts": [None, "x", {}]}) == []
    assert smt.select_artifacts({"artifacts": [_artifact(id_=None)]}) == []


# --- parsing the smoke_timings/1 dataset ------------------------------------

def test_parse_rejects_a_foreign_schema_and_a_non_list_entries():
    data, err = smt.parse_timings(_dataset([_entry()], schema="smoke_timings/2"))
    assert data is None and "smoke_timings/1" in err
    data, err = smt.parse_timings(json.dumps({"schema": "smoke_timings/1",
                                              "entries": {"a": 1}}))
    assert data is None and err == "entries was not a list"
    data, err = smt.parse_timings("not json at all")
    assert data is None and "not valid JSON" in err
    data, err = smt.parse_timings("[]")
    assert data is None and err


def test_parse_normalises_entries_to_exactly_six_keys():
    data, err = smt.parse_timings(_dataset([
        dict(_entry(), extra="ignored", seconds="12.5", exit_code="1"),
    ]))
    assert err == ""
    (row,) = data["entries"]
    assert set(row) == {"entry", "kind", "status", "seconds", "cap_s", "exit_code"}
    assert row["seconds"] == 12.5 and row["exit_code"] == 1


def test_null_seconds_stay_null_and_are_never_counted_as_timed():
    """A skipped entry never ran: 0 s would be a fabricated measurement."""
    data, _ = smt.parse_timings(_dataset([
        _entry("imaging/skip.py", status="skipped", seconds=None, cap=None,
               exit_code=None),
        _entry("imaging/x.py", seconds=12.0),
    ]))
    by_entry = {e["entry"]: e for e in data["entries"]}
    assert by_entry["imaging/skip.py"]["seconds"] is None
    counts = smt.leg_counts(data["entries"])
    assert counts["timed"] == 1 and counts["skipped"] == 1
    assert counts["total_s"] == 12.0


# --- reading one extracted artifact -----------------------------------------

def test_several_dataset_files_merge_on_the_entry_path(tmp_path):
    root = tmp_path / "art"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    (root / "a" / "smoke_timings.json").write_text(
        _dataset([_entry("imaging/x.py", seconds=1.0)], profile="first"))
    (root / "b" / "smoke_timings.json").write_text(
        _dataset([_entry("imaging/x.py", seconds=99.0),
                  _entry("imaging/y.py", seconds=2.0)]))
    entries, err, meta = smt.read_downloaded_leg(root)
    assert err == ""
    assert [e["entry"] for e in entries] == ["imaging/x.py", "imaging/y.py"]
    # first file in sorted path order wins the shared path
    assert entries[0]["seconds"] == 1.0
    assert meta["env_profile"] == "first" and meta["python"] == "3.12"


def test_an_artifact_with_no_usable_dataset_is_an_honest_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert smt.read_downloaded_leg(empty) == ([], smt.NO_DATASET_ERROR, {})
    bad = _extracted(tmp_path, [_entry()], sub="bad", schema="smoke_timings/0")
    entries, err, meta = smt.read_downloaded_leg(bad)
    assert entries == [] and err == smt.NO_DATASET_ERROR and meta == {}


def test_a_dataset_with_no_entries_is_measured_not_absent(tmp_path):
    """"Observed, nothing there" must stay distinguishable from "absent"."""
    entries, err, meta = smt.read_downloaded_leg(_extracted(tmp_path, []))
    assert entries == [] and err == "" and meta["python"] == "3.12"


# --- the per-repo sidecar ----------------------------------------------------

def test_sidecar_with_a_repo_level_error_carries_no_legs():
    side = smt.build_sidecar(REPO, "workspaces", OWNER, [], "T",
                             error="gh api exited 1")
    assert side["error"] == "gh api exited 1" and side["legs"] == []


def test_sidecar_leg_carries_provenance_counts_and_the_run_url(tmp_path):
    legs = [{"artifact": _selected(), "dir": _extracted(tmp_path, [
        _entry("imaging/x.py", seconds=12.0),
        _entry("imaging/skip.py", status="skipped", seconds=None),
    ]), "error": ""}]
    (leg,) = smt.build_sidecar(REPO, "workspaces", OWNER, legs, "T")["legs"]
    assert leg["python"] == "3.12" and leg["artifact_id"] == 11
    assert leg["run_id"] == 7 and leg["run_url"] == RUN_URL
    assert leg["head_branch"] == "feat/x" and leg["head_sha"] == "abc123"
    assert leg["at"] == "2026-09-01T10:00:00Z" and leg["env_profile"] == "smoke"
    assert leg["counts"] == {"passed": 1, "failed": 0, "skipped": 1,
                             "timeout": 0, "timed": 1}
    assert leg["total_s"] == 12.0 and "total_s" not in leg["counts"]


def test_a_failed_leg_keeps_its_provenance_and_loses_no_sibling(tmp_path):
    """One leg's 403 must not cost the repo its other leg."""
    good = _selected(name="smoke-timings-3.11", id_=1, run_id=7)
    bad = _selected(name="smoke-timings-3.12", id_=2, run_id=7)
    legs = [
        {"artifact": good, "dir": _extracted(tmp_path, [_entry(seconds=3.0)]),
         "error": ""},
        {"artifact": bad, "dir": None, "error": "HTTP 403: Resource not accessible"},
    ]
    side = smt.build_sidecar(REPO, "workspaces", OWNER, legs, "T")
    by_py = {leg["python"]: leg for leg in side["legs"]}
    assert by_py["3.11"]["entries"] and by_py["3.11"]["error"] == ""
    assert by_py["3.12"]["entries"] == []
    assert "403" in by_py["3.12"]["error"]
    assert by_py["3.12"]["run_url"] == RUN_URL      # provenance survives


def test_read_downloads_pairs_artifacts_with_dirs_and_error_markers(tmp_path):
    arts = smt.select_artifacts([_artifact(id_=1), _artifact("smoke-timings-3.11", id_=2)])
    (tmp_path / "1").mkdir()
    (tmp_path / "2.error").write_text("HTTP 403: Resource not accessible\nby integration\n")
    legs = {leg["artifact"]["id"]: leg for leg in smt.read_downloads(arts, tmp_path)}
    assert legs[1]["dir"] == tmp_path / "1" and legs[1]["error"] == ""
    assert legs[2]["dir"] is None and legs[2]["error"].startswith("HTTP 403")


# --- events, drift, and the prompts they carry ------------------------------

def test_a_timeout_becomes_an_event_with_its_own_prompt(tmp_path):
    legs = [{"artifact": _selected(), "dir": _extracted(tmp_path, [
        _entry("imaging/slow.py", status="timeout", seconds=None, cap=600.0,
               exit_code=None),
    ]), "error": ""}]
    side = smt.build_sidecar(REPO, "workspaces", OWNER, legs, "T")
    roll = smt.aggregate([side], {}, "T", smt.DEFAULT_SMOKE_TIMINGS_THRESHOLDS)
    (event,) = roll["events"]
    assert event["kind"] == "timeout" and event["entry"] == "imaging/slow.py"
    assert event["run_url"] == RUN_URL and event["cap_s"] == 600.0
    assert event["prompt"] == (
        f"/bug kill timer: {REPO} imaging/slow.py TIMEOUT (600s) on {RUN_URL} "
        f"— stack tail in the run log"
    )
    # A killed entry has no duration, so it is an event and NOT a timed row.
    assert roll["rows"] == []


def test_a_timeout_without_a_cap_still_prompts():
    assert smt.timeout_prompt(REPO, "imaging/x.py", None, RUN_URL).startswith(
        f"/bug kill timer: {REPO} imaging/x.py TIMEOUT (?s) on ")


def _prev_board(rows):
    return {"performance": {"scripts": {"rows": rows}}}


def _prev_row(seconds=10.0, run_id=6, entry="imaging/x.py", python="3.12"):
    return {"repo": REPO, "python": python, "entry": entry, "seconds": seconds,
            "run_id": run_id, "run_url": PREV_RUN_URL}


def test_drift_needs_both_the_ratio_and_the_absolute_floor():
    thr = {"slow_factor": 2.0, "min_delta_s": 5}
    # 3x but only +2s — jitter on a fast script, not a regression.
    assert smt.classify_drift(3.0, _prev_row(1.0), thr, run_id=7)[0] == "ok"
    # +40s but only 1.4x — big, but that is what a long script's noise looks like.
    assert smt.classify_drift(140.0, _prev_row(100.0), thr, run_id=7)[0] == "ok"
    state, ratio, delta = smt.classify_drift(30.0, _prev_row(10.0), thr, run_id=7)
    assert (state, ratio, delta) == ("warn", 3.0, 20.0)


def test_drift_is_ok_when_either_side_is_missing():
    thr = {"slow_factor": 2.0, "min_delta_s": 5}
    assert smt.classify_drift(None, _prev_row(), thr) == ("ok", None, None)
    assert smt.classify_drift(30.0, None, thr) == ("ok", None, None)
    assert smt.classify_drift(30.0, {"seconds": None}, thr) == ("ok", None, None)


def test_a_run_is_never_compared_against_itself():
    """The previous board is re-read on every render — a second render of the
    same run must not report a reassuring 1.0x, or anything at all."""
    thr = {"slow_factor": 2.0, "min_delta_s": 5}
    assert smt.classify_drift(30.0, _prev_row(10.0, run_id=7), thr, run_id=7) == (
        "ok", None, None)


def test_prev_rows_are_keyed_by_repo_python_and_entry():
    rows = smt.prev_rows_of(_prev_board([_prev_row(), _prev_row(python="3.11")]))
    assert set(rows) == {(REPO, "3.12", "imaging/x.py"), (REPO, "3.11", "imaging/x.py")}


def test_malformed_prev_boards_mean_no_comparison():
    for board in (None, "nope", {}, {"performance": "nope"},
                  {"performance": {"scripts": "nope"}},
                  {"performance": {"scripts": {"rows": "nope"}}},
                  {"performance": {"scripts": {"rows": [None, "x", {}]}}}):
        assert smt.prev_rows_of(board) == {}


def test_aggregate_flags_a_slowed_row_with_its_prompt(tmp_path):
    legs = [{"artifact": _selected(), "dir": _extracted(tmp_path, [
        _entry("imaging/x.py", seconds=30.0)]), "error": ""}]
    side = smt.build_sidecar(REPO, "workspaces", OWNER, legs, "T")
    roll = smt.aggregate([side], _prev_board([_prev_row(10.0)]), "T",
                         {"slow_factor": 2.0, "min_delta_s": 5, "top_n": 5})
    (row,) = roll["rows"]
    assert row["state"] == "warn" and row["ratio"] == 3.0 and row["delta_s"] == 20.0
    assert row["prev_s"] == 10.0 and row["prev_run_id"] == 6
    assert row["run_id"] == 7 and row["run_url"] == RUN_URL
    assert row["prompt"] == (
        f"/bug slow script: {REPO} imaging/x.py 10s → 30s between runs "
        f"{PREV_RUN_URL} → {RUN_URL}"
    )
    assert roll["slowed"] == [row]
    assert roll["thresholds"]["slow_factor"] == 2.0


def test_aggregate_repo_row_carries_coverage_and_the_slowest_entries(tmp_path):
    legs = [{"artifact": _selected(), "dir": _extracted(tmp_path, [
        _entry("imaging/a.py", seconds=1.0),
        _entry("imaging/b.py", seconds=30.0),
        _entry("imaging/c.py", seconds=10.0),
        _entry("imaging/d.py", status="skipped", seconds=None),
    ]), "error": ""}]
    side = smt.build_sidecar(REPO, "workspaces", OWNER, legs, "T")
    roll = smt.aggregate([side], {}, "T", {"slow_factor": 2.0, "min_delta_s": 5,
                                           "top_n": 2})
    (repo_row,) = roll["repos"]
    assert repo_row["entries"] == 4 and repo_row["timed"] == 3
    assert repo_row["total_s"] == 41.0 and repo_row["python"] == "3.12"
    assert [s["entry"] for s in repo_row["slowest"]] == ["imaging/b.py", "imaging/c.py"]


def test_aggregate_errors_carry_both_repo_and_leg_level_failures(tmp_path):
    dead = smt.build_sidecar("RepoB", "workspaces", OWNER, [], "T",
                             error="gh api exited 1")
    partial = smt.build_sidecar(REPO, "workspaces", OWNER, [
        {"artifact": _selected(), "dir": None, "error": "HTTP 403"},
    ], "T")
    roll = smt.aggregate([dead, partial], {}, "T", smt.DEFAULT_SMOKE_TIMINGS_THRESHOLDS)
    assert {"repo": "RepoB", "error": "gh api exited 1"} in roll["errors"]
    assert {"repo": REPO, "error": "3.12: HTTP 403"} in roll["errors"]
    # ...and the dead repo contributes no rows that could read as "all quiet".
    assert roll["rows"] == [] and roll["repos"][0]["error"] == "HTTP 403"


def test_aggregate_ordering_is_stable(tmp_path):
    def side(repo, python, entries, art_id):
        art = _selected(name=f"smoke-timings-{python}", id_=art_id)
        return smt.build_sidecar(repo, "workspaces", OWNER, [
            {"artifact": art, "dir": _extracted(tmp_path, entries,
                                                sub=f"{repo}-{python}"),
             "error": ""}], "T")

    b = side("RepoB", "3.12", [_entry("imaging/z.py", seconds=1.0)], 1)
    a = side(REPO, "3.12", [_entry("imaging/b.py", seconds=1.0),
                            _entry("imaging/a.py", seconds=2.0)], 2)
    a11 = side(REPO, "3.11", [_entry("imaging/a.py", seconds=3.0)], 3)
    roll = smt.aggregate([b, a, a11], {}, "T", smt.DEFAULT_SMOKE_TIMINGS_THRESHOLDS)
    assert [(r["repo"], r["python"]) for r in roll["repos"]] == [
        (REPO, "3.11"), (REPO, "3.12"), ("RepoB", "3.12")]
    assert [(r["repo"], r["python"], r["entry"]) for r in roll["rows"]] == [
        (REPO, "3.11", "imaging/a.py"),
        (REPO, "3.12", "imaging/a.py"),
        (REPO, "3.12", "imaging/b.py"),
        ("RepoB", "3.12", "imaging/z.py"),
    ]


def test_aggregate_survives_garbage_sidecars():
    roll = smt.aggregate([None, "nope", {}, {"name": REPO, "legs": "nope"},
                          {"name": REPO, "legs": [None, {"entries": "nope"}]}],
                         {}, "T", smt.DEFAULT_SMOKE_TIMINGS_THRESHOLDS)
    assert roll["rows"] == [] and roll["events"] == []


# --- thresholds --------------------------------------------------------------

def test_thresholds_load_from_config(tmp_path):
    assert smt.load_thresholds(_cfg(tmp_path, 3.0, 9, 2)) == {
        "slow_factor": 3.0, "min_delta_s": 9, "top_n": 2}
    assert (smt.load_thresholds(tmp_path / "absent.yaml")
            == smt.DEFAULT_SMOKE_TIMINGS_THRESHOLDS)


def test_real_config_declares_the_smoke_timings_thresholds():
    """The shipped policy file must carry the block the check reads."""
    thr = smt.load_thresholds()
    assert thr["slow_factor"] >= 1.0 and thr["min_delta_s"] >= 1
    assert thr["top_n"] >= 1


# --- main wiring -------------------------------------------------------------

def test_plan_prints_the_ids_to_download(monkeypatch, capsys):
    listing = json.dumps({"artifacts": [
        _artifact(id_=1), _artifact("smoke-timings-3.11", id_=2),
        _artifact("smoke-timings-scripts-ProjA-imaging", id_=3),
    ]})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(listing))
    assert smt.main(["--plan"]) == 0
    assert capsys.readouterr().out.split("\n")[:2] == [
        "2 smoke-timings-3.11", "1 smoke-timings-3.12"]


def test_plan_says_nothing_on_garbage(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("not json"))
    assert smt.main(["--plan"]) == 0
    assert capsys.readouterr().out == ""


def test_main_writes_sidecar_then_aggregates(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    per_repo = tmp_path / "per-repo"
    per_repo.mkdir()
    listing = tmp_path / "listing.json"
    listing.write_text(json.dumps({"artifacts": [_artifact()]}))
    downloads = tmp_path / "downloads"
    _extracted(downloads, [_entry("imaging/x.py", seconds=30.0)], sub="11")

    out = per_repo / f"{REPO}.smoke_timings.json"
    assert smt.main(["--name", REPO, "--group", "workspaces", "--owner", OWNER,
                     "--ts", "T", "--listing", str(listing),
                     "--downloads", str(downloads), "--out", str(out)]) == 0
    side = json.loads(out.read_text())
    assert side["legs"][0]["counts"]["timed"] == 1
    assert "script(s) timed" in capsys.readouterr().out

    prev = tmp_path / "board.json"
    prev.write_text(json.dumps(_prev_board([_prev_row(10.0)])))
    roll_path = tmp_path / "smoke_timings.json"
    assert smt.main(["--aggregate", "--per-repo-dir", str(per_repo),
                     "--prev-board", str(prev), "--ts", "T",
                     "--out", str(roll_path)]) == 0
    roll = json.loads(roll_path.read_text())
    assert roll["rows"][0]["repo"] == REPO and roll["rows"][0]["state"] == "warn"
    assert "scripts timed across 1 repos" in capsys.readouterr().out


def test_main_records_a_fetch_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    out = tmp_path / f"{REPO}.smoke_timings.json"
    assert smt.main(["--name", REPO, "--group", "workspaces", "--ts", "T",
                     "--out", str(out), "--fetch-error", "gh api exited 1"]) == 0
    side = json.loads(out.read_text())
    assert side["error"] == "gh api exited 1" and side["legs"] == []
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_main_treats_an_unparseable_listing_as_a_fetch_error(tmp_path, monkeypatch,
                                                             capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    listing = tmp_path / "listing.json"
    listing.write_text("<html>404</html>")
    out = tmp_path / f"{REPO}.smoke_timings.json"
    assert smt.main(["--name", REPO, "--group", "workspaces", "--ts", "T",
                     "--listing", str(listing), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["error"] == "artifacts listing was not valid JSON"
    capsys.readouterr()


def test_summary_lines_are_honest_about_unavailability(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    dead = smt.build_sidecar(REPO, "workspaces", OWNER, [], "T", error="boom")
    assert "UNAVAILABLE" in smt.repo_summary_line(dead)
    roll = smt.aggregate([dead], {}, "T", smt.DEFAULT_SMOKE_TIMINGS_THRESHOLDS)
    assert "1 unavailable" in smt.summary_line(roll)


# --- the committed record is the previous observation; the board is fallback --
#
# The published board.json is the same artifact this render produces, so a Pages
# gap loses it. `timings/scripts/<repo>.jsonl` is the durable copy (see
# heart/timings.py) and it wins whenever it has anything.

def _record_rows(seconds, run_id=6):
    return {(REPO, "3.12", "imaging/x.py"): {
        "seconds": seconds, "run_id": run_id, "run_url": PREV_RUN_URL,
        "status": "passed", "cap_s": 600.0,
    }}


def test_aggregate_prefers_the_committed_record_over_the_previous_board(tmp_path):
    legs = [{"artifact": _selected(), "dir": _extracted(tmp_path, [
        _entry("imaging/x.py", seconds=30.0)]), "error": ""}]
    side = smt.build_sidecar(REPO, "workspaces", OWNER, legs, "T")
    roll = smt.aggregate(
        [side],
        # The board would say the script always took 30s — no drift.
        _prev_board([_prev_row(30.0)]), "T",
        {"slow_factor": 2.0, "min_delta_s": 5, "top_n": 5},
        # The record says it took 10s last run — the drift is real.
        record_prev_rows=_record_rows(10.0),
    )
    (row,) = roll["rows"]
    assert row["state"] == "warn" and row["prev_s"] == 10.0 and row["ratio"] == 3.0


def test_aggregate_falls_back_to_the_previous_board_when_the_record_is_empty(tmp_path):
    legs = [{"artifact": _selected(), "dir": _extracted(tmp_path, [
        _entry("imaging/x.py", seconds=30.0)]), "error": ""}]
    side = smt.build_sidecar(REPO, "workspaces", OWNER, legs, "T")
    for record in (None, {}):
        roll = smt.aggregate([side], _prev_board([_prev_row(10.0)]), "T",
                             {"slow_factor": 2.0, "min_delta_s": 5, "top_n": 5},
                             record_prev_rows=record)
        (row,) = roll["rows"]
        assert row["state"] == "warn" and row["prev_s"] == 10.0


def test_the_repo_rows_carry_the_provenance_the_record_stores(tmp_path):
    """head_sha + env_profile: which commit was measured, under which profile.
    The sidecar leg has held both since phase 1; the rollup now passes them on
    so `timings/scripts/<repo>.jsonl` can record them."""
    legs = [{"artifact": _selected(), "dir": _extracted(tmp_path, [
        _entry("imaging/x.py", seconds=12.0)]), "error": ""}]
    side = smt.build_sidecar(REPO, "workspaces", OWNER, legs, "T")
    assert side["legs"][0]["head_sha"] == "abc123"
    assert side["legs"][0]["env_profile"] == "smoke"
    (repo_row,) = smt.aggregate([side], {}, "T",
                                smt.DEFAULT_SMOKE_TIMINGS_THRESHOLDS)["repos"]
    assert repo_row["head_sha"] == "abc123"
    assert repo_row["env_profile"] == "smoke"
    assert repo_row["head_branch"] == "feat/x"


def test_main_aggregate_reads_the_record_directory(tmp_path, monkeypatch, capsys):
    import json as _json

    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    per_repo = tmp_path / "per-repo"
    per_repo.mkdir()
    legs = [{"artifact": _selected(), "dir": _extracted(tmp_path, [
        _entry("imaging/x.py", seconds=30.0)]), "error": ""}]
    side = smt.build_sidecar(REPO, "workspaces", OWNER, legs, "T")
    (per_repo / f"{REPO}.smoke_timings.json").write_text(_json.dumps(side))

    scripts = tmp_path / "timings" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / f"{REPO}.jsonl").write_text(_json.dumps({
        "date": "2026-09-04", "at": "", "python": "3.12", "run_id": 6,
        "run_url": PREV_RUN_URL, "head_branch": "", "head_sha": "",
        "env_profile": "smoke", "entries": {"imaging/x.py": [10.0, "passed", 600.0]},
    }) + "\n")

    out = tmp_path / "smoke_timings.json"
    assert smt.main(["--aggregate", "--per-repo-dir", str(per_repo), "--ts", "T",
                     "--record-dir", str(tmp_path / "timings"),
                     "--out", str(out)]) == 0
    (row,) = _json.loads(out.read_text())["rows"]
    assert row["state"] == "warn" and row["prev_s"] == 10.0 and row["prev_run_id"] == 6
    capsys.readouterr()
