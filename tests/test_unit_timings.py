"""tests/test_unit_timings.py — per-test + per-import CI timings (#206).

The unit twin of ``test_smoke_timings.py``: artifact selection, the two parses
(pytest's junit XML and the runner's ``import_time/1`` dataset), the drift rules
(both-gates for tests, the median window for imports) and the two LEGACY
summaries that make the board's existing "Unit-test timing" and "Import timing"
sections come alive without changing their shape.

Fake repo/owner/package/test names throughout (the tenant firewall): instance
facts live in `config/repos.yaml`, which is the declared surface, never in test
data.
"""

from __future__ import annotations

import json

from heart import dashboard
from heart.checks import import_time as legacy_import
from heart.checks import unit_test_timing as legacy_unit
from heart.checks import unit_timings as ut

REPO = "RepoA"
OWNER = "OwnerX"
PACKAGE = "pkg_a"
RUN_URL = f"https://github.com/{OWNER}/{REPO}/actions/runs/7"
PREV_RUN_URL = "https://ci.invalid/OwnerX/RepoA/actions/runs/6"
NODEID = "tests/foo/test_bar.py::test_x"

THRESHOLDS = {
    "slow_factor": 2.0, "min_delta_s": 2, "top_n": 25,
    "import_yellow_factor": 1.5, "import_red_factor": 3.0,
    "import_window": 7, "import_min_samples": 3,
}


# --- fixtures ---------------------------------------------------------------
def _artifact(name="unit-timings-3.12", *, id_=11, created="2026-09-01T10:00:00Z",
              expired=False, run_id=7, branch="feat/x", sha="abc123"):
    """One row of the REST /actions/artifacts payload."""
    return {
        "id": id_, "name": name, "expired": expired, "created_at": created,
        "workflow_run": {"id": run_id, "head_branch": branch, "head_sha": sha},
    }


def _selected(**kw):
    """The SELECTED artifact dict (what the shell leg hands build_sidecar)."""
    (picked,) = ut.select_artifacts([_artifact(**kw)])
    return picked


def _case(classname="tests.foo.test_bar", name="test_x", time="1.5"):
    attrs = f'classname="{classname}" name="{name}"'
    if time is not None:
        attrs += f' time="{time}"'
    return f"<testcase {attrs}/>"


def _junit(cases=None, *, tests=2, failures=0, errors=0, skipped=1, time="41.0"):
    cases = _case() if cases is None else cases
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="{tests}" '
        f'failures="{failures}" errors="{errors}" skipped="{skipped}" '
        f'time="{time}">{cases}</testsuite></testsuites>'
    )


def _import_json(*, seconds=3.6, returncode=0, schema="import_time/1",
                 package=PACKAGE, python="3.12"):
    return json.dumps({
        "schema": schema, "package": package, "python": python,
        "seconds": seconds, "returncode": returncode,
        "ts": "2026-09-01T09:55:00Z",
    })


def _extracted(tmp_path, *, sub="art", junit=..., imports=...):
    """An extracted-artifact directory holding either/both dataset files."""
    d = tmp_path / sub
    d.mkdir(parents=True, exist_ok=True)
    if junit is ...:
        junit = _junit()
    if imports is ...:
        imports = _import_json()
    if junit is not None:
        (d / "junit.xml").write_text(junit)
    if imports is not None:
        (d / "import_time.json").write_text(imports)
    return d


def _legs(tmp_path, **kw):
    return [{"artifact": _selected(), "dir": _extracted(tmp_path, **kw), "error": ""}]


def _sidecar(tmp_path, *, top_n=25, **kw):
    return ut.build_sidecar(REPO, "libraries", OWNER, _legs(tmp_path, **kw), "T",
                            top_n=top_n)


def _prev_rows(seconds, run_id=6, nodeid=NODEID, python="3.12"):
    return {(REPO, python, nodeid): {"seconds": seconds, "run_id": run_id,
                                     "run_url": PREV_RUN_URL}}


def _cfg(tmp_path, **kw):
    values = dict(THRESHOLDS)
    values.update(kw)
    body = "thresholds:\n  unit_timings:\n" + "".join(
        f"    {key}: {value}\n" for key, value in values.items()
    )
    cfg = tmp_path / "repos.yaml"
    cfg.write_text(body)
    return str(cfg)


# --- artifact selection ------------------------------------------------------

def test_selection_keeps_the_newest_unit_artifact_per_python_leg():
    listing = {"artifacts": [
        _artifact(id_=1, created="2026-08-30T10:00:00Z", run_id=5),
        _artifact(id_=2, created="2026-09-01T10:00:00Z", run_id=7),
        _artifact("unit-timings-3.13", id_=3, created="2026-09-01T10:00:00Z"),
    ]}
    picked = ut.select_artifacts(listing)
    assert [a["python"] for a in picked] == ["3.12", "3.13"]
    by_py = {a["python"]: a for a in picked}
    assert by_py["3.12"]["id"] == 2 and by_py["3.12"]["run_id"] == 7
    assert by_py["3.12"]["head_branch"] == "feat/x"


def test_selection_ignores_the_smoke_and_weekly_artifact_names():
    """A smoke script and a unit test are different sizes of thing measured on
    different cadences; mixing the channels would compare one to the other."""
    listing = {"artifacts": [
        _artifact("smoke-timings-3.12", id_=1),
        _artifact("smoke-timings-scripts-ProjA-imaging", id_=2),
        _artifact("unit-timings-scripts-ProjA", id_=3),
        _artifact("unit-timings-3.12", id_=4),
    ]}
    assert [a["id"] for a in ut.select_artifacts(listing)] == [4]


def test_selection_skips_expired_artifacts_and_tolerates_garbage():
    listing = {"artifacts": [
        _artifact(id_=1, created="2026-09-02T10:00:00Z", expired=True),
        _artifact(id_=2, created="2026-09-01T10:00:00Z"),
    ]}
    assert [a["id"] for a in ut.select_artifacts(listing)] == [2]
    assert ut.select_artifacts(None) == []
    assert ut.select_artifacts({"artifacts": [None, "x", {}]}) == []


# --- parsing pytest's junit XML ---------------------------------------------

def test_a_module_classname_becomes_a_pytest_path_nodeid():
    tests, suite, err = ut.parse_junit(_junit())
    assert err == ""
    assert tests == [{"nodeid": "tests/foo/test_bar.py::test_x", "seconds": 1.5}]
    assert suite == {"tests": 2, "failures": 0, "errors": 0, "skipped": 1,
                     "wall_s": 41.0}


def test_a_class_in_the_middle_of_the_classname_stays_a_class_segment():
    tests, _, err = ut.parse_junit(_junit(
        _case(classname="tests.foo.test_bar.TestThing")))
    assert err == "" and tests[0]["nodeid"] == (
        "tests/foo/test_bar.py::TestThing::test_x")


def test_an_unrecognisable_classname_is_carried_through_verbatim():
    """Stable beats guessed: an id that compares against itself run to run is
    all the drift rule needs."""
    tests, _, _ = ut.parse_junit(_junit(_case(classname="TestThing")))
    assert tests[0]["nodeid"] == "TestThing::test_x"
    tests, _, _ = ut.parse_junit(_junit(_case(classname="")))
    assert tests[0]["nodeid"] == "test_x"


def test_a_testcase_without_a_time_was_never_measured():
    tests, _, err = ut.parse_junit(_junit(
        _case(name="test_x") + _case(name="test_y", time=None)))
    assert err == "" and [t["nodeid"] for t in tests] == [
        "tests/foo/test_bar.py::test_x"]


def test_the_suite_totals_are_summed_over_every_testsuite():
    """A run can emit more than one <testsuite>; the coverage is their sum."""
    doc = (
        "<testsuites>"
        f'<testsuite tests="2" failures="1" errors="0" skipped="1" time="10.5">'
        f"{_case(name='test_x')}</testsuite>"
        f'<testsuite tests="3" failures="0" errors="2" skipped="0" time="4.5">'
        f"{_case(name='test_y')}</testsuite>"
        "</testsuites>"
    )
    tests, suite, err = ut.parse_junit(doc)
    assert err == ""
    assert suite == {"tests": 5, "failures": 1, "errors": 2, "skipped": 1,
                     "wall_s": 15.0}
    assert len(tests) == 2


def test_malformed_junit_is_an_honest_error_never_a_raise():
    for text in ("<testsuites", "", "   "):
        tests, suite, err = ut.parse_junit(text)
        assert tests == [] and suite == {} and err
    tests, suite, err = ut.parse_junit("<other/>")
    assert tests == [] and suite == {} and "no <testsuite>" in err


# --- parsing the import_time/1 dataset --------------------------------------

def test_import_parse_rejects_a_foreign_schema_and_junk():
    row, err = ut.parse_import_time(_import_json(schema="import_time/2"))
    assert row is None and "import_time/1" in err
    row, err = ut.parse_import_time("not json")
    assert row is None and "not valid JSON" in err
    row, err = ut.parse_import_time("[]")
    assert row is None and err


def test_a_failed_import_keeps_null_seconds_and_its_returncode():
    """Never 0.0: in a timing dataset that would read as an instant import."""
    row, err = ut.parse_import_time(_import_json(seconds=None, returncode=1))
    assert err == ""
    assert row == {"package": PACKAGE, "python": "3.12", "seconds": None,
                   "returncode": 1, "ts": "2026-09-01T09:55:00Z"}


# --- reading one extracted artifact -----------------------------------------

def test_a_leg_with_only_the_junit_still_carries_its_tests(tmp_path):
    """An import step that never wrote is not a reason to lose the suite."""
    tests, suite, row, err, meta = ut.read_downloaded_leg(
        _extracted(tmp_path, imports=None))
    assert err == "" and row is None and meta["python"] == ""
    assert tests and suite["tests"] == 2


def test_a_leg_with_only_the_import_still_carries_it(tmp_path):
    """An install that died before pytest ran still measured the import."""
    tests, suite, row, err, meta = ut.read_downloaded_leg(
        _extracted(tmp_path, junit=None))
    assert err == "" and tests == [] and suite == {}
    assert row["package"] == PACKAGE and meta["python"] == "3.12"


def test_a_leg_with_neither_is_an_honest_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ut.read_downloaded_leg(empty) == ([], {}, None, ut.NO_DATASET_ERROR, {})


def test_an_unparseable_half_is_reported_and_does_not_lose_the_other(tmp_path):
    tests, _, row, err, _ = ut.read_downloaded_leg(
        _extracted(tmp_path, junit="<testsuites"))
    assert tests == [] and row is not None and "not valid XML" in err


# --- the per-repo sidecar ----------------------------------------------------

def test_sidecar_leg_carries_provenance_the_suite_and_the_import(tmp_path):
    (leg,) = _sidecar(tmp_path)["legs"]
    assert leg["python"] == "3.12" and leg["artifact_id"] == 11
    assert leg["run_id"] == 7 and leg["run_url"] == RUN_URL
    assert leg["head_branch"] == "feat/x" and leg["head_sha"] == "abc123"
    assert leg["at"] == "2026-09-01T10:00:00Z" and leg["error"] == ""
    assert leg["suite"]["tests"] == 2 and leg["suite"]["wall_s"] == 41.0
    assert leg["slowest"] == [{"nodeid": NODEID, "seconds": 1.5}]
    assert leg["import"] == {"package": PACKAGE, "seconds": 3.6, "returncode": 0}


def test_the_sidecar_keeps_only_the_top_n_slowest_tests(tmp_path):
    """A 1500-test suite recorded whole would multiply every artifact
    downstream for rows nothing renders."""
    cases = "".join(_case(name=f"test_{i}", time=str(float(i)))
                    for i in range(1, 6))
    (leg,) = _sidecar(tmp_path, top_n=2, junit=_junit(cases))["legs"]
    assert [row["seconds"] for row in leg["slowest"]] == [5.0, 4.0]
    # ...and the totals stay beside them, so the coverage is still visible.
    assert leg["suite"]["tests"] == 2


def test_ties_are_broken_by_nodeid_so_the_sidecar_is_stable(tmp_path):
    cases = (_case(name="test_b", time="2.0") + _case(name="test_a", time="2.0"))
    (leg,) = _sidecar(tmp_path, top_n=2, junit=_junit(cases))["legs"]
    assert [row["nodeid"].split("::")[-1] for row in leg["slowest"]] == [
        "test_a", "test_b"]


def test_sidecar_with_a_repo_level_error_carries_no_legs():
    side = ut.build_sidecar(REPO, "libraries", OWNER, [], "T",
                            error="gh api exited 1")
    assert side["error"] == "gh api exited 1" and side["legs"] == []


def test_a_failed_leg_keeps_its_provenance_and_loses_no_sibling(tmp_path):
    """One leg's 403 must not cost the repo its other leg."""
    legs = [
        {"artifact": _selected(name="unit-timings-3.13", id_=1),
         "dir": _extracted(tmp_path, sub="ok"), "error": ""},
        {"artifact": _selected(name="unit-timings-3.12", id_=2), "dir": None,
         "error": "HTTP 403: Resource not accessible"},
    ]
    side = ut.build_sidecar(REPO, "libraries", OWNER, legs, "T")
    by_py = {leg["python"]: leg for leg in side["legs"]}
    assert by_py["3.13"]["slowest"] and by_py["3.13"]["error"] == ""
    assert by_py["3.12"]["slowest"] == [] and "403" in by_py["3.12"]["error"]
    assert by_py["3.12"]["run_url"] == RUN_URL      # provenance survives


def test_read_downloads_pairs_artifacts_with_dirs_and_error_markers(tmp_path):
    arts = ut.select_artifacts([_artifact(id_=1),
                                _artifact("unit-timings-3.13", id_=2)])
    (tmp_path / "1").mkdir()
    (tmp_path / "2.error").write_text("HTTP 403: Resource not accessible\n")
    legs = {leg["artifact"]["id"]: leg for leg in ut.read_downloads(arts, tmp_path)}
    assert legs[1]["dir"] == tmp_path / "1" and legs[1]["error"] == ""
    assert legs[2]["dir"] is None and legs[2]["error"].startswith("HTTP 403")


# --- test drift: the both-gates rule, and never against the same run --------

def test_test_drift_needs_both_the_ratio_and_the_absolute_floor(tmp_path):
    side = _sidecar(tmp_path, junit=_junit(_case(time="6.0")))
    # 3x but only +4s clears both gates at the unit floor (2 s)...
    roll = ut.aggregate([side], "T", THRESHOLDS,
                        record_prev_rows=_prev_rows(2.0))
    (row,) = roll["tests"]
    assert row["state"] == "warn" and row["ratio"] == 3.0 and row["delta_s"] == 4.0
    assert row["prev_s"] == 2.0 and row["prev_run_id"] == 6
    assert row["prompt"] == (
        f"/hygiene perf: {REPO} {NODEID} 2.0s → 6.0s between runs "
        f"{PREV_RUN_URL} → {RUN_URL}"
    )
    assert roll["slowed_tests"] == [row]
    # ...while 2.5x on a fast test does not: +1.5 s is jitter on a test that
    # runs in a second, which is exactly what the 2 s floor is for.
    fast = _sidecar(tmp_path, sub="fast", junit=_junit(_case(time="2.5")))
    quick = ut.aggregate([fast], "T", THRESHOLDS,
                         record_prev_rows=_prev_rows(1.0))
    (row,) = quick["tests"]
    assert row["state"] == "ok" and row["ratio"] == 2.5 and row["delta_s"] == 1.5


def test_a_run_is_never_compared_against_itself(tmp_path):
    side = _sidecar(tmp_path, junit=_junit(_case(time="6.0")))
    roll = ut.aggregate([side], "T", THRESHOLDS,
                        record_prev_rows=_prev_rows(2.0, run_id=7))
    (row,) = roll["tests"]
    assert row["state"] == "ok" and row["ratio"] is None


def test_with_no_record_every_test_is_ok_and_carries_no_ratio(tmp_path):
    """Day one: no baseline is not a slowdown, and not a green either."""
    (row,) = ut.aggregate([_sidecar(tmp_path)], "T", THRESHOLDS)["tests"]
    assert row["state"] == "ok" and row["ratio"] is None and row["prev_s"] is None
    assert row["prompt"] is None


# --- import drift: the median window ----------------------------------------

def test_an_import_is_building_until_the_window_has_enough_samples():
    for history in ([], [3.0], [3.0, 3.0]):
        state, baseline, ratio, samples = ut.classify_import(3.0, history, THRESHOLDS)
        assert state == "building" and ratio is None
        assert samples == len(history)
    state, baseline, ratio, samples = ut.classify_import(3.0, [3.0, 3.0, 3.0],
                                                         THRESHOLDS)
    assert (state, baseline, ratio, samples) == ("green", 3.0, 1.0, 3)


def test_the_import_factors_split_green_yellow_and_red():
    history = [2.0, 2.0, 2.0]
    assert ut.classify_import(3.0, history, THRESHOLDS)[0] == "green"    # 1.5x
    assert ut.classify_import(3.2, history, THRESHOLDS)[0] == "yellow"   # 1.6x
    assert ut.classify_import(6.0, history, THRESHOLDS)[0] == "yellow"   # 3.0x
    assert ut.classify_import(6.4, history, THRESHOLDS)[0] == "red"      # 3.2x


def test_a_null_import_has_no_verdict_to_give():
    assert ut.classify_import(None, [2.0, 2.0, 2.0], THRESHOLDS) == (
        "building", 2.0, None, 3)


def test_the_baseline_is_the_median_of_the_window_not_the_last_run():
    state, baseline, ratio, samples = ut.classify_import(
        9.0, [2.0, 2.0, 20.0, 2.0], THRESHOLDS)
    assert baseline == 2.0 and state == "red" and ratio == 4.5 and samples == 4


def test_a_slowed_import_row_carries_its_own_prompt(tmp_path):
    roll = ut.aggregate([_sidecar(tmp_path)], "T", THRESHOLDS,
                        record_import_history={
                            (REPO, PACKAGE, "3.12"): [1.0, 1.0, 1.0]})
    (row,) = roll["imports"]
    assert row["state"] == "red" and row["baseline_s"] == 1.0 and row["ratio"] == 3.6
    assert row["samples"] == 3 and row["seconds"] == 3.6
    assert row["prompt"] == (
        f"/hygiene perf: import {PACKAGE} 1.00s → 3.60s (py3.12) — {RUN_URL}")
    assert roll["slowed_imports"] == [row]


# --- the rollup --------------------------------------------------------------

def test_the_repo_row_carries_what_the_record_is_built_from(tmp_path):
    (leg,) = ut.aggregate([_sidecar(tmp_path)], "T", THRESHOLDS)["repos"]
    assert leg["repo"] == REPO and leg["python"] == "3.12"
    assert leg["run_id"] == 7 and leg["run_url"] == RUN_URL
    assert leg["head_branch"] == "feat/x" and leg["head_sha"] == "abc123"
    assert leg["tests"] == 2 and leg["wall_s"] == 41.0
    assert leg["import_s"] == 3.6 and leg["package"] == PACKAGE
    assert leg["suite"]["skipped"] == 1
    assert leg["slowest"] == [{"nodeid": NODEID, "seconds": 1.5}]


def test_errors_carry_both_repo_and_leg_level_failures(tmp_path):
    dead = ut.build_sidecar("RepoB", "libraries", OWNER, [], "T",
                            error="gh api exited 1")
    partial = ut.build_sidecar(REPO, "libraries", OWNER, [
        {"artifact": _selected(), "dir": None, "error": "HTTP 403"},
    ], "T")
    roll = ut.aggregate([dead, partial], "T", THRESHOLDS)
    assert {"repo": "RepoB", "error": "gh api exited 1"} in roll["errors"]
    assert {"repo": REPO, "error": "3.12: HTTP 403"} in roll["errors"]
    # ...and the dead repo contributes no rows that could read as "all quiet".
    assert roll["tests"] == [] and roll["repos"][0]["error"] == "HTTP 403"


def test_ordering_is_stable(tmp_path):
    def side(repo, python, art_id, cases):
        art = _selected(name=f"unit-timings-{python}", id_=art_id)
        return ut.build_sidecar(repo, "libraries", OWNER, [
            {"artifact": art,
             "dir": _extracted(tmp_path, sub=f"{repo}-{python}",
                               junit=_junit(cases)),
             "error": ""}], "T")

    b = side("RepoB", "3.12", 1, _case(name="test_z"))
    a = side(REPO, "3.12", 2, _case(name="test_b") + _case(name="test_a"))
    a13 = side(REPO, "3.13", 3, _case(name="test_a"))
    roll = ut.aggregate([b, a, a13], "T", THRESHOLDS)
    assert [(r["repo"], r["python"]) for r in roll["repos"]] == [
        (REPO, "3.12"), (REPO, "3.13"), ("RepoB", "3.12")]
    assert [(r["repo"], r["python"], r["nodeid"].split("::")[-1])
            for r in roll["tests"]] == [
        (REPO, "3.12", "test_a"), (REPO, "3.12", "test_b"),
        (REPO, "3.13", "test_a"), ("RepoB", "3.12", "test_z")]


def test_aggregate_survives_garbage_sidecars():
    roll = ut.aggregate([None, "nope", {}, {"name": REPO, "legs": "nope"},
                         {"name": REPO, "legs": [None, {"slowest": "nope"}]}],
                        "T", THRESHOLDS)
    assert roll["tests"] == [] and roll["imports"] == []


# --- the two legacy summaries ------------------------------------------------
#
# They are the WHOLE point of the ingest: the board's existing sections read
# these two files, so the shapes have to match key for key.

LEGACY_UNIT_KEYS = {"python", "repos_measured", "repos_unavailable",
                    "new_tests_no_baseline", "red_count", "yellow_count",
                    "green_count", "red", "yellow"}
LEGACY_IMPORT_KEYS = {"python", "packages_measured", "packages_unavailable",
                      "new_packages_no_baseline", "red_count", "yellow_count",
                      "green_count", "red", "yellow"}


def test_the_legacy_shapes_are_exactly_what_the_devbox_checks_write(tmp_path):
    unit, imports = ut.legacy_summaries(
        ut.aggregate([_sidecar(tmp_path)], "T", THRESHOLDS), THRESHOLDS)
    assert set(unit) == LEGACY_UNIT_KEYS
    assert set(imports) == LEGACY_IMPORT_KEYS
    # ...and the same key set the dev-box modules produce, so a board section
    # cannot tell which vantage measured it.
    assert set(unit) == set(legacy_unit.run(runner=lambda d: None, repos=[]))
    assert set(imports) == set(legacy_import.run(measurer=lambda p: None,
                                                 packages=[]))
    assert unit["python"] == "ci" and imports["python"] == "ci"


def test_a_new_test_is_not_a_green_one(tmp_path):
    unit, _ = ut.legacy_summaries(
        ut.aggregate([_sidecar(tmp_path)], "T", THRESHOLDS), THRESHOLDS)
    assert unit["new_tests_no_baseline"] == 1
    assert unit["green_count"] == 0 and unit["repos_measured"] == 1


def test_the_legacy_unit_buckets_keep_the_sections_wording_truthful(tmp_path):
    """The section says "(>3× baseline)" / "(>1.5×)"; these constants exist
    only so that wording stays true when the rows come from CI."""
    side = _sidecar(tmp_path, junit=_junit(_case(time="8.0")))
    roll = ut.aggregate([side], "T", THRESHOLDS, record_prev_rows=_prev_rows(2.0))
    unit, _ = ut.legacy_summaries(roll, THRESHOLDS)
    assert unit["red_count"] == 1 and unit["yellow_count"] == 0
    (entry,) = unit["red"]
    assert entry == {"repo": REPO, "test": NODEID, "latest_seconds": 8.0,
                     "baseline_seconds": 2.0, "ratio": 4.0, "samples": 1}

    side = _sidecar(tmp_path, sub="yellow", junit=_junit(_case(time="4.0")))
    roll = ut.aggregate([side], "T", THRESHOLDS, record_prev_rows=_prev_rows(2.0))
    unit, _ = ut.legacy_summaries(roll, THRESHOLDS)
    assert unit["yellow_count"] == 1 and unit["red_count"] == 0


def test_an_unavailable_repo_is_listed_never_counted_as_measured(tmp_path):
    dead = ut.build_sidecar("RepoB", "libraries", OWNER, [], "T", error="boom")
    roll = ut.aggregate([_sidecar(tmp_path), dead], "T", THRESHOLDS)
    unit, _ = ut.legacy_summaries(roll, THRESHOLDS)
    assert unit["repos_measured"] == 1 and unit["repos_unavailable"] == ["RepoB"]


def test_a_null_import_is_unavailable_not_a_fast_one(tmp_path):
    side = _sidecar(tmp_path, imports=_import_json(seconds=None, returncode=1))
    _, imports = ut.legacy_summaries(
        ut.aggregate([side], "T", THRESHOLDS), THRESHOLDS)
    assert imports["packages_measured"] == 0
    assert imports["packages_unavailable"] == [PACKAGE]
    assert imports["new_packages_no_baseline"] == 0


def test_the_legacy_import_buckets_come_from_the_row_state(tmp_path):
    roll = ut.aggregate([_sidecar(tmp_path)], "T", THRESHOLDS,
                        record_import_history={
                            (REPO, PACKAGE, "3.12"): [1.0, 1.0, 1.0]})
    _, imports = ut.legacy_summaries(roll, THRESHOLDS)
    assert imports["red_count"] == 1 and imports["packages_measured"] == 1
    assert imports["red"] == [{"package": PACKAGE, "latest_seconds": 3.6,
                               "baseline_seconds": 1.0, "ratio": 3.6,
                               "samples": 3}]


# --- ...and they drive the board's two existing sections --------------------

def _board(unit_summary=None, import_summary=None, unit_slice=None):
    snapshot = {"ts": "2026-09-05T00:00:00+00:00", "repos": {}}
    if unit_summary is not None:
        snapshot["unit_test_timing"] = unit_summary
    if import_summary is not None:
        snapshot["import_time"] = import_summary
    if unit_slice is not None:
        snapshot["unit_timings"] = unit_slice
    board = dashboard.build_board(snapshot, {"verdict": "green", "score": 100})
    return {section.key: section for section in board.sections}


def test_the_ingested_summaries_drive_the_board_ok_warn_and_fail(tmp_path):
    green_roll = ut.aggregate([_sidecar(tmp_path)], "T", THRESHOLDS,
                              record_prev_rows=_prev_rows(1.4))
    unit, _ = ut.legacy_summaries(green_roll, THRESHOLDS)
    assert _board(unit_summary=unit)["unit_test_timing"].state == dashboard.OK

    warn_roll = ut.aggregate([_sidecar(tmp_path, sub="w",
                                       junit=_junit(_case(time="4.0")))],
                             "T", THRESHOLDS, record_prev_rows=_prev_rows(2.0))
    unit, _ = ut.legacy_summaries(warn_roll, THRESHOLDS)
    assert _board(unit_summary=unit)["unit_test_timing"].state == dashboard.WARN

    fail_roll = ut.aggregate([_sidecar(tmp_path, sub="f",
                                       junit=_junit(_case(time="8.0")))],
                             "T", THRESHOLDS, record_prev_rows=_prev_rows(2.0))
    unit, imports = ut.legacy_summaries(fail_roll, THRESHOLDS)
    sections = _board(unit_summary=unit, import_summary=imports)
    assert sections["unit_test_timing"].state == dashboard.FAIL
    # The import summary of the same rollup measured its package and found
    # nothing red: the two sections are fed from one rollup and judge apart.
    assert sections["import_time"].state == dashboard.OK


# --- thresholds --------------------------------------------------------------

def test_thresholds_load_from_config(tmp_path):
    assert ut.load_thresholds(_cfg(tmp_path, slow_factor=3.0, top_n=2))["top_n"] == 2
    assert (ut.load_thresholds(tmp_path / "absent.yaml")
            == ut.DEFAULT_UNIT_TIMINGS_THRESHOLDS)


def test_real_config_declares_the_unit_timings_thresholds():
    """The shipped policy file must carry the block the check reads."""
    thr = ut.load_thresholds()
    assert thr["slow_factor"] >= 1.0 and thr["min_delta_s"] >= 1
    assert thr["top_n"] >= 1 and thr["import_window"] >= 1
    assert thr["import_red_factor"] > thr["import_yellow_factor"]


# --- main wiring -------------------------------------------------------------

def test_plan_prints_the_ids_to_download(monkeypatch, capsys):
    listing = json.dumps({"artifacts": [
        _artifact(id_=1), _artifact("unit-timings-3.13", id_=2),
        _artifact("smoke-timings-3.12", id_=3),
    ]})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(listing))
    assert ut.main(["--plan"]) == 0
    assert capsys.readouterr().out.split("\n")[:2] == [
        "1 unit-timings-3.12", "2 unit-timings-3.13"]


def test_plan_says_nothing_on_garbage(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("not json"))
    assert ut.main(["--plan"]) == 0
    assert capsys.readouterr().out == ""


def test_main_writes_the_sidecar_then_the_rollup_and_both_legacy_files(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    per_repo = tmp_path / "per-repo"
    per_repo.mkdir()
    listing = tmp_path / "listing.json"
    listing.write_text(json.dumps({"artifacts": [_artifact()]}))
    downloads = tmp_path / "downloads"
    _extracted(downloads, sub="11", junit=_junit(_case(time="6.0")))

    out = per_repo / f"{REPO}.unit_timings.json"
    assert ut.main(["--name", REPO, "--group", "libraries", "--owner", OWNER,
                    "--ts", "T", "--listing", str(listing),
                    "--downloads", str(downloads), "--out", str(out)]) == 0
    side = json.loads(out.read_text())
    assert side["legs"][0]["slowest"][0]["seconds"] == 6.0
    assert "test(s) tracked" in capsys.readouterr().out

    # The record is the only baseline: one prior observation of the same test.
    unit_dir = tmp_path / "timings" / "unit"
    unit_dir.mkdir(parents=True)
    (unit_dir / f"{REPO}.jsonl").write_text(json.dumps({
        "date": "2026-09-04", "at": "", "python": "3.12", "run_id": 6,
        "run_url": PREV_RUN_URL, "head_branch": "", "head_sha": "",
        "package": PACKAGE, "import_s": 1.0,
        "suite": {"tests": 2, "failures": 0, "errors": 0, "skipped": 0,
                  "wall_s": 40.0},
        "slowest": {NODEID: 2.0},
    }) + "\n")

    legacy = tmp_path / "legacy"
    roll_path = tmp_path / "unit_timings.json"
    assert ut.main(["--aggregate", "--per-repo-dir", str(per_repo), "--ts", "T",
                    "--record-dir", str(tmp_path / "timings"),
                    "--legacy-dir", str(legacy), "--out", str(roll_path)]) == 0
    roll = json.loads(roll_path.read_text())
    (row,) = roll["tests"]
    assert row["state"] == "warn" and row["prev_s"] == 2.0 and row["prev_run_id"] == 6
    assert "tests tracked" in capsys.readouterr().out

    unit = json.loads((legacy / "unit_test_timing.json").read_text())
    imports = json.loads((legacy / "import_time.json").read_text())
    assert set(unit) == LEGACY_UNIT_KEYS and set(imports) == LEGACY_IMPORT_KEYS
    # 2.0s → 6.0s is exactly 3.0x: over the legacy section's ">1.5×" wording,
    # not over its ">3× baseline" one.
    assert unit["yellow_count"] == 1 and unit["red_count"] == 0
    # One recorded observation is below import_min_samples, so the import is
    # still building rather than judged.
    assert imports["new_packages_no_baseline"] == 1


def test_main_records_a_fetch_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    out = tmp_path / f"{REPO}.unit_timings.json"
    assert ut.main(["--name", REPO, "--group", "libraries", "--ts", "T",
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
    out = tmp_path / f"{REPO}.unit_timings.json"
    assert ut.main(["--name", REPO, "--group", "libraries", "--ts", "T",
                    "--listing", str(listing), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["error"] == (
        "artifacts listing was not valid JSON")
    capsys.readouterr()


def test_summary_lines_are_honest_about_unavailability(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    dead = ut.build_sidecar(REPO, "libraries", OWNER, [], "T", error="boom")
    assert "UNAVAILABLE" in ut.repo_summary_line(dead)
    roll = ut.aggregate([dead], "T", THRESHOLDS)
    assert "1 unavailable" in ut.summary_line(roll)
