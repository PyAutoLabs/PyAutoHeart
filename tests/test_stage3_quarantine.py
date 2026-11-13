"""tests/test_stage3_quarantine.py — Stage 3 retry + expiring quarantine
(PyAutoHeart#344; human ruling, M1 gate 2026-11-12).

A quarantine entry needs a filed bug task, expires at most 7 days after it was
filed, at most 3 are active, readiness is YELLOW while any is active, an
expired entry is RED again, and quarantine never turns a RED into GREEN. A pass
on Stage 3's one retry is recorded as flaky; a retry that fails stays red.

Workspace names here are drawn from the live config (the tenant firewall keeps
instance facts out of test files); the snapshot comes from test_readiness.
"""

from __future__ import annotations

import datetime
import json

import pytest

from heart import quarantine, readiness, validate
from tests.test_readiness import LIBS, make_snapshot

TODAY = datetime.date(2026, 6, 1)          # make_snapshot()'s ts date
WS = quarantine.stage3_workspaces()[0]


def entry(n=1, filed="2026-05-30", expires="2026-06-05", bug="DemoLib#12", **kw):
    e = {"workspace": WS, "script": f"scripts/demo/s{n}.py", "bug": bug,
         "filed": filed, "expires": expires}
    e.update(kw)
    return e


def compute(snap=None, entries=()):
    return readiness.compute(snap or make_snapshot(), libraries=LIBS, quarantine=list(entries))


# --- the Witness ------------------------------------------------------------

def test_witness_344_retry_pass_recorded_as_flaky():
    """A Stage 3 report whose script passed on the retry: the validation
    report records it as flaky (with the first error), the stage still passes,
    and readiness carries it in `stage3_flaky` without changing the tier."""
    agg = {"ready": True, "summary": {"passed": 3},
           "per_project": {WS: {"passed": 3}}, "failures": [],
           "flaky": [{"project": WS, "file": "scripts/demo/s1.py",
                      "status": "passed", "flaky": True, "attempts": 2,
                      "first_error": "ConnectionError: transient"}]}
    stage = validate.to_stage_report(agg, profile="release")
    assert stage["status"] == "pass"
    assert stage["flaky"] == [{"project": WS, "script": "scripts/demo/s1.py",
                               "first_error": "ConnectionError: transient"}]
    acc = validate._Accumulator()
    acc.add_stage(stage)
    report = validate._distill(acc)
    assert report["flaky"] == stage["flaky"]
    snap = make_snapshot()
    snap["validation_report"]["flaky"] = stage["flaky"]
    v = compute(snap)
    assert v["verdict"] == "green" and v["stage3_flaky"] == stage["flaky"]


def test_witness_344_retry_fail_recorded_red():
    """The retry failed too: the stage fails and readiness is RED."""
    agg = {"ready": False, "summary": {"passed": 2, "failed": 1},
           "per_project": {WS: {"passed": 2, "failed": 1}},
           "failures": [{"project": WS, "file": "scripts/demo/s1.py",
                         "status": "failed", "attempts": 2}]}
    stage = validate.to_stage_report(agg, profile="release")
    assert stage["status"] == "fail" and stage["flaky"] == []
    snap = make_snapshot(validation_report={**make_snapshot()["validation_report"],
                                            "validation_outcome": "fail",
                                            "release_ready": False})
    v = compute(snap)
    assert v["verdict"] == "red"
    assert any("release validation FAILED" in r for r in v["red_reasons"])


def test_witness_344_quarantined_reads_yellow_never_green():
    v = compute(entries=[entry()])
    assert v["verdict"] == "yellow"
    assert v["red_reasons"] == []
    assert v["yellow_reasons"] == [
        f"Stage 3 quarantine active (1/3): {WS}/scripts/demo/s1.py "
        "(DemoLib#12, expires 2026-06-05)"]
    assert v["stage3_quarantine"] == v["yellow_reasons"]


def test_witness_344_missing_bug_ref_is_refused():
    for bad in (entry(bug=""), {k: v for k, v in entry().items() if k != "bug"}):
        c = quarantine.classify([bad], TODAY, [WS])
        assert c["active"] == [] and len(c["refused"]) == 1
        assert "no bug-task ref" in c["refused"][0]["why"]
    v = compute(entries=[entry(bug="")])
    assert v["verdict"] == "red"
    assert any("quarantine entry refused" in r and "bug-task" in r for r in v["red_reasons"])
    # Refused means not skipped: Stage 3 runs the script.
    assert quarantine.skip_list(WS, [entry(bug="")], TODAY, [WS])["entries"] == []


def test_witness_344_expired_entry_is_red_again():
    old = entry(filed="2026-05-20", expires="2026-05-27")
    c = quarantine.classify([old], TODAY, [WS])
    assert c["active"] == [] and len(c["expired"]) == 1
    v = compute(entries=[old])
    assert v["verdict"] == "red"
    assert any("quarantine EXPIRED" in r and "gates again" in r for r in v["red_reasons"])
    # ... and Stage 3 no longer skips it.
    assert quarantine.skip_list(WS, [old], TODAY, [WS])["entries"] == []
    # An entry is live through its expiry day.
    assert quarantine.classify([entry(expires="2026-06-01")], TODAY, [WS])["active"]


def test_witness_344_fourth_entry_is_refused():
    four = [entry(1, filed="2026-05-28", expires="2026-06-04"),
            entry(2, filed="2026-05-29", expires="2026-06-05"),
            entry(3, filed="2026-05-30", expires="2026-06-06"),
            entry(4, filed="2026-05-31", expires="2026-06-07")]
    c = quarantine.classify(four, TODAY, [WS])
    assert [r["label"] for r in c["active"]] == [f"{WS}/scripts/demo/s{n}.py" for n in (1, 2, 3)]
    assert [(r["label"], r["why"]) for r in c["refused"]] == [
        (f"{WS}/scripts/demo/s4.py", "3 entries already active")]
    v = compute(entries=four)
    assert v["verdict"] == "red"
    assert len([r for r in v["yellow_reasons"] if "quarantine active (3/3)" in r]) == 3
    assert [e["script"] for e in quarantine.skip_list(WS, four, TODAY, [WS])["entries"]] == \
        [f"scripts/demo/s{n}.py" for n in (1, 2, 3)]


# --- the doctrine around it ---------------------------------------------------

def test_quarantine_never_turns_red_to_green():
    """Another red reason stays red with an active quarantine beside it."""
    snap = make_snapshot()
    snap["repos"][LIBS[0]]["ci_status"]["conclusion"] = "failure"
    v = compute(snap, entries=[entry()])
    assert v["verdict"] == "red"
    assert any("quarantine active" in r for r in v["yellow_reasons"])


@pytest.mark.parametrize("bad,why", [
    (dict(expires="2026-06-07", filed="2026-05-30"), "at most 7"),
    (dict(expires="2026-05-30", filed="2026-05-30"), "not after filed"),
    (dict(filed="2026-06-02", expires="2026-06-05"), "in the future"),
    (dict(workspace="PyAutoDemoLib"), "never unit tests or install checks"),
    (dict(script="test_thing.txt"), "not a .py path"),
    (dict(script="../escape/x.py"), "not a .py path"),
    (dict(bug="see slack"), "not an issue ref"),
    (dict(filed="yesterday"), "filed date"),
])
def test_schema_refusals(bad, why):
    c = quarantine.classify([entry(**bad)], TODAY, [WS])
    assert c["active"] == [] and why in c["refused"][0]["why"]


def test_bug_ref_forms_accepted():
    for bug in ("DemoLib#3", "DemoOrg/DemoLib#3", "https://github.com/DemoOrg/DemoLib/issues/3"):
        assert quarantine.classify([entry(bug=bug)], TODAY, [WS])["active"]


def test_skip_list_cli_and_check_exit_codes(tmp_path, capsys):
    f = tmp_path / "q.yaml"
    f.write_text(json.dumps({"entries": [entry()]}))      # JSON is valid YAML
    out = tmp_path / "skip.json"
    assert quarantine.main(["skip-list", "--workspace", WS, "--file", str(f),
                            "--date", "2026-06-01", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["entries"] == [
        {"script": "scripts/demo/s1.py", "bug": "DemoLib#12", "expires": "2026-06-05"}]
    assert quarantine.main(["check", "--file", str(f), "--date", "2026-06-01"]) == 0
    f.write_text(json.dumps({"entries": [entry(bug="")]}))
    assert quarantine.main(["check", "--file", str(f), "--date", "2026-06-01"]) == 1


def test_an_unreadable_quarantine_file_is_red(tmp_path, monkeypatch):
    f = tmp_path / "q.yaml"
    f.write_text("entries: {not: a list}\n")
    monkeypatch.setattr(quarantine, "QUARANTINE_PATH", f)
    v = readiness.compute(make_snapshot(), libraries=LIBS)
    assert v["verdict"] == "red"
    assert any("quarantine file unreadable" in r for r in v["red_reasons"])


def test_live_quarantine_file_passes_its_own_check():
    """The repo-tracked file is valid (Heart Tests reds on a refused entry)."""
    entries = quarantine.load(quarantine.HEART_HOME / "config" / "stage3_quarantine.yaml")
    today = datetime.datetime.now(datetime.timezone.utc).date()
    assert quarantine.classify(entries, today)["refused"] == []
