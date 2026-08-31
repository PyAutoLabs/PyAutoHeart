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
        "version_skew": {"workspaces": [{"workspace": "autolens_workspace", "status": "OK"}]},
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
    # The header links the markdown twin and the repository front door.
    assert '<a href="dashboard.md">markdown version</a>' in out
    assert ('<a href="https://github.com/PyAutoLabs/PyAutoHeart/blob/main/'
            'README.md">GitHub Page</a>') in out
    # No external ASSETS (renders anywhere, loads nothing remote): no src=, no
    # <link>, no fetches. Inline <script> is allowed — the one-tap 📋 copy
    # buttons need the clipboard API — and outbound <a href> links are
    # navigation, not asset loads.
    assert "src=" not in out and "<link" not in out.lower()
    assert "fetch(" not in out and "XMLHttpRequest" not in out
    lowered = out.lower()
    assert '<script src' not in lowered and "import(" not in out
    # every http(s) URL sits in an anchor href, never in a loadable attribute
    for m in re.finditer(r"(?:http|https)://", out):
        before = out[max(0, m.start() - 30):m.start()]
        assert 'href="' in before or "href='" in before, f"non-href URL at {m.start()}"


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


def test_find_links_install_pass_is_warned_as_development_only():
    snap = make_snapshot(verify_install={
        "ready": True,
        "index": "find-links",
        "ts": TS,
        "checks": [{"check": "B", "status": "PASS"}],
    })

    board = dashboard.build_board(snap, make_verdict("stale", 90), now=FRESH_NOW)
    section = next(s for s in board.sections if s.key == "verify_install")

    assert section.state == dashboard.WARN
    assert "development-only (find-links" in section.summary


def test_test_run_cloud_conclusion_only_hides_fabricated_zeros():
    # The legacy cloud-only sidecar shape: red conclusion, no count evidence.
    snap = make_snapshot(test_run={
        "ready": False, "passed": 0, "failed": 0, "skipped": 0,
        "source": "cloud", "run_label": "cloud#30516167217"})

    board = dashboard.build_board(snap, make_verdict("yellow", 70), now=FRESH_NOW)
    section = next(s for s in board.sections if s.key == "test_run")

    assert section.state == dashboard.FAIL
    assert "0p" not in section.summary
    assert "counts not ingested" in section.summary
    assert "cloud#30516167217" in section.summary


def test_test_run_failing_scripts_listed_in_details():
    snap = make_snapshot(test_run={
        "ready": False, "passed": 585, "failed": 2, "skipped": 91,
        "counts_measured": True, "run_label": "cloud#1",
        "failing_scripts": [
            {"project": "autogalaxy", "script": "scripts/interferometer/start_here.py",
             "status": "failed"},
        ]})

    board = dashboard.build_board(snap, make_verdict("yellow", 70), now=FRESH_NOW)
    section = next(s for s in board.sections if s.key == "test_run")

    assert "585p" in section.summary and "2f" in section.summary
    assert any("autogalaxy scripts/interferometer/start_here.py" in d
               for d in section.details)


def test_release_install_pass_names_the_index():
    snap = make_snapshot(verify_install={
        "ready": True,
        "index": "testpypi",
        "ts": TS,
        "checks": [{"check": "B", "status": "PASS"}],
    })

    board = dashboard.build_board(snap, make_verdict(), now=FRESH_NOW)
    section = next(s for s in board.sections if s.key == "verify_install")

    assert section.state == dashboard.OK
    assert "passed (testpypi" in section.summary


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


# --- release-validation row + the stale axis on the machine surface ---------


def _section(board, key):
    return next(s for s in board.sections if s.key == key)


def test_validation_incomplete_renders_warn_not_fail():
    """`incomplete` is an evidence gap; a FAIL row would contradict the header.

    The row is a projection of the same report the readiness gate reads, so it
    has to make the same fail/incomplete distinction — otherwise a stale verdict
    is rendered beside a red-looking validation row.
    """
    snap = make_snapshot(validation_report={
        "release_ready": False, "validation_outcome": "incomplete",
        "testpypi_version": "2026.6.1.1.dev100", "profile": "release",
        "stages": {"integrate": {"status": "pass"}}, "ts": TS,
    })
    board = dashboard.build_board(snap, make_verdict("stale", 85), now=FRESH_NOW)
    assert _section(board, "release_validation").state == dashboard.WARN


def test_validation_fail_still_renders_fail():
    snap = make_snapshot(validation_report={
        "release_ready": False, "validation_outcome": "fail",
        "testpypi_version": "2026.6.1.1.dev100", "profile": "release",
        "stages": {"integrate": {"status": "fail"}}, "ts": TS,
    })
    board = dashboard.build_board(snap, make_verdict("red", 45), now=FRESH_NOW)
    assert _section(board, "release_validation").state == dashboard.FAIL


def test_validation_legacy_false_without_discriminator_renders_fail():
    snap = make_snapshot(validation_report={
        "release_ready": False,
        "testpypi_version": "2026.6.1.1.dev100", "profile": "release",
        "stages": {"integrate": {"status": "pass"}}, "ts": TS,
    })
    board = dashboard.build_board(snap, make_verdict("red", 45), now=FRESH_NOW)
    assert _section(board, "release_validation").state == dashboard.FAIL


def test_machine_surface_carries_stale_reasons():
    """The Health Agent and mobile read `to_dict()`.

    Without this key a reason re-classified from the red axis to the stale one
    would vanish from those surfaces entirely rather than being re-reported.
    """
    verdict = make_verdict("stale", 85)
    verdict["stale_reasons"] = ["release validation incomplete: no rehearsal for current source"]
    board = dashboard.build_board(make_snapshot(), verdict, now=FRESH_NOW)
    payload = dashboard.to_dict(board)
    assert "stale_reasons" in payload
    assert payload["stale_reasons"] == verdict["stale_reasons"]


def test_validation_row_mirrors_the_readiness_normaliser():
    """The row must not contradict the header verdict.

    Both now go through `validate.report_outcome`; when they each re-derived it
    inline, readiness graded these RED while the row rendered a green
    `release_ready`.
    """
    for vr in (
        {"release_ready": False, "validation_outcome": "pass",     # contradictory
         "testpypi_version": "1", "profile": "release",
         "stages": {"integrate": {"status": "pass"}}, "ts": TS},
        {"release_ready": True, "validation_outcome": "PASS",      # malformed
         "testpypi_version": "1", "profile": "release",
         "stages": {"integrate": {"status": "pass"}}, "ts": TS},
    ):
        board = dashboard.build_board(make_snapshot(validation_report=vr),
                                      make_verdict("red", 45), now=FRESH_NOW)
        assert _section(board, "release_validation").state == dashboard.FAIL


def test_malformed_stages_does_not_break_the_board():
    vr = {"release_ready": False, "validation_outcome": "fail",
          "testpypi_version": "1", "profile": "release",
          "stages": [{"stage": "integrate"}], "ts": TS}
    board = dashboard.build_board(make_snapshot(validation_report=vr),
                                  make_verdict("red", 45), now=FRESH_NOW)
    assert _section(board, "release_validation").state == dashboard.FAIL


def test_malformed_stage_entry_does_not_break_the_board():
    """The container was guarded; each ENTRY needs it too."""
    vr = {"release_ready": False, "validation_outcome": "fail",
          "testpypi_version": "1", "profile": "release",
          "stages": {"integrate": []}, "ts": TS}
    board = dashboard.build_board(make_snapshot(validation_report=vr),
                                  make_verdict("red", 45), now=FRESH_NOW)
    assert _section(board, "release_validation").state == dashboard.FAIL


# --- CI query failure is reported as itself, not as a pending run ----------

def test_ci_fragment_flags_failed_query_distinctly():
    """Regression: a broken `gh` call rendered as "CI in_progress" on every
    repo at once, so a dead query and a genuine red looked identical."""
    state, text = dashboard._ci_fragment(
        {"conclusion": "", "status": "unavailable", "error": "unknown flag: --branch"}
    )
    assert "unavailable" in text.lower()
    assert "in_progress" not in text


def test_ci_fragment_still_reports_real_pending():
    state, text = dashboard._ci_fragment({"conclusion": "", "status": "in_progress"})
    assert text == "CI in_progress"


def test_ci_fragment_success_and_failure_unchanged():
    assert dashboard._ci_fragment({"conclusion": "success"})[1] == "CI ✓"
    assert "Smoke Tests" in dashboard._ci_fragment(
        {"conclusion": "failure", "workflow": "Smoke Tests"}
    )[1]


# --- actionable board: blockers, actions, md-brief, devbox merge -------------
# The failing-run URL is fixture data; the repo URL is DERIVED from the
# declared config surface (dashboard.REPO_OWNERS), so no owner literal
# appears here — the tenant firewall keeps instance facts out of test code.
RUN_URL = "https://ci.invalid/actions/runs/99"
WS_REPO_URL = (
    f"https://github.com/{dashboard.REPO_OWNERS['autolens_workspace']}/autolens_workspace"
)


def _failing_snapshot():
    snap = make_snapshot()
    snap["repos"]["autolens_workspace"]["ci_status"] = {
        "conclusion": "failure", "workflow": "Smoke Tests", "group": "workspaces",
        "url": RUN_URL,
    }
    return snap


def test_blockers_are_structured_with_links_and_prompts():
    v = make_verdict("red", 45,
                     red_reasons=["autolens_workspace: Smoke Tests failure on main"])
    board = dashboard.build_board(_failing_snapshot(), v, now=FRESH_NOW)
    (b,) = board.blockers
    assert b["severity"] == "red"
    assert b["repo"] == "autolens_workspace"
    assert b["repo_url"] == WS_REPO_URL
    assert b["run_url"] == RUN_URL
    assert b["prompt"].startswith("/bug Heart board: autolens_workspace")
    assert b["run_url"] in b["prompt"]


def test_html_carries_copy_buttons_and_run_links():
    v = make_verdict("red", 45,
                     red_reasons=["autolens_workspace: Smoke Tests failure on main"])
    out = dashboard.render(_failing_snapshot(), v, fmt="html", now=FRESH_NOW)
    assert "data-cmd=" in out  # the shared copy handler's payload hook
    assert "/bug Heart board: autolens_workspace" in out
    assert RUN_URL in out
    # the failing repo group row links the run too
    assert "autolens_workspace run" in out


def test_md_links_blockers_and_collapses_prompts():
    v = make_verdict("red", 45,
                     red_reasons=["autolens_workspace: Smoke Tests failure on main"])
    out = dashboard.render(_failing_snapshot(), v, fmt="md", now=FRESH_NOW)
    assert f"[autolens_workspace]({WS_REPO_URL})" in out
    assert f"([run]({RUN_URL}))" in out
    assert "<details>" in out and "/bug Heart board:" in out


def test_md_brief_is_a_strip_not_a_table():
    v = make_verdict("red", 45,
                     red_reasons=["autolens_workspace: Smoke Tests failure on main"])
    out = dashboard.render(_failing_snapshot(), v, fmt="md-brief", now=FRESH_NOW)
    assert "**RED**" in out
    assert "[autolens_workspace](" in out
    assert dashboard.PAGES_URL in out
    assert "| Check |" not in out  # no table — the Pages board carries it
    assert "snapshot" not in out   # no timestamp clutter — the board has it
    assert not out.startswith("#")  # the README supplies the section heading


def test_md_brief_is_one_line_unless_something_is_wrong():
    # GREEN and STALE both collapse to the single verdict+link line: evidence
    # gaps are the board's business, not the README's.
    green = dashboard.render(make_snapshot(), make_verdict(), fmt="md-brief",
                             now=FRESH_NOW)
    assert "\n" not in green and dashboard.PAGES_URL in green
    stale_v = {"verdict": "stale", "score": 65,
               "stale_reasons": ["install verification not run"], "ts": TS}
    stale = dashboard.render(make_snapshot(), stale_v, fmt="md-brief", now=FRESH_NOW)
    assert "\n" not in stale
    assert "install verification" not in stale


def test_failing_repo_row_link_carries_its_own_prompt():
    v = make_verdict("red", 45,
                     red_reasons=["autolens_workspace: Smoke Tests failure on main"])
    board = dashboard.build_board(_failing_snapshot(), v, now=FRESH_NOW)
    ws = {s.key: s for s in board.sections}["workspaces"]
    (link,) = ws.links
    assert link["prompt"].startswith("/bug Heart board: autolens_workspace Smoke Tests")
    assert RUN_URL in link["prompt"]
    html = dashboard.render(_failing_snapshot(), v, fmt="html", now=FRESH_NOW)
    # the row-level 📋 renders beside the run link, not only in the blockers
    assert html.count("/bug Heart board: autolens_workspace") >= 2


def test_unobserved_rows_carry_watch_line_and_observe_action():
    out = dashboard.build_board(make_snapshot(), make_verdict(),
                                unobserved=dashboard.LOCAL_ONLY_FAMILIES, now=FRESH_NOW)
    sec = {s.key: s for s in out.sections}["worktree_drift"]
    assert "not observed here" in sec.summary
    assert any("watches" in d for d in sec.details)
    assert sec.action["payload"] == "pyauto-heart tick && pyauto-heart publish"


def _devbox(ts: str) -> dict:
    return {"schema_version": 1, "ts": ts, "sections": {
        "worktree_drift": {"state": "warn",
                           "summary": "2 orphan dir(s) (clean)",
                           "details": ["wt-a: clean", "wt-b: clean"]}}}


def test_devbox_merge_fills_fresh_and_expires_stale():
    v = make_verdict()
    fresh = dashboard.build_board(make_snapshot(), v,
                                  unobserved=dashboard.LOCAL_ONLY_FAMILIES,
                                  now=FRESH_NOW, devbox=_devbox(TS))
    sec = {s.key: s for s in fresh.sections}["worktree_drift"]
    assert sec.state == dashboard.WARN
    assert sec.summary == "2 orphan dir(s) (clean)"
    assert "on the dev box" in sec.observed_ago

    old_ts = "2026-05-01T00:00:00+00:00"  # 31 days before FRESH_NOW
    stale = dashboard.build_board(make_snapshot(), v,
                                  unobserved=dashboard.LOCAL_ONLY_FAMILIES,
                                  now=FRESH_NOW, devbox=_devbox(old_ts))
    sec = {s.key: s for s in stale.sections}["worktree_drift"]
    assert sec.state == dashboard.UNOBS
    assert "dev box last looked" in sec.summary


def test_devbox_never_overrides_an_observed_row():
    v = make_verdict()
    board = dashboard.build_board(make_snapshot(), v, unobserved=(),
                                  now=FRESH_NOW, devbox=_devbox(TS))
    by_key = {s.key: s for s in board.sections}
    # Locally the snapshot has no worktree_drift slice, so that section is
    # simply absent — the devbox dict must NOT conjure one up...
    assert "worktree_drift" not in by_key
    # ...and script_timing IS observed locally and must keep its own summary
    # even though a devbox dict is present.
    st = by_key["script_timing"]
    assert st.observed_ago is None
    assert "within baseline" in st.summary


def test_json_v2_carries_blockers_and_actions():
    v = make_verdict("red", 45,
                     red_reasons=["autolens_workspace: Smoke Tests failure on main"])
    out = dashboard.render(_failing_snapshot(), v, fmt="json",
                           unobserved=dashboard.LOCAL_ONLY_FAMILIES, now=FRESH_NOW)
    d = json.loads(out)
    assert d["schema_version"] == dashboard.SCHEMA_VERSION
    assert d["blockers"][0]["prompt"].startswith("/bug ")
    unobs = [s for s in d["sections"] if s["state"] == "unobserved"]
    assert unobs and all(s["action"]["payload"].startswith("pyauto-heart") for s in unobs)


# --- the ⏱ performance surface: CI wall-clock + the NO_RUN census ------------
# Fake repo/workflow names (the tenant firewall): the real ones live in
# config/repos.yaml, which is the declared surface, never in test data.
EVENT_URL = "https://ci.invalid/actions/runs/9"
GATE_URL = "https://ci.invalid/RepoA/actions"
EVENT_PROMPT = (
    "/bug kill timer: RepoA Gate One timed_out after 18000s on main "
    f"— {EVENT_URL}"
)
GATE_PROMPT = (
    "/bug smoke gate RepoA: Gate One median wall-clock rose 600s → 900s vs its "
    f"recent history — {GATE_URL}"
)
ROW_PROMPT = (
    "/bug no_run: RepoA imaging/x.py SLOW since 2026-07-14 with no measurement — "
    "retime against the real cap, then fix it or delete the marker"
)


def _ci_timing_slice(*, events=True, warn=True):
    return {
        "ts": TS,
        "gates": [
            {"repo": "RepoA", "workflow": "Gate One", "median_s": 900.0,
             "pr_median_s": 960.0, "queue_median_s": 12.0, "max_s": 1200.0,
             "runs_counted": 17, "conclusions": {"success": 17}, "superseded": 2,
             "actions_url": GATE_URL, "baseline_s": 600.0, "ratio": 1.5,
             "delta_s": 300.0, "state": "warn" if warn else "ok",
             "prompt": GATE_PROMPT if warn else None},
            {"repo": "RepoB", "workflow": "Gate Two", "median_s": 45.0,
             "pr_median_s": None, "queue_median_s": 3.0, "max_s": 60.0,
             "runs_counted": 4, "conclusions": {"success": 4}, "superseded": 0,
             "actions_url": "https://ci.invalid/RepoB/actions", "baseline_s": None,
             "ratio": None, "delta_s": None, "state": "ok", "prompt": None},
        ],
        "history": [
            {"date": "2026-08-22", "gates": {"RepoA/Gate One": {"p50_s": 600.0, "runs": 10}}},
            {"date": "2026-08-23", "gates": {"RepoA/Gate One": {"p50_s": 700.0, "runs": 12}}},
            {"date": "2026-08-24", "gates": {"RepoA/Gate One": {"p50_s": 900.0, "runs": 17}}},
        ],
        "events": ([{"kind": "timed_out", "repo": "RepoA", "workflow": "Gate One",
                     "run_url": EVENT_URL, "duration_s": 18000.0,
                     "head_branch": "main", "at": TS, "prompt": EVENT_PROMPT}]
                   if events else []),
        "errors": [],
    }


def _no_run_slice():
    return {
        "ts": TS,
        "totals": {"slow": 21, "needs_fix": 4, "permanent": 46,
                   "unmeasured_slow": 7, "repos": 11, "repos_present": 10},
        "repos": [
            {"repo": "RepoA", "present": True, "slow": 3, "needs_fix": 1,
             "permanent": 5, "unmeasured_slow": 2},
            {"repo": "RepoB", "present": False, "slow": 0, "needs_fix": 0,
             "permanent": 0, "unmeasured_slow": 0},
        ],
        "rows": [
            {"repo": "RepoA", "entry": "imaging/x.py", "marker": "SLOW",
             "date": "2026-07-14", "reason": "too slow", "measured": False,
             "prompt": ROW_PROMPT},
            {"repo": "RepoA", "entry": "imaging/y.py", "marker": "NEEDS_FIX",
             "date": "", "reason": "raises", "measured": False,
             "prompt": "/bug no_run: RepoA imaging/y.py NEEDS_FIX since unknown date"},
        ],
    }


def _perf_snapshot(**kw):
    return make_snapshot(ci_timing=_ci_timing_slice(**kw), no_run_census=_no_run_slice())


def test_sparkline_is_pure_and_needs_two_points():
    assert dashboard.sparkline([]) == ""
    assert dashboard.sparkline([5]) == ""              # one point is not a trend
    assert dashboard.sparkline([1, 2, 3, 4]) == "▁▃▆█"
    assert dashboard.sparkline([7, 7, 7]) == "▅▅▅"     # flat, never a div-by-zero
    assert dashboard.sparkline([1, "x", 3]) == "▁█"    # garbage dropped


def test_both_performance_sections_render_in_every_fmt():
    snap = _perf_snapshot()
    v = make_verdict()
    board = dashboard.build_board(snap, v, now=FRESH_NOW)
    keys = [s.key for s in board.sections]
    assert "ci_timing" in keys and "no_run_census" in keys
    # ...and they sit after the Test run row, where the spec puts them.
    assert keys.index("ci_timing") > keys.index("test_run")
    for fmt in ("term", "md", "html", "json"):
        # term upper-cases its row titles, so compare case-insensitively.
        out = dashboard.render(snap, v, fmt=fmt, now=FRESH_NOW).lower()
        assert "ci wall-clock" in out and "no_run census" in out


def test_ci_timing_section_states_and_summary():
    sec = _section(dashboard.build_board(_perf_snapshot(), make_verdict(), now=FRESH_NOW),
                   "ci_timing")
    assert sec.state == dashboard.FAIL          # an event is a hard row
    assert "2 gates" in sec.summary
    assert "slowest RepoA Gate One 15m" in sec.summary
    assert "1 slowed" in sec.summary and "1 hang event" in sec.summary
    # coverage beside time, always — and the gate's own history as a sparkline
    assert sec.details[0].startswith("RepoA Gate One  p50 15m  max 20m  (17 runs)")
    assert sec.details[0].rstrip().endswith("▁▃█")


def test_ci_timing_without_events_is_warn_when_a_gate_slowed():
    sec = _section(dashboard.build_board(_perf_snapshot(events=False), make_verdict(),
                                         now=FRESH_NOW), "ci_timing")
    assert sec.state == dashboard.WARN
    # exactly one slowed gate has no run URL of its own → its prompt rides the row
    assert sec.action["payload"] == GATE_PROMPT


def test_ci_timing_all_quiet_is_ok():
    sec = _section(dashboard.build_board(_perf_snapshot(events=False, warn=False),
                                         make_verdict(), now=FRESH_NOW), "ci_timing")
    assert sec.state == dashboard.OK
    assert sec.action is None and sec.links == []


def test_ci_timing_errors_are_warn_not_silence():
    slice_ = _ci_timing_slice(events=False, warn=False)
    slice_["errors"] = [{"repo": "RepoC", "error": "gh api exited 1"}]
    board = dashboard.build_board(make_snapshot(ci_timing=slice_), make_verdict(),
                                  now=FRESH_NOW)
    sec = _section(board, "ci_timing")
    assert sec.state == dashboard.WARN
    assert "1 unavailable" in sec.summary


def test_ci_timing_links_are_events_with_their_own_prompts():
    sec = _section(dashboard.build_board(_perf_snapshot(), make_verdict(), now=FRESH_NOW),
                   "ci_timing")
    (link,) = sec.links
    assert link["label"] == "RepoA timed_out"
    assert link["url"] == EVENT_URL
    assert link["prompt"] == EVENT_PROMPT


def test_no_run_section_summary_details_and_action():
    sec = _section(dashboard.build_board(_perf_snapshot(), make_verdict(), now=FRESH_NOW),
                   "no_run_census")
    assert sec.state == dashboard.WARN
    assert sec.summary == ("21 SLOW (7 unmeasured) / 4 NEEDS_FIX / 46 permanent "
                           "across 10 workspaces")
    assert sec.details[0] == "RepoA: imaging/x.py SLOW 2026-07-14"
    assert any("no no_run.yaml: RepoB" in d for d in sec.details)
    assert sec.links == []
    assert sec.action == {"label": "copy the fix prompt", "payload": ROW_PROMPT}


def test_no_run_only_permanent_is_info_not_a_todo():
    slice_ = {"ts": TS,
              "totals": {"slow": 0, "needs_fix": 0, "permanent": 46,
                         "unmeasured_slow": 0, "repos": 10, "repos_present": 10},
              "repos": [], "rows": []}
    sec = _section(dashboard.build_board(make_snapshot(no_run_census=slice_),
                                         make_verdict(), now=FRESH_NOW), "no_run_census")
    assert sec.state == dashboard.INFO
    assert sec.action is None


def test_no_run_measured_slow_only_is_ok():
    slice_ = {"ts": TS,
              "totals": {"slow": 3, "needs_fix": 0, "permanent": 5,
                         "unmeasured_slow": 0, "repos": 2, "repos_present": 2},
              "repos": [], "rows": []}
    sec = _section(dashboard.build_board(make_snapshot(no_run_census=slice_),
                                         make_verdict(), now=FRESH_NOW), "no_run_census")
    assert sec.state == dashboard.OK


def test_performance_rows_are_cloud_observed_not_local_only():
    """Both slices come from the GitHub API, so the cloud job measures them
    first-hand — marking them "not observed here" would be a lie."""
    assert "ci_timing" not in dashboard.LOCAL_ONLY_FAMILIES
    assert "no_run_census" not in dashboard.LOCAL_ONLY_FAMILIES
    board = dashboard.build_board(_perf_snapshot(), make_verdict(),
                                  unobserved=dashboard.LOCAL_ONLY_FAMILIES, now=FRESH_NOW)
    assert _section(board, "ci_timing").state == dashboard.FAIL
    assert _section(board, "no_run_census").state == dashboard.WARN


def test_board_json_carries_the_performance_block_with_prompts_verbatim():
    """The contract the Brain board consumes: every actionable row carries its
    own ready-to-paste prompt, written by the producer, never re-derived."""
    d = json.loads(dashboard.render(_perf_snapshot(), make_verdict(), fmt="json",
                                    now=FRESH_NOW))
    perf = d["performance"]
    assert perf["schema"] == 1
    gate = next(g for g in perf["gates"] if g["state"] == "warn")
    assert {"repo", "workflow", "median_s", "pr_median_s", "max_s", "runs_counted",
            "state", "prompt", "actions_url", "spark"} == set(gate)
    assert gate["prompt"] == GATE_PROMPT          # verbatim, not re-derived
    assert gate["spark"] == "▁▃█"
    (event,) = perf["events"]
    assert event["prompt"] == EVENT_PROMPT
    assert event["run_url"] == EVENT_URL and event["repo"] == "RepoA"
    assert perf["no_run"]["totals"]["unmeasured_slow"] == 7
    assert perf["no_run"]["rows"][0]["prompt"] == ROW_PROMPT
    assert len(perf["history"]) == 3
    # the performance block is purely additive — it bumps nothing on its own.
    assert d["schema_version"] == dashboard.SCHEMA_VERSION


def test_performance_no_run_rows_are_capped_at_ten():
    slice_ = _no_run_slice()
    slice_["rows"] = [dict(slice_["rows"][0], entry=f"s{i}.py") for i in range(25)]
    board = dashboard.build_board(make_snapshot(no_run_census=slice_), make_verdict(),
                                  now=FRESH_NOW)
    assert len(board.performance["no_run"]["rows"]) == 10


def test_html_carries_the_event_prompt_in_a_data_copy_attribute():
    out = dashboard.render(_perf_snapshot(), make_verdict(), fmt="html", now=FRESH_NOW)
    assert f'data-cmd="{_html_escape(EVENT_PROMPT)}"' in out
    assert EVENT_URL in out


def _html_escape(text: str) -> str:
    import html as _h
    return _h.escape(text, quote=True)


def test_empty_slices_mean_no_sections_and_no_performance_block():
    """An ABSENT block is honest; an empty one would imply "measured, nothing
    there" — the distinction a consumer needs."""
    board = dashboard.build_board(make_snapshot(), make_verdict(), now=FRESH_NOW)
    keys = [s.key for s in board.sections]
    assert "ci_timing" not in keys and "no_run_census" not in keys
    assert board.performance is None
    assert "performance" not in json.loads(
        dashboard.render(make_snapshot(), make_verdict(), fmt="json", now=FRESH_NOW))


def test_malformed_performance_slices_never_break_the_board():
    for snap in (make_snapshot(ci_timing="nope", no_run_census=[]),
                 make_snapshot(ci_timing={"gates": "nope", "events": None}),
                 make_snapshot(no_run_census={"rows": [None, "x"], "totals": None})):
        for fmt in ("term", "md", "html", "json"):
            assert isinstance(dashboard.render(snap, make_verdict(), fmt=fmt,
                                               now=FRESH_NOW), str)


def test_html_wears_the_shared_family_theme():
    # The look is the Brain's `board/_theme.py`, not a stylesheet copied in
    # here: the page must carry this board's hero (mark, wordmark, tagline)
    # and its accent, or it has silently fallen out of the family.
    t = dashboard.theme()
    out = dashboard.render(_failing_snapshot(), make_verdict("red", 45),
                           fmt="html", now=FRESH_NOW)
    assert t.MARKS[dashboard.BOARD_KEY] in out
    assert t.ORGANS[dashboard.BOARD_KEY]["tagline"] in out
    assert t.ORGANS[dashboard.BOARD_KEY]["ink_dark"] in out
    assert "#58a6ff" not in out  # the old hard-coded GitHub blue


def test_a_long_out_link_label_cannot_push_the_page_sideways():
    """The out-links carry DATA in their labels (`<repo> run`), and this org's
    longest repo name is 36 characters. Under `white-space:nowrap` that was a
    single unbreakable ~500px word: it set the summary column's min-content
    width and scrolled the whole board sideways on a phone (a 375px viewport
    measured 521px). Short labels have no wrap opportunity to take, so nothing
    is lost by letting them break."""
    out = dashboard.render(
        _failing_snapshot() if "_failing_snapshot" in globals() else make_snapshot(),
        make_verdict("red", 40, red_reasons=["RepoA: CI failure"]),
        fmt="html", now=FRESH_NOW)
    rule = re.search(r"a\.out\{[^}]*\}", out).group(0)
    assert "nowrap" not in rule


# --- evidence gaps carry the check that closes them --------------------------
#
# STALE is the board's steady state, so a gap that only names itself leaves the
# reader with nothing to do. Each row carries its own remedy (looked up by the
# readiness gate key, never sniffed from the sentence), and the tier carries one
# plan that closes all of them at once.

GAPS = ["install verification not run",
        "test run status unknown (no report.json)"]


def _stale_verdict(reasons, keys, score=80):
    return {"verdict": "stale", "score": score, "ts": TS,
            "red_reasons": [], "yellow_reasons": [], "stale_reasons": list(reasons),
            "stale_details": [{"text": t, "key": k} for t, k in zip(reasons, keys)]}


def test_each_gap_carries_the_command_that_closes_it():
    v = _stale_verdict(GAPS, ["install_unknown", "test_unknown"])
    board = dashboard.build_board(make_snapshot(), v, now=FRESH_NOW)
    install, test_run = [b for b in board.blockers if b["severity"] == "stale"]

    assert install["command"] == dashboard.VERIFY_INSTALL_CMD
    assert test_run["command"] == dashboard.TICK_CMD
    # the prompt names the check AND the gap it closes — not the sentence back
    assert dashboard.VERIFY_INSTALL_CMD in install["prompt"]
    assert GAPS[0] in install["prompt"]
    assert install["prompt"].startswith("/health")
    # STALE's rule survives the trip to the chip
    assert "never change code" in install["prompt"]
    assert not any(b["prompt"].startswith("/bug") for b in board.blockers)


def test_a_gap_needing_a_conversation_offers_no_command():
    v = _stale_verdict(["no release validation for current source"], ["validation_absent"])
    board = dashboard.build_board(make_snapshot(), v, now=FRESH_NOW)
    (gap,) = board.blockers

    assert gap["command"] is None          # the Heart never dispatches a rehearsal
    assert "/release rehearse" in gap["prompt"]


def test_an_unkeyed_gap_falls_back_to_the_generic_nudge():
    # A verdict from an older Heart carries no `stale_details` at all: the row
    # must degrade to the old prompt, never to a guessed remedy.
    v = {"verdict": "stale", "score": 90, "ts": TS,
         "stale_reasons": ["some gap nobody has mapped yet"]}
    board = dashboard.build_board(make_snapshot(), v, now=FRESH_NOW)
    (gap,) = board.blockers

    assert gap["command"] is None
    assert gap["prompt"] == "/health re-run the stale evidence: some gap nobody has mapped yet"
    assert board.stale_plan is None


def test_the_tier_carries_one_plan_that_clears_every_gap():
    v = _stale_verdict(GAPS, ["install_unknown", "test_unknown"])
    board = dashboard.build_board(make_snapshot(), v, now=FRESH_NOW)
    plan = board.stale_plan

    assert plan["count"] == 2
    for gap in GAPS:
        assert gap in plan["prompt"]           # every gap named, in order
    assert plan["prompt"].startswith("/health clear the Heart's 2 evidence gap(s)")
    # every gap here has a command, so the whole tier is one shell chain that
    # ends by re-reading the verdict
    assert plan["command"] == (f"{dashboard.VERIFY_INSTALL_CMD} && {dashboard.TICK_CMD}"
                               " && pyauto-heart readiness")


def test_the_plan_withholds_a_command_chain_it_cannot_complete():
    # A chain that silently skips the rehearsal would read as "that cleared it".
    v = _stale_verdict(GAPS + ["no release validation for current source"],
                       ["install_unknown", "test_unknown", "validation_absent"])
    board = dashboard.build_board(make_snapshot(), v, now=FRESH_NOW)

    assert board.stale_plan["command"] is None
    assert "/release rehearse" in board.stale_plan["prompt"]


def test_no_plan_when_nothing_is_stale():
    board = dashboard.build_board(make_snapshot(), make_verdict(), now=FRESH_NOW)
    assert board.stale_plan is None


def test_surfaces_render_the_remedies_and_the_plan():
    v = _stale_verdict(GAPS, ["install_unknown", "test_unknown"])
    snap = make_snapshot()

    html = dashboard.render(snap, v, fmt="html", now=FRESH_NOW)
    assert "\U0001f4cb clear them all" in html and "command chain" in html
    assert dashboard.VERIFY_INSTALL_CMD in html

    md = dashboard.render(snap, v, fmt="md", now=FRESH_NOW)
    assert "Clear every gap in one go" in md
    assert dashboard.VERIFY_INSTALL_CMD in md

    d = json.loads(dashboard.render(snap, v, fmt="json", now=FRESH_NOW))
    assert d["stale_plan"]["count"] == 2
    assert all("command" in b for b in d["blockers"])


def test_a_worded_copy_face_is_a_chip_and_a_glyph_stays_a_square():
    """The theme sizes `button.copy` as a fixed 2.6rem SQUARE. A face carrying
    words needs the `text` modifier or the label wraps inside 42px into a
    one-word-per-line column and spills out of the box — which is what the
    plan line did until it was caught on a laptop.

    Asserted per button rather than by position: the invariant is "this face
    has words, so this button is a chip", which stays true however the board
    reorders its tiers.
    """
    v = _stale_verdict(GAPS, ["install_unknown", "test_unknown"])
    html = dashboard.render(make_snapshot(), v, fmt="html", now=FRESH_NOW)

    buttons = re.findall(r"<button class='(copy[^']*)'[^>]*>([^<]*)</button>", html)
    assert buttons, "no copy buttons on the board at all"
    for cls, face in buttons:
        is_chip = "text" in cls.split()
        assert is_chip == (len(face.split()) > 1), (cls, face)

    faces = [f for _, f in buttons]
    assert "\U0001f4cb clear them all" in faces   # a worded chip is on show
    assert "\U0001f4cb" in faces                  # and a bare glyph beside it


def test_the_plan_stays_off_a_board_showing_another_tier():
    # The board shows one tier at a time; a plan for gaps the reader cannot see
    # is noise (the json surface still carries it as data).
    v = _stale_verdict(GAPS, ["install_unknown", "test_unknown"], score=45)
    v.update(verdict="red", red_reasons=["PyAutoLens: CI failure"])
    html = dashboard.render(make_snapshot(), v, fmt="html", now=FRESH_NOW)

    assert "\U0001f4cb clear them all" not in html
    assert json.loads(dashboard.render(make_snapshot(), v, fmt="json",
                                       now=FRESH_NOW))["stale_plan"]["count"] == 2


def test_the_terminal_verdict_points_at_the_door_out_of_stale():
    v = _stale_verdict(GAPS, ["install_unknown", "test_unknown"])
    block = "\n".join(dashboard.render_readiness_block(v))
    assert "pyauto-heart fix stale" in block
    # the prompt hook's one-liner stays a one-liner
    quiet = "\n".join(dashboard.render_readiness_block(v, quiet=True))
    assert "fix stale" not in quiet
