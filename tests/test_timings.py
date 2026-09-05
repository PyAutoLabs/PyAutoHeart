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
        "status": "passed", "cap_s": 600.0,
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
    assert timings.census(tmp_path) == {
        "gates_days": 2, "gates_first": "2026-09-04", "gates_last": TODAY,
        "scripts_observations": 2, "repos": 2,
        "unit_observations": 0, "unit_repos": 0, "unparseable": 0,
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


def test_the_epoch_file_is_reserved_and_never_written(tmp_path):
    """`epochs.jsonl` is a human's judgement about the world, not an
    observation — the path is named so the reservation lives in the code."""
    assert timings.EPOCHS_FILE.name == "epochs.jsonl"
    ci, smoke = _write_rollups(tmp_path)
    record = tmp_path / "timings"
    timings.main(["append", "--ci-timing", str(ci), "--smoke-timings", str(smoke),
                  "--today", TODAY, "--dir", str(record),
                  "--census-out", str(tmp_path / "census.json")])
    assert not (record / "epochs.jsonl").exists()
    assert not timings.EPOCHS_FILE.exists()


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
        "seconds": 4.0, "run_id": 8, "run_url": RUN_URL}
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
