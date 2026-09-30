"""Timing clarity must not turn missing evidence into a passing measurement."""
import copy
import json
import re

import pytest

from heart import dashboard, timing_display as display
from tests.test_dashboard import (
    FRESH_NOW, _ci_timing_slice, _legacy_import, _legacy_unit,
    _section, _unit_timings_slice, make_snapshot, make_verdict,
)


def test_four_building_and_one_unavailable_import_are_not_green():
    snapshot = make_snapshot(import_time=_legacy_import(
        packages_measured=4, green_count=0, new_packages_no_baseline=4,
        packages_unavailable=["pkg_missing"]))
    before = copy.deepcopy(snapshot)
    board = dashboard.build_board(snapshot, make_verdict(), now=FRESH_NOW)
    section = _section(board, "import_time")
    assert section.state == dashboard.WARN
    assert "4 baseline building" in section.summary
    assert "1 unavailable" in section.summary
    assert "0 imports within baseline" not in section.summary
    assert "pkg_missing: unavailable" in section.details[0]
    assert board.score == 100 and board.verdict == "green"  # advisory != readiness
    assert snapshot == before


@pytest.mark.parametrize("overrides,state", [
    ({"green_count": 0, "new_packages_no_baseline": 2}, dashboard.INFO),
    ({"packages_measured": 0, "green_count": 0}, dashboard.WARN),
    ({"green_count": 2}, dashboard.OK),
    ({"red_count": 1, "new_packages_no_baseline": 1}, dashboard.FAIL),
])
def test_import_coverage_preserves_real_regressions(overrides, state):
    board = dashboard.build_board(make_snapshot(import_time=_legacy_import(**overrides)),
                                  make_verdict(), now=FRESH_NOW)
    assert _section(board, "import_time").state == state


def test_import_cards_keep_failed_rows_baseline_and_source():
    plain, html = display.imports(_unit_timings_slice(), {})
    assert "unavailable" in plain[1] and "0.00s" not in plain[1]
    assert "Baseline 1.00s · change +260%" in html
    assert "3 baseline samples" in html and "source run" in html
    assert "Measured regression" in html


def test_suites_rank_tests_and_disclose_without_dropping_parallel_legs():
    data = _unit_timings_slice()
    leg = data["repos"][0]
    leg["slowest"] = [{"nodeid": f"test_{i}", "seconds": i} for i in range(6)]
    data["repos"] = [dict(leg, python=f"3.{i}") for i in range(10, 18)]
    plain, html = display.suites(data)
    assert sum("wall-clock 412.00s" in s for s in plain) == 8
    assert "3,296" not in html  # never sum parallel durations
    assert "Show 5 more suite legs" in html
    assert "Show 3 more measured tests" in html
    assert html.index("test_5") < html.index("test_4") < html.index("test_3") < html.index("test_2")
    assert "3 skipped" in html and "0 failures" in html
    assert "No comparable previous run" in html


def test_previous_time_without_comparable_ratio_is_not_a_regression():
    data = _unit_timings_slice()
    data["tests"][0]["ratio"] = None
    _, html = display.suites(data)
    assert "No comparable previous run" in html
    assert "measured regression" not in html


def test_ci_chart_labels_statistics_and_leaves_missing_day_as_gap():
    ct = _ci_timing_slice()
    ct["gates"][0]["window_from"] = "2026-08-01T00:00:00Z"
    ct["history"][1]["gates"] = {}
    plain, html = display.gates(ct)
    assert "17 completed runs" in plain[0] and "2026-08-01" in plain[0]
    assert html.index("Median") < html.index("<svg")
    assert 'role="img" aria-label=' in html
    assert "2026-08-23: unavailable" in html
    # The missing middle measurement must break the line, not connect or drop to 0.
    segments = re.findall(r'<polyline points="([^"]*)"', html)
    assert len(segments) == 2 and all(" " not in p for p in segments)
    assert "Range 600.00s – 900.00s" in html


@pytest.mark.parametrize("value", [None, True, -1, "90", float("nan"), float("inf")])
def test_invalid_measurements_never_become_zero(value):
    assert display.duration(value) == "unavailable"
    html = display.chart([{"date": "2026-09-01", "gates": {"A/B": {"p50_s": value}}}], "A/B")
    assert "No measured daily history" in html and "<svg" not in html


def test_timing_text_and_links_are_escaped_and_unsafe_links_omitted():
    data = _unit_timings_slice()
    for row in data["imports"]:
        row.update(package='<img src=x onerror="alert(1)">', run_url="javascript:alert(1)")
    _, html = display.imports(data, {})
    assert "<img" not in html and "&lt;img" in html and "javascript:" not in html


def test_performance_keeps_consumer_fields_and_spark_and_no_input_mutation():
    ct, unit = _ci_timing_slice(), _unit_timings_slice()
    snapshot = make_snapshot(ci_timing=ct, unit_timings=unit,
                             import_time=_legacy_import(), unit_test_timing=_legacy_unit())
    before = copy.deepcopy(snapshot)
    board = dashboard.build_board(snapshot, make_verdict(), now=FRESH_NOW)
    perf = json.loads(dashboard.render(snapshot, make_verdict(), fmt="json", now=FRESH_NOW))["performance"]
    assert perf["schema"] == 1
    assert perf["gates"][0] == {
        "repo": "RepoA", "workflow": "Gate One", "median_s": 900.0,
        "pr_median_s": 960.0, "max_s": 1200.0, "runs_counted": 17,
        "state": "warn", "prompt": ct["gates"][0]["prompt"],
        "actions_url": ct["gates"][0]["actions_url"], "spark": "▁▃█",
    }
    assert perf["unit"]["repos"] == unit["repos"]
    assert perf["unit"]["imports"] == unit["imports"]
    assert perf["history"] == ct["history"]
    assert before == snapshot


def test_zero_run_ci_is_information_and_not_a_passing_gate():
    ct = _ci_timing_slice(events=False, warn=False)
    for row in ct["gates"]:
        row.update(runs_counted=0, median_s=None, max_s=None)
    board = dashboard.build_board(make_snapshot(ci_timing=ct), make_verdict(), now=FRESH_NOW)
    section = _section(board, "ci_timing")
    assert section.state == dashboard.INFO
    assert "Median <strong class=\"duration\">unavailable" in section.timing_html
