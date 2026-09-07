"""tests/test_timings.py — the permanent, committed CI timing record.

The record is the *durable* copy of what the two timing checks measure: the
published board.json is the same artifact the render produces, so a Pages gap
loses it, and nothing can recompute it afterwards. These tests pin the two
things that make it worth committing — it is append-only (a line is never
rewritten by a later run) and it dedupes on IDENTITY, never on the day, so a
quiet week cannot write seven copies of one run — plus the two read views the
checks consume.

Fake repo/workflow/entry names throughout (the tenant firewall): the real ones
live in config/repos.yaml, which is the declared surface, never in test data.
"""

from __future__ import annotations

import json

from heart import timings
from heart.checks import ci_timing, smoke_timings

TS = "2026-09-05T05:03:00+00:00"
TODAY = "2026-09-05"
RUN_URL = "https://ci.invalid/OwnerX/RepoA/actions/runs/7"
GATE_URL = "https://ci.invalid/RepoA/actions"


def _ci_rollup(**kw):
    """A ci_timing.json rollup: two gates, one of them unmeasured."""
    return {
        "ts": TS,
        "gates": [
            {"repo": "RepoA", "workflow": "Gate One", "median_s": 600.0,
             "pr_median_s": 660.0, "queue_median_s": 12.0, "max_s": 900.0,
             "runs_counted": 14, "state": "ok", "actions_url": GATE_URL},
            {"repo": "RepoB", "workflow": "Gate Two", "median_s": 45.0,
             "pr_median_s": None, "queue_median_s": None, "max_s": 60.0,
             "runs_counted": 4, "state": "ok", "actions_url": ""},
            # No completed runs in the window: measured nothing, recorded as
            # nothing rather than as a row of nulls.
            {"repo": "RepoC", "workflow": "Gate Three", "median_s": None,
             "pr_median_s": None, "queue_median_s": None, "max_s": None,
             "runs_counted": 0, "state": "ok", "actions_url": ""},
        ],
        "history": [],
        "events": [],
        "errors": [],
        **kw,
    }


def _smoke_rollup(run_id=7, seconds=12.5):
    """A smoke_timings.json rollup: one repo, one python leg, two entries."""
    return {
        "ts": TS,
        "repos": [
            {"repo": "RepoA", "python": "3.12", "run_id": run_id,
             "run_url": RUN_URL, "head_branch": "feat/x", "head_sha": "abc123",
             "env_profile": "smoke", "at": "2026-09-04T10:00:00Z",
             "entries": 3, "timed": 2, "total_s": seconds + 4.0, "error": "",
             "slowest": []},
        ],
        "rows": [
            {"repo": "RepoA", "python": "3.12", "entry": "imaging/x.py",
             "kind": "script", "status": "passed", "seconds": seconds,
             "cap_s": 600.0, "exit_code": 0, "run_id": run_id,
             "run_url": RUN_URL, "state": "ok"},
            {"repo": "RepoA", "python": "3.12", "entry": "imaging/y.py",
             "kind": "script", "status": "passed", "seconds": 4.0,
             "cap_s": 600.0, "exit_code": 0, "run_id": run_id,
             "run_url": RUN_URL, "state": "ok"},
        ],
        "slowed": [],
        "events": [],
        "errors": [],
        "thresholds": {},
    }


# --- gates -------------------------------------------------------------------
def test_gates_line_records_only_the_gates_that_measured_something():
    line = timings.gates_line_from_rollup(_ci_rollup(), TODAY, TS)
    assert line["date"] == TODAY and line["ts"] == TS
    assert set(line["gates"]) == {"RepoA/Gate One", "RepoB/Gate Two"}
    assert line["gates"]["RepoA/Gate One"] == {
        "p50_s": 600.0, "pr_median_s": 660.0, "max_s": 900.0,
        "queue_median_s": 12.0, "runs": 14,
    }


def test_a_rollup_with_no_measured_gate_yields_no_line():
    """An empty line is worse than no line — it would read as a quiet day."""
    assert timings.gates_line_from_rollup({"gates": []}, TODAY, TS) is None
    assert timings.gates_line_from_rollup("not a rollup", TODAY, TS) is None


def test_the_gates_record_is_keyed_by_date(tmp_path):
    path = tmp_path / "gates.jsonl"
    line = timings.gates_line_from_rollup(_ci_rollup(), TODAY, TS)
    assert timings.append_gates(path, line) is True
    # Re-running the daily job on the same date is a no-op, not a second point
    # for that day in every gate's sparkline.
    assert timings.append_gates(path, line) is False
    later = timings.gates_line_from_rollup(_ci_rollup(), "2026-09-06", TS)
    assert timings.append_gates(path, later) is True
    records, skipped = timings.read_jsonl(path)
    assert [r["date"] for r in records] == [TODAY, "2026-09-06"]
    assert skipped == 0


def test_appending_never_rewrites_the_existing_bytes(tmp_path):
    path = tmp_path / "gates.jsonl"
    timings.append_gates(path, timings.gates_line_from_rollup(_ci_rollup(), TODAY, TS))
    before = path.read_bytes()
    timings.append_gates(path, timings.gates_line_from_rollup(_ci_rollup(), "2026-09-06", TS))
    after = path.read_bytes()
    assert after.startswith(before)          # append-only, byte for byte
    assert len(after) > len(before)


def test_every_record_is_one_newline_terminated_line_with_sorted_keys(tmp_path):
    path = tmp_path / "gates.jsonl"
    timings.append_gates(path, timings.gates_line_from_rollup(_ci_rollup(), TODAY, TS))
    text = path.read_text()
    assert text.endswith("\n")
    assert text.count("\n") == 1
    keys = list(json.loads(text).keys())
    assert keys == sorted(keys)


def test_a_missing_file_is_an_empty_record(tmp_path):
    assert timings.read_jsonl(tmp_path / "nope.jsonl") == ([], 0)
    assert timings.gates_history(tmp_path / "nope.jsonl", 30) == []
    assert timings.previous_script_rows(tmp_path / "nope") == {}


def test_an_unparseable_line_is_skipped_and_counted(tmp_path):
    """A hole in the record is a finding, not something to hide."""
    path = tmp_path / "gates.jsonl"
    path.write_text(
        '{"date": "2026-09-04", "gates": {}}\n'
        "{not json at all\n"
        "\n"
        "[1, 2, 3]\n"
        '{"date": "2026-09-05", "gates": {}}\n'
    )
    records, skipped = timings.read_jsonl(path)
    assert [r["date"] for r in records] == ["2026-09-04", "2026-09-05"]
    assert skipped == 2
    assert timings.census(tmp_path)["unparseable"] == 2


# --- the read views the checks consume ---------------------------------------
def test_gates_history_is_exactly_what_the_baseline_and_the_spark_read(tmp_path):
    from heart import dashboard

    path = tmp_path / "gates.jsonl"
    for date, p50 in (("2026-09-02", 500.0), ("2026-09-03", 600.0),
                      ("2026-09-04", 700.0), (TODAY, 1800.0)):
        timings.append_gates(path, {
            "date": date, "ts": TS,
            "gates": {"RepoA/Gate One": {"p50_s": p50, "pr_median_s": None,
                                         "max_s": None, "queue_median_s": None,
                                         "runs": 10}},
        })
    history = timings.gates_history(path, 30)
    assert [h["date"] for h in history] == ["2026-09-02", "2026-09-03",
                                            "2026-09-04", TODAY]
    # The view is the p50 + coverage only; the extra recorded figures are
    # dropped so board.json's performance.history keeps its shape.
    assert history[0]["gates"]["RepoA/Gate One"] == {"p50_s": 500.0, "runs": 10}
    # Today is excluded from its own baseline, so the median is of 500/600/700.
    assert ci_timing.history_baseline(history, "RepoA/Gate One", TODAY) == 600.0
    assert dashboard._gate_spark(history, "RepoA/Gate One") == "▁▁▂█"
    # `cap` keeps the last N, oldest first.
    assert [h["date"] for h in timings.gates_history(path, 2)] == ["2026-09-04", TODAY]


def test_scripts_lines_are_one_per_repo_python_and_run(tmp_path):
    lines = timings.scripts_lines_from_rollup(_smoke_rollup(), TODAY)
    assert list(lines) == ["RepoA"]
    (line,) = lines["RepoA"]
    assert line["date"] == TODAY
    assert line["python"] == "3.12" and line["run_id"] == 7
    assert line["run_url"] == RUN_URL and line["head_branch"] == "feat/x"
    assert line["head_sha"] == "abc123" and line["env_profile"] == "smoke"
    assert line["at"] == "2026-09-04T10:00:00Z"
    # Entries sorted by path, each the positional [seconds, status, cap_s].
    assert list(line["entries"]) == ["imaging/x.py", "imaging/y.py"]
    assert line["entries"]["imaging/x.py"] == [12.5, "passed", 600.0]


def test_a_leg_with_no_run_id_has_no_identity_and_is_not_recorded():
    rollup = _smoke_rollup()
    rollup["repos"][0]["run_id"] = None
    assert timings.scripts_lines_from_rollup(rollup, TODAY) == {}


def test_the_scripts_record_is_keyed_by_leg_and_run_never_by_the_day(tmp_path):
    """A quiet week hands the job the SAME run seven times; keying on the day
    would write seven copies of one measurement."""
    path = timings.scripts_file("RepoA", tmp_path)
    lines = timings.scripts_lines_from_rollup(_smoke_rollup(), TODAY)["RepoA"]
    assert timings.append_scripts(path, lines) == 1
    # Same run, a later day: nothing new was measured, so nothing is recorded.
    again = timings.scripts_lines_from_rollup(_smoke_rollup(), "2026-09-06")["RepoA"]
    assert timings.append_scripts(path, again) == 0
    # A NEW run of the same leg is a new measurement and does land.
    fresh = timings.scripts_lines_from_rollup(
        _smoke_rollup(run_id=8, seconds=40.0), "2026-09-06")["RepoA"]
    assert timings.append_scripts(path, fresh) == 1
    records, _ = timings.read_jsonl(path)
    assert [(r["run_id"], r["date"]) for r in records] == [(7, TODAY), (8, "2026-09-06")]


def test_previous_script_rows_takes_the_latest_line_per_leg_and_drives_drift(tmp_path):
    scripts_dir = tmp_path / "scripts"
    path = timings.scripts_file("RepoA", tmp_path)
    for run_id, seconds in ((7, 12.5), (8, 40.0)):
        timings.append_scripts(path, timings.scripts_lines_from_rollup(
            _smoke_rollup(run_id=run_id, seconds=seconds), TODAY)["RepoA"])
    # A second python leg, so "latest per leg" is not "latest in the file".
    other = timings.scripts_lines_from_rollup(_smoke_rollup(run_id=9, seconds=5.0), TODAY)
    other["RepoA"][0]["python"] = "3.11"
    timings.append_scripts(path, other["RepoA"])

    prev = timings.previous_script_rows(scripts_dir)
    assert prev[("RepoA", "3.12", "imaging/x.py")] == {
        "seconds": 40.0, "run_id": 8, "run_url": RUN_URL,
        "status": "passed", "cap_s": 600.0, "cache_jax": "unknown",
    }
    assert prev[("RepoA", "3.11", "imaging/x.py")]["seconds"] == 5.0

    thr = {"slow_factor": 2.0, "min_delta_s": 5}
    row = prev[("RepoA", "3.12", "imaging/x.py")]
    assert smoke_timings.classify_drift(120.0, row, thr, run_id=9)[0] == "warn"
    assert smoke_timings.classify_drift(41.0, row, thr, run_id=9)[0] == "ok"
    # ...and a row from the SAME run is never compared against itself.
    assert smoke_timings.classify_drift(120.0, row, thr, run_id=8) == ("ok", None, None)


def test_the_census_counts_days_observations_and_repos(tmp_path):
    assert timings.census(tmp_path) == dict(timings.EMPTY_CENSUS)
    timings.append_gates(tmp_path / "gates.jsonl",
                         timings.gates_line_from_rollup(_ci_rollup(), "2026-09-04", TS))
    timings.append_gates(tmp_path / "gates.jsonl",
                         timings.gates_line_from_rollup(_ci_rollup(), TODAY, TS))
    for repo in ("RepoA", "RepoB"):
        lines = timings.scripts_lines_from_rollup(_smoke_rollup(), TODAY)["RepoA"]
        for line in lines:
            line["repo"] = repo
        timings.append_scripts(timings.scripts_file(repo, tmp_path), lines)
    # The two unit_* keys arrived with the unit-timings slice (#206): the
    # census is the record's key set, and the record now holds three slices.
    # The two unit_* keys arrived with #206; `epochs`/`epoch` with #208 — the
    # census is the record's key set, and the record now holds the boundaries
    # every reader compares inside of as well as the three observation slices.
    assert timings.census(tmp_path) == {
        "gates_days": 2, "gates_first": "2026-09-04", "gates_last": TODAY,
        "scripts_observations": 2, "repos": 2,
        "unit_observations": 0, "unit_repos": 0, "unparseable": 0,
        "epochs": 0, "epoch": None,
    }


# --- the CLI -----------------------------------------------------------------
def _write_rollups(tmp_path, *, run_id=7):
    ci = tmp_path / "ci_timing.json"
    smoke = tmp_path / "smoke_timings.json"
    ci.write_text(json.dumps(_ci_rollup()))
    smoke.write_text(json.dumps(_smoke_rollup(run_id=run_id)))
    return ci, smoke


def test_main_append_writes_both_files_the_census_and_one_summary_line(tmp_path, capsys):
    ci, smoke = _write_rollups(tmp_path)
    record = tmp_path / "timings"
    census_out = tmp_path / "timings_record.json"
    argv = ["append", "--ci-timing", str(ci), "--smoke-timings", str(smoke),
            "--today", TODAY, "--ts", TS, "--dir", str(record),
            "--census-out", str(census_out)]

    assert timings.main(argv) == 0
    out = capsys.readouterr().out.strip()
    assert out == ("timings: gates +1 line, scripts +1 lines across 1 repos, "
                   "0 skipped (already recorded)")
    assert (record / "gates.jsonl").is_file()
    assert timings.scripts_file("RepoA", record).is_file()

    census = json.loads(census_out.read_text())
    assert census["gates_days"] == 1 and census["scripts_observations"] == 1
    assert census["gates_first"] == TODAY and census["repos"] == 1
    assert census["appended_today"] == {"gates": True, "scripts": {"RepoA": 1}}

    # A second run over the SAME inputs appends nothing at all.
    assert timings.main(argv) == 0
    out = capsys.readouterr().out.strip()
    assert out == ("timings: gates +0 line, scripts +0 lines across 0 repos, "
                   "2 skipped (already recorded)")
    assert len((record / "gates.jsonl").read_text().splitlines()) == 1
    assert len(timings.scripts_file("RepoA", record).read_text().splitlines()) == 1
    assert json.loads(census_out.read_text())["appended_today"] == {
        "gates": False, "scripts": {},
    }


def test_main_append_survives_a_missing_rollup(tmp_path, capsys):
    """A slice that produced no file means nothing to append from it — never an
    error, and never a lost append from the OTHER slice."""
    _, smoke = _write_rollups(tmp_path)
    record = tmp_path / "timings"
    assert timings.main([
        "append", "--ci-timing", str(tmp_path / "gone.json"),
        "--smoke-timings", str(smoke), "--today", TODAY, "--dir", str(record),
        "--census-out", str(tmp_path / "census.json"),
    ]) == 0
    assert "gates +0 line" in capsys.readouterr().out
    assert not (record / "gates.jsonl").exists()
    assert timings.scripts_file("RepoA", record).is_file()


def test_main_show_prints_the_census_one_key_per_line(tmp_path, capsys):
    ci, smoke = _write_rollups(tmp_path)
    record = tmp_path / "timings"
    timings.main(["append", "--ci-timing", str(ci), "--smoke-timings", str(smoke),
                  "--today", TODAY, "--dir", str(record),
                  "--census-out", str(tmp_path / "census.json")])
    capsys.readouterr()
    assert timings.main(["show", "--dir", str(record)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "gates_days: 1"
    assert "scripts_observations: 1" in lines
    assert {ln.split(":")[0] for ln in lines} == set(timings.EMPTY_CENSUS)


def test_the_daily_append_never_writes_the_epoch_file(tmp_path):
    """`epochs.jsonl` is a human's judgement about the world, not an
    observation — so the daily job's verb never creates it and never adds to
    it. (#208: the file is live now; before it, this test asserted the stronger
    "and it does not exist" — the half that survives is the load-bearing one,
    that the writer of observations is not the writer of boundaries.)"""
    assert timings.EPOCHS_FILE.name == "epochs.jsonl"
    ci, smoke = _write_rollups(tmp_path)
    record = tmp_path / "timings"
    timings.main(["append", "--ci-timing", str(ci), "--smoke-timings", str(smoke),
                  "--today", TODAY, "--dir", str(record),
                  "--census-out", str(tmp_path / "census.json")])
    assert not (record / "epochs.jsonl").exists()


# --- the unit slice: per-test durations + the cold import (#206) --------------
#
# Same two rules as the scripts record — append-only, keyed on (python, run_id)
# — over the libraries' own CI instead of the workspaces'. What is new here is
# WHAT is recorded: only the slowest N tests plus the suite totals (a 1500-test
# suite recorded whole would multiply the file for rows nothing reads), and the
# fresh-process import seconds, `null` when that import never succeeded.

PACKAGE = "pkg_a"
NODEID = "tests/foo/test_bar.py::test_x"


def _unit_rollup(run_id=7, seconds=1.5, import_s=3.6, python="3.12"):
    """A unit_timings.json rollup: one repo, one python leg, two slow tests."""
    return {
        "ts": TS,
        "repos": [
            {"repo": "RepoA", "python": python, "run_id": run_id,
             "run_url": RUN_URL, "head_branch": "feat/x", "head_sha": "abc123",
             "at": "2026-09-04T10:00:00Z", "tests": 1500, "wall_s": 412.0,
             "import_s": import_s, "package": PACKAGE, "error": "",
             "suite": {"tests": 1500, "failures": 0, "errors": 0, "skipped": 3,
                       "wall_s": 412.0},
             "slowest": [{"nodeid": NODEID, "seconds": seconds},
                         {"nodeid": "tests/foo/test_bar.py::test_y",
                          "seconds": 0.5}]},
        ],
        "tests": [], "imports": [], "slowed_tests": [], "slowed_imports": [],
        "errors": [], "thresholds": {},
    }


def test_unit_lines_are_one_per_repo_python_and_run():
    lines = timings.unit_lines_from_rollup(_unit_rollup(), TODAY)
    assert list(lines) == ["RepoA"]
    (line,) = lines["RepoA"]
    assert line["date"] == TODAY and line["python"] == "3.12"
    assert line["run_id"] == 7 and line["run_url"] == RUN_URL
    assert line["head_branch"] == "feat/x" and line["head_sha"] == "abc123"
    assert line["at"] == "2026-09-04T10:00:00Z"
    assert line["package"] == PACKAGE and line["import_s"] == 3.6
    assert line["suite"] == {"tests": 1500, "failures": 0, "errors": 0,
                             "skipped": 3, "wall_s": 412.0}
    # Only the slowest tests, node id -> seconds, sorted by node id.
    assert line["slowest"] == {NODEID: 1.5,
                               "tests/foo/test_bar.py::test_y": 0.5}


def test_a_unit_leg_with_no_run_id_has_no_identity_and_is_not_recorded():
    rollup = _unit_rollup()
    rollup["repos"][0]["run_id"] = None
    assert timings.unit_lines_from_rollup(rollup, TODAY) == {}


def test_a_failed_import_is_recorded_as_null_never_as_zero():
    (line,) = timings.unit_lines_from_rollup(
        _unit_rollup(import_s=None), TODAY)["RepoA"]
    assert line["import_s"] is None


def test_the_unit_record_is_keyed_by_leg_and_run_never_by_the_day(tmp_path):
    path = timings.unit_file("RepoA", tmp_path)
    lines = timings.unit_lines_from_rollup(_unit_rollup(), TODAY)["RepoA"]
    assert timings.append_unit(path, lines) == 1
    # Same run, a later day: nothing new was measured, so nothing is recorded.
    again = timings.unit_lines_from_rollup(_unit_rollup(), "2026-09-06")["RepoA"]
    assert timings.append_unit(path, again) == 0
    fresh = timings.unit_lines_from_rollup(
        _unit_rollup(run_id=8, seconds=9.0), "2026-09-06")["RepoA"]
    assert timings.append_unit(path, fresh) == 1
    records, _ = timings.read_jsonl(path)
    assert [(r["run_id"], r["date"]) for r in records] == [(7, TODAY),
                                                           (8, "2026-09-06")]
    # Append-only, byte for byte.
    assert path.read_text().count("\n") == 2


def test_previous_unit_rows_takes_the_latest_line_per_leg_and_drives_drift(tmp_path):
    unit_dir = tmp_path / "unit"
    path = timings.unit_file("RepoA", tmp_path)
    for run_id, seconds in ((7, 1.5), (8, 4.0)):
        timings.append_unit(path, timings.unit_lines_from_rollup(
            _unit_rollup(run_id=run_id, seconds=seconds), TODAY)["RepoA"])
    # A second python leg, so "latest per leg" is not "latest in the file".
    timings.append_unit(path, timings.unit_lines_from_rollup(
        _unit_rollup(run_id=9, seconds=0.5, python="3.13"), TODAY)["RepoA"])

    prev = timings.previous_unit_rows(unit_dir)
    assert prev[("RepoA", "3.12", NODEID)] == {
        "seconds": 4.0, "run_id": 8, "run_url": RUN_URL,
        # A rollup with no cache block: unknown, and it compares as it always
        # did (below).
        "cache_state": "unknown"}
    assert prev[("RepoA", "3.13", NODEID)]["seconds"] == 0.5

    thr = {"slow_factor": 2.0, "min_delta_s": 2}
    row = prev[("RepoA", "3.12", NODEID)]
    assert smoke_timings.classify_drift(12.0, row, thr, run_id=9)[0] == "warn"
    assert smoke_timings.classify_drift(5.0, row, thr, run_id=9)[0] == "ok"
    # ...and a row from the SAME run is never compared against itself.
    assert smoke_timings.classify_drift(12.0, row, thr, run_id=8) == (
        "ok", None, None)


def test_import_history_is_oldest_first_window_capped_and_skips_nulls(tmp_path):
    unit_dir = tmp_path / "unit"
    path = timings.unit_file("RepoA", tmp_path)
    for run_id, import_s in ((1, 1.0), (2, None), (3, 2.0), (4, 3.0), (5, 4.0)):
        timings.append_unit(path, timings.unit_lines_from_rollup(
            _unit_rollup(run_id=run_id, import_s=import_s), TODAY)["RepoA"])
    key = ("RepoA", PACKAGE, "3.12")
    # A null was never a measurement: carrying it as 0.0 would drag the median
    # toward a number nothing ever observed.
    assert timings.import_history(unit_dir, 7)[key] == [1.0, 2.0, 3.0, 4.0]
    assert timings.import_history(unit_dir, 2)[key] == [3.0, 4.0]
    assert timings.import_history(tmp_path / "nope", 7) == {}


def test_the_census_counts_the_unit_slice_too(tmp_path):
    assert timings.census(tmp_path)["unit_observations"] == 0
    for repo in ("RepoA", "RepoB"):
        lines = timings.unit_lines_from_rollup(_unit_rollup(), TODAY)["RepoA"]
        timings.append_unit(timings.unit_file(repo, tmp_path), lines)
    census = timings.census(tmp_path)
    assert census["unit_observations"] == 2 and census["unit_repos"] == 2
    # ...and the scripts figures stay their own.
    assert census["scripts_observations"] == 0 and census["repos"] == 0


def test_main_append_records_the_unit_slice_and_is_idempotent(tmp_path, capsys):
    ci, smoke = _write_rollups(tmp_path)
    unit = tmp_path / "unit_timings.json"
    unit.write_text(json.dumps(_unit_rollup()))
    record = tmp_path / "timings"
    census_out = tmp_path / "census.json"
    argv = ["append", "--ci-timing", str(ci), "--smoke-timings", str(smoke),
            "--unit-timings", str(unit), "--today", TODAY, "--ts", TS,
            "--dir", str(record), "--census-out", str(census_out)]

    assert timings.main(argv) == 0
    assert capsys.readouterr().out.strip() == (
        "timings: gates +1 line, scripts +1 lines across 1 repos, "
        "unit +1 lines across 1 repos, 0 skipped (already recorded)")
    assert timings.unit_file("RepoA", record).is_file()
    census = json.loads(census_out.read_text())
    assert census["unit_observations"] == 1 and census["unit_repos"] == 1
    assert census["appended_today"]["unit"] == {"RepoA": 1}

    # A second run over the SAME inputs appends nothing at all.
    assert timings.main(argv) == 0
    assert capsys.readouterr().out.strip() == (
        "timings: gates +0 line, scripts +0 lines across 0 repos, "
        "unit +0 lines across 0 repos, 3 skipped (already recorded)")
    assert len(timings.unit_file("RepoA", record).read_text().splitlines()) == 1
    assert json.loads(census_out.read_text())["appended_today"]["unit"] == {}


def test_a_run_without_the_unit_flag_reads_exactly_as_it_always_did(tmp_path,
                                                                    capsys):
    """The slice reports itself only when it was asked for, so an older
    invocation's summary line and census payload are unchanged."""
    ci, smoke = _write_rollups(tmp_path)
    census_out = tmp_path / "census.json"
    assert timings.main([
        "append", "--ci-timing", str(ci), "--smoke-timings", str(smoke),
        "--today", TODAY, "--dir", str(tmp_path / "timings"),
        "--census-out", str(census_out),
    ]) == 0
    assert "unit +" not in capsys.readouterr().out
    assert "unit" not in json.loads(census_out.read_text())["appended_today"]


# --- epochs: where the world changed (#208) ----------------------------------
#
# A boundary is the one line in this record a HUMAN writes, in a PR, because it
# is a judgement about the world (a rebuild, a runner change) and not something
# a daily job can observe. What it buys is the thing these tests pin: every
# reader compares INSIDE the current epoch, so a number measured before the
# world changed never stands in as the baseline for a run measured after it.

EPOCH_NOTE = "the reference round; comparisons must not cross this boundary"


def _epochs_file(tmp_path, *lines):
    """Write an epochs.jsonl by hand — the readers must cope with any order."""
    path = tmp_path / "epochs.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def test_read_epochs_sorts_normalises_and_drops_the_half_boundaries(tmp_path):
    path = _epochs_file(
        tmp_path,
        {"date": "2026-09-05", "label": "legacy", "note": EPOCH_NOTE},
        {"date": "2026-07-01", "label": "runners"},
        # Neither of these is a boundary: one cannot be compared against, the
        # other cannot be named on a board row.
        {"date": "2026-08-01"},
        {"label": "nameless"},
    )
    assert timings.read_epochs(path) == [
        {"date": "2026-07-01", "label": "runners", "note": ""},
        {"date": "2026-09-05", "label": "legacy", "note": EPOCH_NOTE},
    ]
    # A missing file is an empty record, never an error.
    assert timings.read_epochs(tmp_path / "nope.jsonl") == []


def test_two_boundaries_on_one_day_keep_the_order_they_landed(tmp_path):
    path = _epochs_file(tmp_path,
                        {"date": "2026-09-05", "label": "first"},
                        {"date": "2026-09-05", "label": "second"})
    assert [e["label"] for e in timings.read_epochs(path)] == ["first", "second"]


def test_current_epoch_is_the_latest_boundary_on_or_before_today():
    epochs = [
        {"date": "2026-07-01", "label": "runners", "note": ""},
        {"date": "2026-09-05", "label": "legacy", "note": EPOCH_NOTE},
        # Written ahead of the change it labels: not in force yet, so it cannot
        # blind every baseline the day its PR lands.
        {"date": "2026-12-01", "label": "fast-tests", "note": ""},
    ]
    assert timings.current_epoch(epochs, TODAY)["label"] == "legacy"
    assert timings.current_epoch(epochs, "2026-08-31")["label"] == "runners"
    assert timings.current_epoch(epochs, "2026-06-30") is None
    # No boundaries at all is one unbroken epoch, which is the old behaviour.
    assert timings.current_epoch([], TODAY) is None


def test_append_epoch_dedupes_on_date_and_label_and_never_rewrites(tmp_path):
    path = tmp_path / "epochs.jsonl"
    assert timings.append_epoch(path, TODAY, "legacy", EPOCH_NOTE) is True
    before = path.read_bytes()
    # The same judgement recorded twice is a no-op, not a second boundary.
    assert timings.append_epoch(path, TODAY, "legacy", "a different note") is False
    assert path.read_bytes() == before
    # A different label on the same day IS a different judgement.
    assert timings.append_epoch(path, TODAY, "runners", "") is True
    after = path.read_bytes()
    assert after.startswith(before) and len(after) > len(before)
    assert [e["label"] for e in timings.read_epochs(path)] == ["legacy", "runners"]


def test_append_epoch_refuses_a_bad_date_or_an_empty_label(tmp_path):
    """The record sorts on these dates as strings, so `YYYY-MM-DD` and nothing
    else; a label-less boundary could not be named on a board row."""
    path = tmp_path / "epochs.jsonl"
    for date in ("05-09-2026", "2026-9-5", "20260905", "tomorrow", ""):
        assert timings.append_epoch(path, date, "legacy", "") is False
    for label in ("", "   "):
        assert timings.append_epoch(path, TODAY, label, "") is False
    assert not path.exists()


# --- every reader compares inside the current epoch ---------------------------
def _dated_gates(date, p50):
    return {"date": date, "ts": TS,
            "gates": {"RepoA/Gate One": {"p50_s": p50, "runs": 5}}}


def test_gates_history_drops_the_lines_before_the_boundary(tmp_path):
    path = tmp_path / "gates.jsonl"
    for line in (_dated_gates("2026-09-03", 100.0),
                 _dated_gates("2026-09-05", 900.0)):
        timings.append_gates(path, line)
    assert [h["date"] for h in timings.gates_history(path, 30)] == [
        "2026-09-03", "2026-09-05"]
    # A gate measured before the world changed is not a baseline for one
    # measured after it.
    assert [h["date"] for h in timings.gates_history(path, 30, since=TODAY)] == [
        "2026-09-05"]
    assert timings.gates_history(path, 30, since="2026-12-01") == []


def _scripts_line(date, run_id, seconds):
    return {"date": date, "at": "", "python": "3.12", "run_id": run_id,
            "run_url": RUN_URL, "head_branch": "", "head_sha": "",
            "env_profile": "smoke",
            "entries": {"imaging/x.py": [seconds, "passed", 600.0]}}


def test_previous_script_rows_filters_before_it_picks_the_latest(tmp_path):
    """The order matters: a pre-boundary line is not a stale baseline to be
    superseded, it is a measurement of a different world — so it must not win
    the "latest per leg" race either."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    path = scripts / "RepoA.jsonl"
    timings._append(path, [_scripts_line("2026-09-04", 6, 10.0),
                           _scripts_line("2026-09-05", 7, 30.0)])
    key = ("RepoA", "3.12", "imaging/x.py")
    assert timings.previous_script_rows(scripts)[key]["seconds"] == 30.0
    assert timings.previous_script_rows(scripts, since=TODAY)[key]["run_id"] == 7
    # With only the pre-boundary line inside reach, there is no baseline at all
    # — which is honest: nothing comparable was recorded.
    only_old = tmp_path / "old"
    only_old.mkdir()
    timings._append(only_old / "RepoA.jsonl", [_scripts_line("2026-09-04", 6, 10.0)])
    assert timings.previous_script_rows(only_old, since=TODAY) == {}
    assert timings.previous_script_rows(only_old)[key]["seconds"] == 10.0


def test_an_undated_line_is_older_than_any_boundary(tmp_path):
    """The record cannot place it after the world changed, and a baseline that
    MIGHT predate the change is not a baseline. With no boundary in force it is
    read exactly as it always was."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    undated = _scripts_line("", 6, 10.0)
    undated.pop("date")
    timings._append(scripts / "RepoA.jsonl", [undated])
    key = ("RepoA", "3.12", "imaging/x.py")
    assert timings.previous_script_rows(scripts)[key]["seconds"] == 10.0
    assert timings.previous_script_rows(scripts, since=TODAY) == {}


def _unit_line(date, run_id, seconds, import_s):
    return {"date": date, "at": "", "python": "3.12", "run_id": run_id,
            "run_url": RUN_URL, "head_branch": "", "head_sha": "",
            "package": PACKAGE, "import_s": import_s,
            "suite": {"tests": 2, "failures": 0, "errors": 0, "skipped": 0,
                      "wall_s": 40.0},
            "slowest": {NODEID: seconds}}


def test_previous_unit_rows_and_import_history_honour_the_boundary(tmp_path):
    unit = tmp_path / "unit"
    unit.mkdir()
    timings._append(unit / "RepoA.jsonl", [
        _unit_line("2026-09-03", 5, 1.0, 1.0),
        _unit_line("2026-09-04", 6, 2.0, 2.0),
        _unit_line("2026-09-05", 7, 9.0, 9.0),
    ])
    key = ("RepoA", "3.12", NODEID)
    assert timings.previous_unit_rows(unit)[key]["run_id"] == 7
    assert timings.previous_unit_rows(unit, since=TODAY)[key]["run_id"] == 7

    hist_key = ("RepoA", PACKAGE, "3.12")
    assert timings.import_history(unit, 7)[hist_key] == [1.0, 2.0, 9.0]
    # A window reaching back across a boundary would take the median of two
    # different worlds — so the boundary is applied BEFORE the window.
    assert timings.import_history(unit, 7, since=TODAY)[hist_key] == [9.0]
    assert timings.import_history(unit, 7, since="2026-09-04")[hist_key] == [2.0, 9.0]


def test_an_empty_since_reads_byte_identically_to_no_since_at_all(tmp_path):
    """The whole record predates epochs; with no boundary in force every reader
    must answer exactly what it always answered."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    timings._append(scripts / "RepoA.jsonl", [_scripts_line("2026-09-04", 6, 10.0)])
    unit = tmp_path / "unit"
    unit.mkdir()
    timings._append(unit / "RepoA.jsonl", [_unit_line("2026-09-04", 6, 2.0, 2.0)])
    gates = tmp_path / "gates.jsonl"
    timings.append_gates(gates, _dated_gates("2026-09-04", 100.0))

    assert timings.gates_history(gates, 30, since="") == timings.gates_history(gates, 30)
    assert (timings.previous_script_rows(scripts, since="")
            == timings.previous_script_rows(scripts))
    assert (timings.previous_unit_rows(unit, since="")
            == timings.previous_unit_rows(unit))
    assert timings.import_history(unit, 7, since="") == timings.import_history(unit, 7)


# --- the census and the CLI ---------------------------------------------------
def test_the_census_carries_the_boundaries_and_the_one_in_force(tmp_path):
    empty = timings.census(tmp_path)
    assert empty["epochs"] == 0 and empty["epoch"] is None
    _epochs_file(tmp_path,
                 {"date": "2026-07-01", "label": "runners", "note": ""},
                 {"date": "2026-09-05", "label": "legacy", "note": EPOCH_NOTE},
                 {"date": "2099-01-01", "label": "not yet", "note": ""})
    census = timings.census(tmp_path)
    assert census["epochs"] == 3
    # The note is the human's reasoning and belongs in the file, not on a row.
    assert census["epoch"] == {"date": "2026-09-05", "label": "legacy"}


def test_main_epoch_appends_once_and_says_so_on_the_repeat(tmp_path, capsys):
    record = tmp_path / "timings"
    argv = ["epoch", "--date", TODAY, "--label", "legacy", "--note", EPOCH_NOTE,
            "--dir", str(record)]
    assert timings.main(argv) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {
        "date": TODAY, "label": "legacy", "note": EPOCH_NOTE}
    before = (record / "epochs.jsonl").read_bytes()

    assert timings.main(argv) == 0
    assert capsys.readouterr().out.strip() == f"epoch already recorded: {TODAY} legacy"
    assert (record / "epochs.jsonl").read_bytes() == before


def test_main_epoch_exits_2_on_a_boundary_it_cannot_record(tmp_path, capsys):
    record = tmp_path / "timings"
    assert timings.main(["epoch", "--date", "05-09-2026", "--label", "legacy",
                         "--dir", str(record)]) == 2
    assert timings.main(["epoch", "--date", TODAY, "--label", "  ",
                         "--dir", str(record)]) == 2
    capsys.readouterr()
    assert not (record / "epochs.jsonl").exists()


def test_main_append_leaves_the_epoch_file_byte_identical(tmp_path):
    """The daily job writes observations; a boundary is a judgement, and the
    two writers are not the same one."""
    ci, smoke = _write_rollups(tmp_path)
    record = tmp_path / "timings"
    record.mkdir()
    epochs = _epochs_file(record, {"date": TODAY, "label": "legacy",
                                   "note": EPOCH_NOTE})
    before = epochs.read_bytes()
    assert timings.main([
        "append", "--ci-timing", str(ci), "--smoke-timings", str(smoke),
        "--today", TODAY, "--dir", str(record),
        "--census-out", str(tmp_path / "census.json"),
    ]) == 0
    assert epochs.read_bytes() == before
    assert json.loads((tmp_path / "census.json").read_text())["epoch"] == {
        "date": TODAY, "label": "legacy"}


def test_main_show_prints_the_epoch_keys_too(tmp_path, capsys):
    _epochs_file(tmp_path, {"date": TODAY, "label": "legacy", "note": EPOCH_NOTE})
    assert timings.main(["show", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "epochs: 1" in out
    assert "'label': 'legacy'" in out and "'date': '2026-09-05'" in out


def test_the_committed_record_carries_the_legacy_and_fast_tests_boundaries():
    """Declared data in the record, read the way the config tests read
    config/repos.yaml: the `legacy` boundary labels the pre-rebuild reference
    round, and `fast-tests` is the one the rebuild phases appended when they
    landed (the day after). Readers take `since` from the newest one."""
    epochs = timings.read_epochs(timings.EPOCHS_FILE)
    assert [(e["date"], e["label"]) for e in epochs] == [
        ("2026-09-05", "legacy"),
        ("2026-09-06", "fast-tests"),
    ]
    assert all(e["note"] for e in epochs)
    assert len(timings.EPOCHS_FILE.read_text().splitlines()) == 2
    assert timings.current_epoch(epochs)["label"] == "fast-tests"


# --- the cache state a measurement was taken under --------------------------
# A leg that ran with a restored JAX compile cache and one that recompiled from
# scratch are not the same measurement. The record stores which it was, because
# a baseline that cannot say is a baseline nothing can compare against.

def _cached_rollup(jax="hit", datasets="miss", numba="hit", **kw):
    rollup = _smoke_rollup(**kw)
    rollup["repos"][0]["cache"] = {"jax": jax, "datasets": datasets,
                                   "numba": numba, "epoch": "1"}
    return rollup


def test_the_scripts_line_records_the_cache_state_beside_the_seconds():
    (line,) = timings.scripts_lines_from_rollup(_cached_rollup(), TODAY)["RepoA"]
    # One key per cache the smoke gate restores, and no epoch: that is a
    # property of the workflow's keys, not of the measurement.
    assert line["cache"] == {"jax": "hit", "datasets": "miss", "numba": "hit"}


def test_a_rollup_from_before_the_sidecar_records_unknown_not_a_miss():
    """Every rollup older than the cache work. "We do not know" must stay
    distinguishable from "nothing was restored"."""
    (line,) = timings.scripts_lines_from_rollup(_smoke_rollup(), TODAY)["RepoA"]
    assert line["cache"] == {"jax": "unknown", "datasets": "unknown",
                             "numba": "unknown"}
    junk = _smoke_rollup()
    junk["repos"][0]["cache"] = {"jax": "warm", "datasets": None}
    (line,) = timings.scripts_lines_from_rollup(junk, TODAY)["RepoA"]
    assert line["cache"] == {"jax": "unknown", "datasets": "unknown",
                             "numba": "unknown"}


# --- the gate's fixed overhead, recorded beside the seconds -----------------
# A fifth of every `_test` leg is spent before the first script runs. The
# record carries it so a leg whose total fell while its setup rose cannot read
# as a leg that got faster.

def test_the_scripts_line_records_the_setup_seconds_beside_the_cache():
    rollup = _cached_rollup()
    rollup["repos"][0]["cache"]["setup_s"] = 104.0
    (line,) = timings.scripts_lines_from_rollup(rollup, TODAY)["RepoA"]
    assert line["setup_s"] == 104.0
    # Beside `cache`, not inside it: `cache` says what the measurement was
    # taken UNDER and every value in it is a state string; this is a
    # measurement of its own, of the part no script is responsible for.
    assert "setup_s" not in line["cache"]


def test_a_line_whose_rollup_never_measured_the_setup_records_null():
    """Every rollup older than the mark steps, and every leg whose marks did
    not survive. A fabricated 0 would claim a gate with no fixed cost."""
    (line,) = timings.scripts_lines_from_rollup(_smoke_rollup(), TODAY)["RepoA"]
    assert line["setup_s"] is None
    for junk in ("104", True, -5, float("nan"), None):
        rollup = _cached_rollup()
        rollup["repos"][0]["cache"]["setup_s"] = junk
        (line,) = timings.scripts_lines_from_rollup(rollup, TODAY)["RepoA"]
        assert line["setup_s"] is None, junk
    # Zero is a reading, not an absence.
    rollup = _cached_rollup()
    rollup["repos"][0]["cache"]["setup_s"] = 0
    (line,) = timings.scripts_lines_from_rollup(rollup, TODAY)["RepoA"]
    assert line["setup_s"] == 0.0


# --- the unit record: jax and numba, combined --------------------------------
# A library suite is conditioned by two caches at once, so a comparison is only
# safe when both sides ran the same way — and the combined state has its own
# field name because it means something different from the scripts record's
# `cache_jax`.

def _unit_cached_rollup(jax="hit", numba="hit", **kw):
    rollup = _unit_rollup(**kw)
    rollup["repos"][0]["cache"] = {"jax": jax, "datasets": "miss",
                                   "numba": numba, "epoch": "1"}
    return rollup


def test_the_unit_line_records_the_two_caches_it_was_measured_under():
    (line,) = timings.unit_lines_from_rollup(_unit_cached_rollup(numba="miss"),
                                             TODAY)["RepoA"]
    # Two keys, not the scripts record's three: the libraries' gate has no
    # dataset cache, so a `datasets` field here could only say "unknown".
    assert line["cache"] == {"jax": "hit", "numba": "miss"}


def test_a_unit_rollup_from_before_the_sidecar_records_unknown_on_both():
    (line,) = timings.unit_lines_from_rollup(_unit_rollup(), TODAY)["RepoA"]
    assert line["cache"] == {"jax": "unknown", "numba": "unknown"}
    junk = _unit_rollup()
    junk["repos"][0]["cache"] = {"jax": "warm", "numba": None}
    (line,) = timings.unit_lines_from_rollup(junk, TODAY)["RepoA"]
    assert line["cache"] == {"jax": "unknown", "numba": "unknown"}


def test_the_combined_unit_cache_state_is_hit_only_when_both_were():
    """Half-hot against half-cold is not a comparison anybody can read, so
    every mixture is unknown and compares exactly as it always did."""
    state = timings.unit_cache_state
    assert state({"jax": "hit", "numba": "hit"}) == "hit"
    assert state({"jax": "miss", "numba": "miss"}) == "miss"
    for mixed in ({"jax": "hit", "numba": "miss"},
                  {"jax": "miss", "numba": "hit"},
                  {"jax": "hit", "numba": "unknown"},
                  {"jax": "unknown", "numba": "hit"},
                  {"jax": "hit"}, {}, None, "nonsense"):
        assert state(mixed) == "unknown", mixed


def test_previous_unit_rows_hands_the_combined_state_to_the_drift_rule(tmp_path):
    unit_dir = tmp_path / "unit"
    path = timings.unit_file("RepoA", tmp_path)
    timings.append_unit(path, timings.unit_lines_from_rollup(
        _unit_cached_rollup(seconds=4.0), TODAY)["RepoA"])
    row = timings.previous_unit_rows(unit_dir)[("RepoA", "3.12", NODEID)]
    # Named for what it is — jax AND numba — not for half of it.
    assert row["cache_state"] == "hit" and "cache_jax" not in row

    thr = {"slow_factor": 2.0, "min_delta_s": 2}
    # 4s → 12s is a 3x, and it is not reported: the baseline ran hot.
    assert smoke_timings.classify_drift(
        12.0, row, thr, run_id=9, cache="miss",
        prev_cache_key="cache_state") == ("ok", None, None)
    assert smoke_timings.classify_drift(
        12.0, row, thr, run_id=9, cache="hit",
        prev_cache_key="cache_state")[0] == "warn"


def test_a_legacy_unit_line_reads_unknown_and_compares_as_it_always_did(tmp_path):
    """The record is append-only and full of lines written before this field
    existed; they must keep comparing exactly as they did."""
    unit_dir = tmp_path / "unit"
    path = timings.unit_file("RepoA", tmp_path)
    (line,) = timings.unit_lines_from_rollup(_unit_cached_rollup(seconds=4.0),
                                             TODAY)["RepoA"]
    line.pop("cache")
    timings.append_unit(path, [line])
    row = timings.previous_unit_rows(unit_dir)[("RepoA", "3.12", NODEID)]
    assert row["cache_state"] == "unknown"
    thr = {"slow_factor": 2.0, "min_delta_s": 2}
    assert smoke_timings.classify_drift(
        12.0, row, thr, run_id=9, cache="hit",
        prev_cache_key="cache_state")[0] == "warn"


def test_previous_script_rows_hands_the_cache_state_to_the_drift_rule(tmp_path):
    scripts_dir = tmp_path / "scripts"
    path = timings.scripts_file("RepoA", tmp_path)
    timings.append_scripts(path, timings.scripts_lines_from_rollup(
        _cached_rollup(jax="hit", seconds=10.0), TODAY)["RepoA"])
    prev = timings.previous_script_rows(scripts_dir)
    row = prev[("RepoA", "3.12", "imaging/x.py")]
    assert row["cache_jax"] == "hit"
    # 10s → 30s is a 3x, and it is not reported: the baseline ran hot.
    thr = {"slow_factor": 2.0, "min_delta_s": 5}
    assert smoke_timings.classify_drift(30.0, row, thr, run_id=9,
                                        cache="miss") == ("ok", None, None)
    assert smoke_timings.classify_drift(30.0, row, thr, run_id=9,
                                        cache="hit")[0] == "warn"


def test_a_legacy_record_line_reads_unknown_and_compares_as_it_always_did(tmp_path):
    """The record is append-only and full of lines written before this field
    existed; they must keep comparing exactly as they did."""
    scripts_dir = tmp_path / "scripts"
    path = timings.scripts_file("RepoA", tmp_path)
    (line,) = timings.scripts_lines_from_rollup(_smoke_rollup(seconds=10.0),
                                                TODAY)["RepoA"]
    line.pop("cache")
    timings.append_scripts(path, [line])
    row = timings.previous_script_rows(scripts_dir)[("RepoA", "3.12", "imaging/x.py")]
    assert row["cache_jax"] == "unknown"
    thr = {"slow_factor": 2.0, "min_delta_s": 5}
    assert smoke_timings.classify_drift(30.0, row, thr, run_id=9,
                                        cache="hit")[0] == "warn"
