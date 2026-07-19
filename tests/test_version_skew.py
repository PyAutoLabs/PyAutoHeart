"""tests/test_version_skew.py — workspace compatibility floor vs newest release."""

from __future__ import annotations

import pytest

from heart.checks import version_skew as vs


@pytest.mark.parametrize("floor,newest,expected", [
    ("2026.7.9.1", "2026.7.9.1", "OK"),         # floor == newest release
    ("2026.5.1.1", "2026.7.9.1", "OK"),         # floor older than newest
    ("2026.7.15.1", "2026.7.9.1", "UNSATISFIABLE"),  # floor ahead of newest release
    ("2026.7.9", "2026.7.9.1", "OK"),           # shorter tuple compares as less
    ("2026.7.9.2", "2026.7.9.1", "UNSATISFIABLE"),
    ("not.a.version", "2026.7.9.1", "BAD"),
    ("2026.7.9.1", None, "BAD"),
    (None, "2026.7.9.1", "BAD"),
])
def test_compare_floor(floor, newest, expected):
    assert vs.compare_floor(floor, newest) == expected


@pytest.mark.parametrize("tags,expected", [
    (["2026.5.29.4", "2026.7.9.1", "2026.7.15.1"], "2026.7.15.1"),
    (["2026.7.9.1", "v-not-a-version", "latest"], "2026.7.9.1"),
    (["2026.10.1.1", "2026.7.15.1"], "2026.10.1.1"),   # numeric, not lexical
    ([], None),
    (["nightly", "dev"], None),
])
def test_newest_version(tags, expected):
    assert vs._newest_version(tags) == expected


def test_read_workspace_floor(tmp_path):
    ws = tmp_path / "autolens_workspace" / "config"
    ws.mkdir(parents=True)
    (ws / "general.yaml").write_text(
        "version:\n"
        "  minimum_library_version: 2026.7.9.1\n"
        "  workspace_version: 2026.7.9.1\n"
    )
    assert vs.read_workspace_floor("autolens_workspace", root=tmp_path) == "2026.7.9.1"


def test_read_workspace_floor_absent(tmp_path):
    # A general.yaml with no floor key → None (not a candidate).
    ws = tmp_path / "autolens_workspace" / "config"
    ws.mkdir(parents=True)
    (ws / "general.yaml").write_text("version:\n  python_version_check: true\n")
    assert vs.read_workspace_floor("autolens_workspace", root=tmp_path) is None
    # No config dir at all → None.
    assert vs.read_workspace_floor("autofit_workspace", root=tmp_path) is None


def test_newest_release_tag_reads_git_tags(tmp_path):
    import subprocess

    repo = tmp_path / "PyAutoLens"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True, env={**env})
    for t in ("2026.5.29.4", "2026.7.9.1", "2026.7.15.1"):
        subprocess.run(["git", "-C", str(repo), "tag", t], check=True)
    assert vs.newest_release_tag("PyAutoLens", root=tmp_path) == "2026.7.15.1"


def test_newest_release_tag_none_when_not_a_checkout(tmp_path):
    (tmp_path / "PyAutoLens").mkdir()  # no .git
    assert vs.newest_release_tag("PyAutoLens", root=tmp_path) is None


def test_run_skips_workspaces_without_a_floor(tmp_path, monkeypatch):
    # Only autolens_workspace has a floor; others are skipped silently.
    ws = tmp_path / "autolens_workspace" / "config"
    ws.mkdir(parents=True)
    (ws / "general.yaml").write_text("version:\n  minimum_library_version: 2026.7.9.1\n")
    monkeypatch.setattr(vs, "newest_release_tag", lambda repo, root=tmp_path: "2026.7.15.1")
    result = vs.run(root=tmp_path)
    by_ws = {w["workspace"]: w for w in result["workspaces"]}
    assert by_ws["autolens_workspace"]["status"] == "OK"
    assert "autofit_workspace" not in by_ws  # no floor → skipped


def test_run_flags_unsatisfiable_floor(tmp_path, monkeypatch):
    # Floor ahead of the newest released version → UNSATISFIABLE (release-blocking).
    ws = tmp_path / "autolens_workspace" / "config"
    ws.mkdir(parents=True)
    (ws / "general.yaml").write_text("version:\n  minimum_library_version: 2026.8.1.1\n")
    monkeypatch.setattr(vs, "newest_release_tag", lambda repo, root=tmp_path: "2026.7.15.1")
    result = vs.run(root=tmp_path)
    w = {x["workspace"]: x for x in result["workspaces"]}["autolens_workspace"]
    assert w["status"] == "UNSATISFIABLE"
    assert w["floor"] == "2026.8.1.1" and w["newest_release"] == "2026.7.15.1"


def test_run_unknown_when_newest_release_unresolvable(tmp_path, monkeypatch):
    # Floored workspace but the library has no resolvable release tag → UNKNOWN
    # (caution), never a hard block.
    ws = tmp_path / "autolens_workspace" / "config"
    ws.mkdir(parents=True)
    (ws / "general.yaml").write_text("version:\n  minimum_library_version: 2026.7.9.1\n")
    monkeypatch.setattr(vs, "newest_release_tag", lambda repo, root=tmp_path: None)
    result = vs.run(root=tmp_path)
    w = {x["workspace"]: x for x in result["workspaces"]}["autolens_workspace"]
    assert w["status"] == "UNKNOWN"
    assert w["newest_release"] is None


def test_autolens_assistant_is_a_polled_workspace():
    # Gap closed vs verify_workspace_versions.sh, which covers 8 workspaces.
    # The map lives in config/repos.yaml `version_skew` (the policy file).
    mapping = vs.workspace_library()
    assert "autolens_assistant" in mapping
    assert mapping["autolens_assistant"] == ("PyAutoLens", "autolens")


# --- state-dir isolation (the 2026-07-15 clobber incident's sibling) -----------

def test_run_writes_nothing_to_state_dir(tmp_path):
    """run() must be side-effect-free: the write lives in main() only, so tests
    (and any library caller) can never clobber live Heart state."""
    import os
    from pathlib import Path
    state_dir = Path(os.environ["HEART_STATE_DIR"])
    before = set(state_dir.glob("**/*")) if state_dir.exists() else set()
    vs.run(root=tmp_path)
    after = set(state_dir.glob("**/*")) if state_dir.exists() else set()
    assert after == before


def test_main_persists_result_to_state_dir(monkeypatch):
    """The tick path (python -m heart.checks.version_skew) must still persist."""
    import json
    import os
    from pathlib import Path
    monkeypatch.setattr(vs, "run", lambda root=vs.PYAUTO_ROOT: {"workspaces": []})
    assert vs.main(["version_skew"]) == 0
    written = json.loads((Path(os.environ["HEART_STATE_DIR"]) / "version_skew.json").read_text())
    assert written == {"workspaces": []}
