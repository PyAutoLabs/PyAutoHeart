"""tests/test_token_push.py — the release push-credential probe and sidecar."""

from __future__ import annotations

import datetime
import json
import subprocess

from heart.checks import token_push as tp


def _fake_runner(refuse=(), unreachable=()):
    calls = []

    def run(cmd, cwd):
        calls.append(list(cmd))
        url = next((c for c in cmd if c.startswith("https://")), "")
        repo = url.rsplit("/", 1)[-1].removesuffix(".git")
        rc = 0
        if "ls-remote" in cmd and repo in unreachable:
            rc = 128
        if "push" in cmd and (repo in refuse or repo in unreachable):
            rc = 128
        return subprocess.CompletedProcess(cmd, rc, "", "remote: Permission denied (403)\n" if rc else "")

    return run, calls


def test_probe_reports_push_per_repo_and_only_dry_runs():
    run, calls = _fake_runner(refuse={"PyAutoGalaxy"})
    out = tp.probe(run=run)
    assert set(out) == set(tp.REPOS)
    assert out["PyAutoFit"] == {"ls_remote": True, "push": True, "error": None}
    assert out["PyAutoGalaxy"]["push"] is False
    assert out["PyAutoGalaxy"]["ls_remote"] is True
    assert "403" in out["PyAutoGalaxy"]["error"]
    pushes = [c for c in calls if "push" in c]
    assert len(pushes) == len(tp.REPOS)
    assert all("--dry-run" in c and f"HEAD:{tp.PROBE_REF}" in c for c in pushes)
    # no URL ever carries userinfo — auth is the credential helper's job
    assert all("@" not in c for cmd in calls for c in cmd if c.startswith("https://"))


def test_probe_records_unreachable_repo_without_raising():
    run, _ = _fake_runner(unreachable={"PyAutoNerves"})
    out = tp.probe(run=run)
    assert out["PyAutoNerves"]["ls_remote"] is False
    assert out["PyAutoNerves"]["push"] is False


def test_build_and_load_round_trip(tmp_path):
    now = datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.timezone.utc)
    payload = tp.build({"PyAutoFit": {"ls_remote": True, "push": True, "error": None}},
                       "pyautolabs-bot", "https://example.test/runs/1", now)
    path = tmp_path / "token_push.json"
    path.write_text(json.dumps(payload))
    got = tp.load(path)
    assert got["schema"] == tp.SCHEMA
    assert got["identity"] == "pyautolabs-bot"
    assert got["checked_at"].startswith("2026-10-23T12:00:00")
    assert got["repos"]["PyAutoFit"]["push"] is True


def test_load_missing_or_malformed_is_empty(tmp_path):
    assert tp.load(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert tp.load(bad) == {}
