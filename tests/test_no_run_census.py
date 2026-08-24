"""tests/test_no_run_census.py — the NO_RUN marker census.

Fake repo names throughout (the tenant firewall). The parser is line-based on
purpose; the two traps it exists to survive — markers living in `#` comments,
and a bare `- off` entry parsing as a YAML boolean — are asserted directly.
"""

from __future__ import annotations

import json

from heart.checks import no_run_census as nrc

SAMPLE = """\
# Scripts the release run does not execute.
scripts:
  - imaging/slow_measured.py   # SLOW 2026-07-14 - takes 233.7s, over the 300s cap
  - imaging/slow_capped.py     # SLOW 2026-07-14 - flakes at the 1800s cap
  - imaging/slow_undated.py    # SLOW - never timed
  - imaging/broken.py          # NEEDS_FIX 2026-07-14 - raises on the new API
  - imaging/broken_undated.py  # NEEDS_FIX - fails on import
  - off
  - generated/plot_maker.py
  - "quoted/entry.py"
"""


def _rows(text=SAMPLE, repo="RepoA"):
    return {r["entry"]: r for r in nrc.parse_no_run(text, repo)}


# --- tiers -------------------------------------------------------------------

def test_parses_all_three_tiers():
    rows = _rows()
    assert rows["imaging/slow_measured.py"]["marker"] == "SLOW"
    assert rows["imaging/slow_measured.py"]["date"] == "2026-07-14"
    assert rows["imaging/broken.py"]["marker"] == "NEEDS_FIX"
    assert rows["generated/plot_maker.py"]["marker"] == "permanent"
    assert rows["generated/plot_maker.py"]["prompt"] is None


def test_undated_markers_keep_their_tier():
    rows = _rows()
    assert rows["imaging/slow_undated.py"]["marker"] == "SLOW"
    assert rows["imaging/slow_undated.py"]["date"] == ""
    assert rows["imaging/broken_undated.py"]["marker"] == "NEEDS_FIX"
    assert rows["imaging/broken_undated.py"]["reason"] == "fails on import"


def test_bare_off_entry_is_a_permanent_path_not_a_boolean():
    """The recorded crash: `- off` is a YAML boolean, and `should_skip` died on
    it. Line-based parsing sees the five-letter path it actually is."""
    rows = _rows()
    assert "off" in rows
    assert rows["off"]["marker"] == "permanent"
    assert rows["off"]["entry"] == "off"


def test_quoted_entries_are_unquoted():
    assert "quoted/entry.py" in _rows()


def test_section_keys_and_comment_lines_are_not_entries():
    rows = _rows()
    assert "scripts:" not in rows
    assert not any(e.startswith("#") for e in rows)


# --- the measured heuristic ---------------------------------------------------

def test_a_real_seconds_figure_is_a_measurement():
    assert nrc.is_measured("takes 233.7s, over the 300s cap") is True
    assert nrc.is_measured("ran in 45 s locally") is True


def test_a_bare_cap_mention_is_not_a_measurement():
    """"flakes at the 1800s cap" names the LIMIT the script hit, not its
    runtime — exactly the unmeasured claim this census exists to surface."""
    assert nrc.is_measured("flakes at the 1800s cap") is False
    assert nrc.is_measured("over the 300 s cap") is False
    assert nrc.is_measured("too slow") is False


def test_measured_flag_lands_on_the_row():
    rows = _rows()
    assert rows["imaging/slow_measured.py"]["measured"] is True
    assert rows["imaging/slow_capped.py"]["measured"] is False
    assert rows["imaging/slow_undated.py"]["measured"] is False


# --- prompts (verbatim; the producer writes them, no renderer re-derives) -----

def test_unmeasured_slow_prompt_is_exact():
    assert _rows()["imaging/slow_capped.py"]["prompt"] == (
        "/bug no_run: RepoA imaging/slow_capped.py SLOW since 2026-07-14 with no "
        "measurement — retime against the real cap, then fix it or delete the marker"
    )


def test_undated_unmeasured_slow_says_unknown_date():
    assert _rows()["imaging/slow_undated.py"]["prompt"] == (
        "/bug no_run: RepoA imaging/slow_undated.py SLOW since unknown date with no "
        "measurement — retime against the real cap, then fix it or delete the marker"
    )


def test_measured_slow_prompt_is_exact():
    assert _rows()["imaging/slow_measured.py"]["prompt"] == (
        "/bug no_run: RepoA imaging/slow_measured.py SLOW since 2026-07-14 — "
        "takes 233.7s, over the 300s cap"
    )


def test_needs_fix_prompt_is_exact():
    assert _rows()["imaging/broken.py"]["prompt"] == (
        "/bug no_run: RepoA imaging/broken.py NEEDS_FIX since 2026-07-14 — "
        "raises on the new API. Reproduce before fixing: stale markers have "
        "evaporated before"
    )


def test_needs_fix_undated_prompt_says_unknown_date():
    assert "NEEDS_FIX since unknown date" in _rows()["imaging/broken_undated.py"]["prompt"]


def test_long_reasons_are_truncated():
    long_reason = "x" * 300
    row = nrc.parse_no_run(f"  - a.py  # NEEDS_FIX 2026-07-14 - {long_reason}", "RepoA")[0]
    assert row["reason"] == long_reason           # the row keeps the whole thing
    assert ("x" * 121) not in row["prompt"]       # the prompt is bounded


# --- sidecars -----------------------------------------------------------------

def test_sidecar_counts_every_tier():
    side = nrc.build_sidecar("RepoA", "workspaces", SAMPLE, "T")
    assert side["present"] is True
    assert side["slow"] == 3
    assert side["unmeasured_slow"] == 2
    assert side["needs_fix"] == 2
    assert side["permanent"] == 3            # off, generated/plot_maker.py, quoted
    assert side["ts"] == "T"


def test_missing_file_is_present_false_not_an_error():
    """One workspace_test repo genuinely has no no_run.yaml. That is honest
    data; an empty census would read as "nothing skipped here"."""
    side = nrc.build_sidecar("RepoB", "workspaces_test", "", "T", present=False)
    assert side["present"] is False
    assert side["rows"] == []
    assert side["slow"] == side["needs_fix"] == side["permanent"] == 0


def test_empty_but_present_file_is_a_clean_census():
    side = nrc.build_sidecar("RepoC", "workspaces", "scripts:\n", "T")
    assert side["present"] is True and side["rows"] == []


# --- aggregate ----------------------------------------------------------------

def _agg():
    a = nrc.build_sidecar("RepoA", "workspaces", SAMPLE, "T")
    b = nrc.build_sidecar("RepoB", "workspaces_test", "", "T", present=False)
    c = nrc.build_sidecar(
        "RepoC", "howto",
        "  - slow/one.py  # SLOW 2026-08-01 - 91.2s measured\n"
        "  - fine/two.py\n", "T")
    return nrc.aggregate([a, b, c], "T")


def test_aggregate_totals():
    roll = _agg()
    assert roll["totals"] == {"slow": 4, "needs_fix": 2, "permanent": 4,
                              "unmeasured_slow": 2, "repos": 3, "repos_present": 2}


def test_aggregate_repo_summaries_include_the_absent_repo():
    roll = _agg()
    by_repo = {r["repo"]: r for r in roll["repos"]}
    assert by_repo["RepoB"]["present"] is False
    assert by_repo["RepoC"]["slow"] == 1


def test_aggregate_rows_are_worst_first_and_actionable_only():
    roll = _agg()
    markers = [(r["marker"], r["measured"]) for r in roll["rows"]]
    # unmeasured SLOW, then NEEDS_FIX, then measured SLOW; permanent never.
    assert markers == [("SLOW", False), ("SLOW", False),
                       ("NEEDS_FIX", False), ("NEEDS_FIX", False),
                       ("SLOW", True), ("SLOW", True)]
    assert all(r["prompt"] for r in roll["rows"])
    assert all(r["repo"] for r in roll["rows"])


def test_summary_lines(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    roll = _agg()
    line = nrc.summary_line(roll)
    assert "4 SLOW (2 unmeasured) / 2 NEEDS_FIX / 4 permanent" in line
    assert "across 2 workspaces" in line
    absent = nrc.repo_summary_line({"name": "RepoB", "present": False})
    assert "no no_run.yaml" in absent


# --- main wiring --------------------------------------------------------------

def test_main_writes_sidecar_then_aggregates(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    per_repo = tmp_path / "per-repo"
    per_repo.mkdir()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(SAMPLE))
    rc = nrc.main(["--name", "RepoA", "--group", "workspaces", "--ts", "T",
                   "--out", str(per_repo / "RepoA.no_run_census.json")])
    assert rc == 0
    side = json.loads((per_repo / "RepoA.no_run_census.json").read_text())
    assert side["unmeasured_slow"] == 2

    out = tmp_path / "no_run_census.json"
    rc = nrc.main(["--aggregate", "--per-repo-dir", str(per_repo), "--ts", "T",
                   "--out", str(out)])
    assert rc == 0
    roll = json.loads(out.read_text())
    assert roll["totals"]["slow"] == 3
    assert "SLOW" in capsys.readouterr().out


def test_main_missing_writes_present_false(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    out = tmp_path / "RepoB.no_run_census.json"
    rc = nrc.main(["--name", "RepoB", "--group", "workspaces_test", "--ts", "T",
                   "--missing", "--out", str(out)])
    assert rc == 0
    assert json.loads(out.read_text())["present"] is False
    assert "no no_run.yaml" in capsys.readouterr().out
