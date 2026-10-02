"""tests/test_publish.py — the distilled dev-box board (pyauto-heart publish).

What matters is the contract with the public repo: only the local-only
families travel, nothing that names a local filesystem path ever leaves the
machine, families with no local observation publish no claim, and the output
round-trips through dashboard's devbox merge.
"""

from __future__ import annotations

import datetime

from heart import dashboard, publish

TS = "2026-06-01T00:00:00+00:00"
NOW = datetime.datetime(2026, 6, 1, 0, 1, 0, tzinfo=datetime.timezone.utc)


# Fixture names are deliberately fake (RepoA, /home/user/...): this file is
# not on the tenant-firewall allowlist, so no instance fact may appear here.
def _snapshot() -> dict:
    return {
        "ts": TS,
        "repos": {},
        "worktree_drift": {
            "ts": TS,
            "orphans": [], "missing": [], "parked": [],
            "dirty": [{"worktree": "task-a", "repo": "RepoA", "dirty_files": 3}],
            "canonical_dirty": [{"repo": "/home/user/code/RepoB",
                                 "dirty_files": 1}],
        },
        "script_timing": {"red_count": 0, "yellow_count": 0, "green_count": 12},
    }


def test_distills_only_local_families_and_observed_ones():
    out = publish.build_devbox_board(_snapshot(), {"verdict": "green", "score": 100}, now=NOW)
    assert set(out["sections"]) <= set(publish.PUBLISH_FAMILIES)
    assert "worktree_drift" in out["sections"]
    assert "script_timing" in out["sections"]
    # families the snapshot never observed publish no claim
    assert "profiling_drift" not in out["sections"]
    assert out["ts"] == TS


def test_no_local_paths_leave_the_machine():
    out = publish.build_devbox_board(_snapshot(), {"verdict": "green", "score": 100}, now=NOW)
    flat = str(out)
    assert "/home/" not in flat
    # the scrub drops the offending detail line, not the whole section
    wd = out["sections"]["worktree_drift"]
    assert any("task-a" in d for d in wd["details"])


def test_round_trips_through_the_devbox_merge():
    out = publish.build_devbox_board(_snapshot(), {"verdict": "green", "score": 100}, now=NOW)
    board = dashboard.build_board(
        {"ts": TS, "repos": {}}, {"verdict": "green", "score": 100},
        unobserved=dashboard.LOCAL_ONLY_FAMILIES, now=NOW, devbox=out,
    )
    sec = {s.key: s for s in board.sections}["worktree_drift"]
    assert sec.state != dashboard.UNOBS
    assert sec.observed_ago and "dev box" in sec.observed_ago


def _round_trip(snapshot, cloud=None):
    public = publish.build_devbox_board(snapshot, {"verdict": "green", "score": 100}, now=NOW)
    board = dashboard.build_board(cloud or {"ts": TS, "repos": {}},
        {"verdict": "green", "score": 100}, unobserved=dashboard.LOCAL_ONLY_FAMILIES,
        now=NOW, devbox=public)
    return public, board


def test_previously_omitted_families_reach_public_inventory_and_sections():
    snapshot = {"ts": TS, "repos": {},
        "manifest_drift": {"ts": TS, "available": True, "checks": {"layout": {"ok": False, "problems": ["undeclared checkout"]}}},
        "required_workflow_drift": {"ts": TS, "available": True, "repos": [{"repo": "RepoA", "state": "yellow"}], "drift_count": 1},
        "url_check": {"ts": TS, "repos": [{"repo": "RepoA", "findings": 1}], "total_findings": 1},
        "version_skew_pypi": {"ts": TS, "workspaces": [{"workspace": "RepoA", "status": "SATISFIABLE"}]}}
    public, board = _round_trip(snapshot)
    local = dashboard.build_board(snapshot, {"verdict": "green", "score": 100}, now=NOW)
    for family in snapshot.keys() - {"ts", "repos"}:
        assert family in public["sections"]
        assert family in {s.key for s in board.sections}
        before = {c["id"]: (c["status"], c["observed_at"]) for c in local.monitoring["checks"] if c["family"] == family}
        after = {c["id"]: (c["status"], c["observed_at"]) for c in board.monitoring["checks"] if c["family"] == family}
        assert before == after
    assert any(c["family"] == "manifest_drift" and c["status"] == "red" for c in board.monitoring["findings"])


def test_cloud_observation_takes_precedence_over_published_failure():
    local = {"ts": TS, "url_check": {"ts": TS, "repos": [{"repo": "RepoA", "findings": 1}], "total_findings": 1}}
    cloud = {"ts": TS, "url_check": {"ts": TS, "repos": [{"repo": "RepoA", "findings": 0}], "total_findings": 0}}
    _, board = _round_trip(local, cloud)
    rows = [c for c in board.monitoring["checks"] if c["family"] == "url_check"]
    assert rows and all(c["status"] == "green" for c in rows)
    assert all(c["source"].startswith("snapshot") for c in rows)


def test_republication_never_refreshes_missing_or_expired_evidence():
    for observed in [None, "2026-05-01T00:00:00+00:00"]:
        snapshot = {"ts": TS, "manifest_drift": {"ts": observed, "available": True, "checks": {"layout": {"ok": True}}}}
        public, board = _round_trip(snapshot)
        assert public["sections"]["manifest_drift"]["observed_at"] == observed
        row = next(c for c in board.monitoring["checks"] if c["id"] == "manifest_drift:coverage")
        assert row["status"] == ("grey" if observed is None else "stale")
        assert row["observed_at"] == observed
        sec = next(s for s in board.sections if s.key == "manifest_drift")
        assert sec.state == dashboard.UNOBS


def test_private_manifest_findings_are_preserved_without_paths():
    snapshot = {"ts": TS, "manifest_drift": {"ts": TS, "available": True,
        "checks": {"layout": {"ok": False, "problems": ["unmapped /home/private/work"]}}}}
    public, board = _round_trip(snapshot)
    assert "/home/private" not in str(public)
    assert any(c["family"] == "manifest_drift" and c["status"] == "red" for c in board.monitoring["findings"])
