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


def _snapshot() -> dict:
    return {
        "ts": TS,
        "repos": {},
        "worktree_drift": {
            "orphans": [], "missing": [], "parked": [],
            "dirty": [{"worktree": "task-a", "repo": "PyAutoFit", "dirty_files": 3}],
            "canonical_dirty": [{"repo": "/home/jammy/Code/PyAutoLabs/PyAutoLens",
                                 "dirty_files": 1}],
        },
        "script_timing": {"red_count": 0, "yellow_count": 0, "green_count": 12},
    }


def test_distills_only_local_families_and_observed_ones():
    out = publish.build_devbox_board(_snapshot(), {"verdict": "green", "score": 100})
    assert set(out["sections"]) <= set(publish.PUBLISH_FAMILIES)
    assert "worktree_drift" in out["sections"]
    assert "script_timing" in out["sections"]
    # families the snapshot never observed publish no claim
    assert "profiling_drift" not in out["sections"]
    assert out["ts"] == TS


def test_no_local_paths_leave_the_machine():
    out = publish.build_devbox_board(_snapshot(), {"verdict": "green", "score": 100})
    flat = str(out)
    assert "/home/" not in flat
    # the scrub drops the offending detail line, not the whole section
    wd = out["sections"]["worktree_drift"]
    assert any("task-a" in d for d in wd["details"])


def test_round_trips_through_the_devbox_merge():
    out = publish.build_devbox_board(_snapshot(), {"verdict": "green", "score": 100})
    board = dashboard.build_board(
        {"ts": TS, "repos": {}}, {"verdict": "green", "score": 100},
        unobserved=dashboard.LOCAL_ONLY_FAMILIES, now=NOW, devbox=out,
    )
    sec = {s.key: s for s in board.sections}["worktree_drift"]
    assert sec.state != dashboard.UNOBS
    assert sec.observed_ago and "dev box" in sec.observed_ago
