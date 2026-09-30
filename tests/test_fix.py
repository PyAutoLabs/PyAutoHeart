"""The systematic plan is observer-only, complete, and shared across surfaces."""

import json
import subprocess
from pathlib import Path

from heart import dashboard, fix


def inputs():
    repos = {f"Repo{i}": {"ci_status": {"conclusion": "failure", "group": "libraries",
                                       "url": f"https://example.org/run/{i}"}}
             for i in range(12)}
    snapshot = {
        "ts": "2026-09-30T12:00:00+00:00", "repos": repos,
        "worktree_drift": {
            "dirty": [{"worktree": f"task{i}", "repo": "Repo0", "dirty_files": 1}
                      for i in range(25)],
            "missing": [{"task": "absent-task", "path": "/absent"}],
            "orphans": [{"path": "/orphan", "has_real_worktrees": True}],
        },
        "unit_timings": {"imports": [{"package": f"pkg{i}", "seconds": i + 1}
                                     for i in range(10)]},
    }
    verdict = {"verdict": "red", "score": 25,
               "red_reasons": [f"Repo{i}: CI failure" for i in range(12)],
               "yellow_reasons": ["warning <script>unsafe</script>"],
               "stale_reasons": ["install verification not run"],
               "stale_details": [{"key": "install_unknown"}]}
    return snapshot, verdict


def test_complete_plan_ignores_display_caps_and_keeps_authority_boundaries():
    snapshot, verdict = inputs()
    board = dashboard.build_board(snapshot, verdict)
    plan = board.fix_plan["prompt"]
    assert "Repo11: CI failure" in plan
    assert "https://example.org/run/11" in plan
    assert "task24" in plan and "absent-task" in plan and "/orphan" in plan
    assert "pkg9" in plan
    assert "not approval to merge, delete work or release" in plan
    assert "Dirty worktrees are not disposable" in plan
    assert "never schedule background follow-up" in plan
    assert "diagnose unsupported findings" in plan
    assert board.verdict == "red" and board.score == 25
    assert {b["severity"] for b in board.blockers} == {"red", "yellow", "stale"}


def test_fix_all_matches_dashboard_and_does_not_dispatch(monkeypatch, capsys):
    from heart import readiness

    snapshot, verdict = inputs()
    monkeypatch.setattr(fix.state, "load", lambda: snapshot)
    monkeypatch.setattr(readiness, "load_verdict", lambda: verdict)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("fix all must never execute a command")))
    assert fix.main(["fix", "all"]) == 0
    expected = json.loads(dashboard.render(snapshot, verdict, fmt="json"))["fix_plan"]
    assert capsys.readouterr().out == expected["prompt"] + "\n"


def test_devbox_and_test_failures_survive_display_caps():
    snapshot, verdict = inputs()
    snapshot["test_run"] = {"ready": False, "failed_scripts": [f"script{i}" for i in range(20)]}
    devbox = {"ts": snapshot["ts"], "sections": {"worktree_drift": {
        "state": "fail", "summary": "12 dirty",
        "details": [f"devbox-task{i}" for i in range(12)]}}}
    board = dashboard.build_board(snapshot, verdict,
                                  unobserved=("worktree_drift",), devbox=devbox)
    prompt = board.fix_plan["prompt"]
    assert "script19" in prompt and "devbox-task11" in prompt
    assert "check timestamp before acting" in prompt


def test_no_cache_never_claims_green(monkeypatch, capsys):
    monkeypatch.setattr(fix.state, "load", lambda: None)
    assert fix.main(["fix", "all"]) == 2
    assert "no cache" in capsys.readouterr().err


def test_html_all_tiers_complete_disclosure_and_safe_fallback():
    snapshot, verdict = inputs()
    html = dashboard.render(snapshot, verdict, fmt="html")
    assert "Release blockers (12)" in html
    assert "Warnings (1)" in html and "Evidence gaps (1)" in html
    assert "Show 4 more" in html and "Repo11" in html
    assert "warning &lt;script&gt;unsafe&lt;/script&gt;" in html
    assert '<script>unsafe</script>' not in html
    assert 'role="status" aria-live="polite"' in html
    assert 'class="prompt-fallback"' in html
    assert "[these guys are giving me life]" in html
    assert html.index("Fix Heart systematically") < html.index("Release blockers (12)")


def test_numeric_emphasis_is_built_from_fields_and_escapes_subjects():
    row = {"imports": [{"package": "<pkg>", "python": "3.12", "seconds": 3.5}]}
    html = dashboard._unit_import_details(row, html=True)[0]
    assert '&lt;pkg&gt;' in html and '<strong class="duration">3.50s</strong>' in html
    assert dashboard._unit_import_details(row)[0] == "<pkg> py3.12: 3.50s"


def test_bash_dispatch_exposes_all_topic(tmp_path):
    # An empty cache exercises the real shell case list without any health tick.
    import os

    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "HEART_STATE_DIR": str(tmp_path), "NO_COLOR": "1"}
    result = subprocess.run(["bash", str(root / "bin/pyauto-heart"), "fix", "all"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "no cache" in result.stderr
    assert "unknown topic" not in result.stderr
