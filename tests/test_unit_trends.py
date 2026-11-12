"""The unit record drawn on the board: suite/import trends + the organism total."""
import json

from heart import dashboard, timings, timing_display as display
from tests.test_dashboard import (
    FRESH_NOW, _legacy_import, _legacy_unit, _section, _unit_timings_slice,
    make_snapshot, make_verdict,
)


def _line(day, tests, wall, *, branch="main", python="3.12", run_id=1, at=None, imp=1.0):
    return {"date": day, "at": at if at is not None else f"{day}T02:00:00Z",
            "python": python, "run_id": run_id, "head_branch": branch,
            "package": "pkg_a", "import_s": imp,
            "suite": {"tests": tests, "failures": 0, "errors": 0, "skipped": 0,
                      "wall_s": wall}}


def _write(directory, repo, lines):
    path = directory / "unit" / f"{repo}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def test_unit_history_keeps_main_only_and_the_last_run_of_each_day(tmp_path):
    _write(tmp_path, "RepoA", [
        _line("2026-09-01", 100, 10.0, run_id=1),
        _line("2026-09-01", 999, 99.0, run_id=2, branch="feature/x"),
        _line("2026-09-02", 110, 11.0, run_id=3, at="2026-09-01T09:00:00Z"),
        _line("2026-09-03", 120, 12.0, run_id=4, python="3.13"),
        _line("2026-09-04", True, -1, run_id=5, imp=None),
    ])
    history = timings.unit_history(tmp_path / "unit")
    points = history["RepoA"]
    # The run DAY (`at`) is the point's date; the later run that day wins.
    assert [(p["python"], p["date"], p["tests"]) for p in points] == [
        ("3.12", "2026-09-01", 110), ("3.12", "2026-09-04", None),
        ("3.13", "2026-09-03", 120)]
    assert points[1]["import_s"] is None  # never a zero
    assert all(p["tests"] != 999 for p in points)  # the branch run is not main's suite


def test_unit_history_respects_the_epoch_and_the_cap(tmp_path):
    _write(tmp_path, "RepoA", [_line(f"2026-09-0{d}", 100 + d, 1.0, run_id=d) for d in range(1, 8)])
    history = timings.unit_history(tmp_path / "unit", since="2026-09-03", cap=2)
    assert [p["date"] for p in history["RepoA"]] == ["2026-09-06", "2026-09-07"]
    assert timings.unit_history(tmp_path / "missing") == {}


def test_append_hands_the_render_a_unit_history_that_ends_today(tmp_path):
    record = tmp_path / "record"
    _write(record, "RepoA", [_line("2026-09-01", 100, 10.0)])
    out = tmp_path / "census.json"
    assert timings.main(["append", "--dir", str(record), "--census-out", str(out),
                         "--today", "2026-09-02"]) == 0
    payload = json.loads(out.read_text())
    assert payload["unit_history"]["RepoA"][0]["tests"] == 100
    # The census itself (the record's key set, `show`) is unchanged.
    assert "unit_history" not in timings.census(record)


def _history():
    return {"RepoA": [{"date": "2026-09-01", "python": "3.12", "tests": 1400, "wall_s": 300.0, "import_s": 3.0},
                      {"date": "2026-09-05", "python": "3.12", "tests": 1500, "wall_s": 412.0, "import_s": 3.6}],
            "RepoB": [{"date": "2026-09-03", "python": "3.12", "tests": 200, "wall_s": 30.0, "import_s": None}]}


def test_every_suite_card_draws_wall_clock_and_test_count_from_the_record():
    _, html = display.suites(_unit_timings_slice(), _history())
    assert html.count('class="timing-chart"') == 4  # two legs x (wall-clock, tests)
    assert "RepoA · Python 3.12: suite wall-clock seconds on main" in html
    assert "Range 1,400 – 1,500" in html and "Range 300.00s – 412.00s" in html
    # Runs are not daily: the measured points are joined, not broken at gaps.
    segments = html.count("<polyline")
    assert segments == 4  # one unbroken line per chart, across RepoA's 4-day gap
    # A leg with no record says so; a render with no record says nothing.
    _, bare = display.suites(_unit_timings_slice())
    assert "timing-chart" not in bare and "No main-branch history" not in bare
    _, empty = display.suites(_unit_timings_slice(), {})
    assert empty.count("No main-branch history in the timing record yet.") == 4


def test_import_cards_draw_import_seconds_and_skip_unmeasured_days():
    _, html = display.imports(_unit_timings_slice(), {}, _history())
    assert html.count('class="timing-chart"') == 1
    assert "RepoA · Python 3.12: import seconds on main" in html
    assert "Range 3.00s – 3.60s" in html
    # RepoB's record holds no measured import: no chart, and no zero drawn.
    assert html.count("No main-branch history in the timing record yet.") == 1


def test_organism_total_carries_counts_forward_from_when_every_library_reports():
    plain, html = display.organism_total(_history())
    assert plain == "Organism total: 1,700 unit tests across 2 libraries (latest main run of each)"
    # 09-01 has RepoA only and is left out; 09-03 = 1400+200; 09-05 = 1500+200.
    assert "2026-09-01:" not in html
    assert "<li>2026-09-03: 1,600</li>" in html and "<li>2026-09-05: 1,700</li>" in html
    assert display.organism_total({}) == (None, "")
    assert display.organism_total(None) == (None, "")


def test_unit_section_carries_the_total_row_and_charts_only_with_a_record():
    snapshot = make_snapshot(unit_timings=_unit_timings_slice(), import_time=_legacy_import(),
                             unit_test_timing=_legacy_unit(),
                             timings_record={"unit_history": _history()})
    board = dashboard.build_board(snapshot, make_verdict(), now=FRESH_NOW)
    unit = _section(board, "unit_test_timing")
    assert unit.details[-1].startswith("Organism total: 1,700 unit tests")
    assert unit.timing_html.index("Organism total") < unit.timing_html.index("Suite wall-clock")
    assert "import seconds on main" in _section(board, "import_time").timing_html

    plain = dashboard.build_board(make_snapshot(unit_timings=_unit_timings_slice(),
                                                unit_test_timing=_legacy_unit()),
                                  make_verdict(), now=FRESH_NOW)
    assert "Organism total" not in _section(plain, "unit_test_timing").timing_html
