"""tests/test_rehearsal_ancestry.py — M2.4: a rehearsal stays fresh while main
only moves FORWARD from the rehearsed commits (PyAutoHands#337 partner).

Offline: the GitHub compare API is the injected ``api`` seam. Repo names are
synthetic; readiness is driven with an explicit library list.
"""

from __future__ import annotations

import datetime

import pytest

from heart import readiness
from heart.checks import rehearsal_ancestry as ra

LIBS = ["LibOne", "LibTwo"]
OWNERS = {lib: "some-org" for lib in LIBS}
REHEARSED = {"LibOne": "a" * 40, "LibTwo": "b" * 40}
TS = "2026-11-12T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _synthetic_gate(monkeypatch):
    # The SHA leg walks the module-level release-gate tuple; point it at the
    # synthetic libraries so the test stays free of live repo names.
    monkeypatch.setattr(readiness, "_GATE_SHA_LIBS", tuple(LIBS))


def snapshot(heads=None, ancestry=None):
    heads = heads or dict(REHEARSED)
    snap = {
        "ts": TS,
        "repos": {lib: {"ci_status": {"conclusion": "success", "head_sha": heads[lib]},
                        "repo_state": {"branch": "main", "dirty_real": 0, "behind": 0}}
                  for lib in LIBS},
        "test_run": {"ready": True, "passed": 1, "failed": 0, "ts": TS},
        "script_timing": {}, "version_skew": {"workspaces": []},
        "verify_install": {"ready": True, "ts": TS, "index": "pypi", "checks": []},
        "validation_report": {"validation_outcome": "pass", "release_ready": True,
                              "profile": "release", "commit_shas": dict(REHEARSED),
                              "stages": {}, "ts": TS},
        "token_push": {"checked_at": TS, "identity": "bot",
                       "repos": {lib: {"push": True} for lib in LIBS}},
    }
    if ancestry is not None:
        snap["rehearsal_ancestry"] = ancestry
    return snap


def fake_api(statuses):
    """compare API stub: repo -> (status, ahead_by)."""
    calls = []

    def api(path):
        calls.append(path)
        repo = path.split("/")[2]
        status, ahead = statuses[repo]
        return {"status": status, "ahead_by": ahead}
    api.calls = calls
    return api


def compute(snap):
    return readiness.compute(snap, libraries=LIBS, required_workflows={})


def test_unmoved_main_needs_no_compare_and_is_green():
    api = fake_api({})
    side = ra.collect(snapshot(), OWNERS, api)
    assert side["repos"] == {} and api.calls == []
    assert compute(snapshot(ancestry=side))["verdict"] == "green"


def test_rehearsed_sha_an_ancestor_of_main_stays_fresh_and_says_how_far_ahead():
    heads = {"LibOne": "c" * 40, "LibTwo": REHEARSED["LibTwo"]}
    api = fake_api({"LibOne": ("ahead", 3)})
    side = ra.collect(snapshot(heads), OWNERS, api)
    assert api.calls == [f"repos/some-org/LibOne/compare/{'a' * 40}...{'c' * 40}"]
    assert side["repos"]["LibOne"]["ancestor"] is True
    v = compute(snapshot(heads, side))
    assert v["verdict"] == "green"
    assert not any("release validation" in r for r in v["reasons"])
    assert v["validation_ahead"] == {"LibOne": 3}
    assert "main is ahead by LibOne 3 commit(s)" in v["validation_note"]


def test_rehearsed_sha_not_an_ancestor_is_stale():
    heads = {"LibOne": "c" * 40, "LibTwo": "d" * 40}
    side = ra.collect(snapshot(heads), OWNERS,
                      fake_api({"LibOne": ("ahead", 2), "LibTwo": ("diverged", 0)}))
    assert side["repos"]["LibTwo"]["ancestor"] is False
    v = compute(snapshot(heads, side))
    assert v["verdict"] == "stale"
    assert any("not an ancestor of main (LibTwo)" in r for r in v["stale_reasons"])
    assert v["stale_details"][0]["key"] == "validation_stale_sha"


def test_missing_ancestry_record_is_stale():
    heads = {"LibOne": "c" * 40, "LibTwo": REHEARSED["LibTwo"]}
    v = compute(snapshot(heads))  # no rehearsal_ancestry sidecar at all
    assert v["verdict"] == "stale"
    assert any("source moved since rehearsal (LibOne; ancestry unproven)" in r
               for r in v["stale_reasons"])
    assert v["validation_ahead"] == {}


def test_a_record_for_a_different_head_or_a_failed_compare_is_not_proof():
    heads = {"LibOne": "c" * 40, "LibTwo": REHEARSED["LibTwo"]}
    side = ra.collect(snapshot(heads), OWNERS, fake_api({"LibOne": ("ahead", 1)}))
    moved_again = {"LibOne": "e" * 40, "LibTwo": REHEARSED["LibTwo"]}
    assert compute(snapshot(moved_again, side))["verdict"] == "stale"
    failed = ra.collect(snapshot(heads), OWNERS, lambda path: {})
    assert failed["repos"]["LibOne"]["error"] == "compare unavailable"
    assert compute(snapshot(heads, failed))["verdict"] == "stale"


def test_main_behind_the_rehearsed_commit_is_not_an_ancestor():
    heads = {"LibOne": "c" * 40, "LibTwo": REHEARSED["LibTwo"]}
    side = ra.collect(snapshot(heads), OWNERS, fake_api({"LibOne": ("behind", 0)}))
    assert compute(snapshot(heads, side))["verdict"] == "stale"


def test_sidecar_is_folded_into_the_snapshot():
    src = (readiness.HEART_HOME / "heart" / "state.py").read_text()
    assert '"rehearsal_ancestry": _read_json_or_default(' in src


def test_check_is_wired_into_the_tick_and_the_cloud_job():
    home = readiness.HEART_HOME
    assert "heart.checks.rehearsal_ancestry" in (home / "heart" / "tick.sh").read_text()
    assert "heart.checks.rehearsal_ancestry" in (
        home / ".github" / "workflows" / "heart-health.yml").read_text()


def test_live_owner_map_is_the_release_gate_libraries():
    assert set(ra.library_owners()) == set(readiness.load_release_gate_names())


def test_sidecar_carries_its_collection_time():
    now = datetime.datetime(2026, 11, 12, tzinfo=datetime.timezone.utc)
    assert ra.collect(snapshot(), OWNERS, fake_api({}), now=now)["checked_at"] == now.isoformat()
