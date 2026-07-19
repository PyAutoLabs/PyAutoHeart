"""tests/test_dashboard.py — the one unified renderer.

Covers each fmt, the staleness path, the cloud-only "unobserved check" path,
and — crucially — the *unify invariant*: term/oneline/md/json rendered from one
fixture snapshot must report the SAME verdict/score. The surfaces are all
projections of one :class:`~heart.dashboard.Board`, so they cannot disagree.
"""

from __future__ import annotations

import datetime
import json
import re

import pytest

from heart import dashboard

LIBS = ["PyAutoNerves", "PyAutoFit", "PyAutoArray", "PyAutoGalaxy", "PyAutoLens"]
TS = "2026-06-01T00:00:00+00:00"


def _lib(concl: str = "success", branch: str = "main") -> dict:
    return {
        "ci_status": {"conclusion": concl, "group": "libraries"},
        "repo_state": {"group": "libraries", "branch": branch, "dirty_real": 0, "behind": 0},
    }


def make_snapshot(**overrides) -> dict:
    snap = {
        "ts": TS,
        "repos": {
            **{lib: _lib() for lib in LIBS},
            "autolens_workspace": {
                "ci_status": {"conclusion": "success", "group": "workspaces"},
                "repo_state": {"group": "workspaces", "branch": "main", "dirty_real": 0},
                "open_prs": {"open_count": 1, "max_age_days": 3},
            },
        },
        "script_timing": {"red_count": 0, "yellow_count": 0, "green_count": 10},
        "test_run": {"ready": True, "passed": 100, "failed": 0, "parked_stale_count": 0,
                     "run_label": "2026.6.1"},
        "version_skew": {"workspaces": [{"workspace": "autolens_workspace", "status": "MATCH"}]},
        "validation_report": {
            "release_ready": True, "testpypi_version": "2026.6.1.1.dev100",
            "profile": "release", "stages": {"rehearse": {"status": "pass"},
                                             "integrate": {"status": "pass"}},
            "ts": TS,
        },
    }
    snap.update(overrides)
    return snap


def make_verdict(verdict: str = "green", score: int = 100, **kw) -> dict:
    return {
        "verdict": verdict,
        "score": score,
        "red_reasons": kw.get("red_reasons", []),
        "yellow_reasons": kw.get("yellow_reasons", []),
        "ts": TS,
    }


# A `now` close to the snapshot ts so the board is fresh by default.
FRESH_NOW = datetime.datetime(2026, 6, 1, 0, 1, 0, tzinfo=datetime.timezone.utc)
STALE_NOW = datetime.datetime(2026, 6, 3, 0, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _no_color(monkeypatch):
    # Deterministic, colour-free strings so the extractors below are stable.
    monkeypatch.setenv("NO_COLOR", "1")


# --- each fmt renders --------------------------------------------------------
@pytest.mark.parametrize("fmt", ["term", "oneline", "md", "html", "json"])
def test_each_fmt_renders(fmt):
    out = dashboard.render(make_snapshot(), make_verdict(), fmt=fmt, now=FRESH_NOW)
    assert isinstance(out, str) and out.strip()


def test_json_is_valid_and_carries_verdict():
    out = dashboard.render(make_snapshot(), make_verdict("yellow", 78), fmt="json", now=FRESH_NOW)
    d = json.loads(out)
    assert d["verdict"] == "yellow"
    assert d["score"] == 78
    assert d["schema_version"] == dashboard.SCHEMA_VERSION
    assert d["stale"] is False
    assert any(s["key"] == "libraries" for s in d["sections"])


def test_html_is_self_contained():
    out = dashboard.render(make_snapshot(), make_verdict("red", 30,
                           red_reasons=["PyAutoLens: CI failure"]), fmt="html", now=FRESH_NOW)
    assert out.lstrip().startswith("<!doctype html>")
    assert "RED" in out
    # No external assets (strict-CSP / renders anywhere): no scripts, no remote
    # stylesheets/images, no fetches.
    assert "<script" not in out.lower()
    assert "http://" not in out and "https://" not in out
    assert "src=" not in out and "<link" not in out.lower()


# --- the unify invariant -----------------------------------------------------
def _extract(out: str, fmt: str) -> tuple[str, int]:
    if fmt == "json":
        d = json.loads(out)
        return dashboard._VERDICT_WORD[d["verdict"]], int(d["score"])
    word = re.search(r"\b(RED|YELLOW|GREEN)\b", out).group(1)
    score = int(re.search(r"(?:score |·\s*|[A-Z] )(\d+)", out).group(1))
    return word, score


@pytest.mark.parametrize("verdict,score", [("green", 100), ("yellow", 64), ("red", 22)])
def test_unify_invariant_verdict_and_score_agree(verdict, score):
    snap = make_snapshot()
    v = make_verdict(verdict, score,
                     red_reasons=["a blocker"] if verdict == "red" else [],
                     yellow_reasons=["a warning"] if verdict != "green" else [])
    seen = set()
    for fmt in ("term", "oneline", "md", "json"):
        out = dashboard.render(snap, v, fmt=fmt, now=FRESH_NOW)
        seen.add(_extract(out, fmt))
    # All surfaces must extract to exactly one (verdict-word, score) pair.
    assert seen == {(dashboard._VERDICT_WORD[verdict], score)}


# --- staleness path ----------------------------------------------------------
def test_stale_board_flags_itself_in_every_fmt():
    snap = make_snapshot()
    v = make_verdict()
    board = dashboard.build_board(snap, v, now=STALE_NOW)
    assert board.stale is True

    term = dashboard.render(snap, v, fmt="term", now=STALE_NOW)
    assert "stale" in term.lower()
    one = dashboard.render(snap, v, fmt="oneline", now=STALE_NOW)
    assert "stale" in one.lower()
    md = dashboard.render(snap, v, fmt="md", now=STALE_NOW)
    assert "stale" in md.lower()
    d = json.loads(dashboard.render(snap, v, fmt="json", now=STALE_NOW))
    assert d["stale"] is True


def test_fresh_board_not_stale():
    board = dashboard.build_board(make_snapshot(), make_verdict(), now=FRESH_NOW)
    assert board.stale is False


# --- cloud-only-honest / unobserved path ------------------------------------
def test_cloud_marks_local_only_checks_unobserved():
    snap = make_snapshot()
    board = dashboard.build_board(snap, make_verdict(),
                                  unobserved=dashboard.LOCAL_ONLY_FAMILIES, now=FRESH_NOW)
    by_key = {s.key: s for s in board.sections}
    for fam in ("worktree_drift", "script_timing", "test_run", "version_skew"):
        assert by_key[fam].state == dashboard.UNOBS
        assert "not observed here" in by_key[fam].summary
    # repo_state is folded into the library rows; those rows must not claim a
    # green working tree the cloud never saw.
    libs = by_key["libraries"]
    assert any("repo state n/a here" in d for d in libs.details)


def test_cloud_json_still_reports_observed_checks():
    snap = make_snapshot()
    out = dashboard.render(snap, make_verdict("yellow", 70),
                           fmt="json", unobserved=dashboard.LOCAL_ONLY_FAMILIES, now=FRESH_NOW)
    d = json.loads(out)
    states = {s["key"]: s["state"] for s in d["sections"]}
    # CI (observed) still present on library rows; version_skew marked unobserved.
    assert states["version_skew"] == dashboard.UNOBS
    assert states["libraries"] in (dashboard.OK, dashboard.WARN, dashboard.FAIL)


def test_local_board_does_not_mark_anything_unobserved():
    board = dashboard.build_board(make_snapshot(), make_verdict(), now=FRESH_NOW)
    assert all(s.state != dashboard.UNOBS for s in board.sections)


# --- degradation / robustness ------------------------------------------------
def test_empty_snapshot_never_raises():
    for fmt in ("term", "oneline", "md", "html", "json"):
        out = dashboard.render({}, {}, fmt=fmt, now=FRESH_NOW)
        assert isinstance(out, str)


def test_no_cache_age_is_none():
    board = dashboard.build_board({"ts": ""}, {}, now=FRESH_NOW)
    assert board.age_seconds is None
    assert board.stale is False


def test_unknown_fmt_raises():
    with pytest.raises(ValueError):
        dashboard.render(make_snapshot(), make_verdict(), fmt="nope")


# --- badge -------------------------------------------------------------------
@pytest.mark.parametrize("verdict,color", [("green", "brightgreen"), ("yellow", "yellow"),
                                           ("stale", "blue"), ("red", "red")])
def test_badge_endpoint_colour(verdict, color):
    board = dashboard.build_board(make_snapshot(), make_verdict(verdict, 50), now=FRESH_NOW)
    b = dashboard.badge_endpoint(board)
    assert b["schemaVersion"] == 1
    assert b["label"] == "health"
    assert b["color"] == color
    assert dashboard._VERDICT_WORD[verdict] in b["message"]


# --- readiness block is shared (one source of truth) -------------------------
def test_readiness_block_matches_readiness_module():
    from heart import readiness
    v = make_verdict("red", 40, red_reasons=["boom"])
    assert readiness.render_block(v) == dashboard.render_readiness_block(v)


# --- CLI main() no-cache degradation (the I/O shell) -------------------------
def test_main_no_cache_oneline_degrades_cleanly(monkeypatch, tmp_path, capsys):
    from heart import state
    monkeypatch.setattr(state, "HEART_STATE_FILE", tmp_path / "absent.json")
    rc = dashboard.main(["--oneline"])
    out = capsys.readouterr().out
    assert rc == 0                       # must never error the prompt
    assert "no fresh state" in out


def test_main_no_cache_badge_emits_unknown_payload(monkeypatch, tmp_path, capsys):
    # Regression: --badge must reach the no-cache fallback (fmt selection must
    # include "badge"), not exit 2 with the generic "no cache" error.
    from heart import state
    monkeypatch.setattr(state, "HEART_STATE_FILE", tmp_path / "absent.json")
    rc = dashboard.main(["--badge"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["schemaVersion"] == 1
    assert payload["message"] == "unknown"
    assert payload["color"] == "lightgrey"
