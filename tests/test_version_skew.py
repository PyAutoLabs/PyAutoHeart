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


# --- deep PyPI yank leg (--pypi) ----------------------------------------------

def _files(*yanked):
    return [{"yanked": y} for y in yanked]


# 2026.7.6.649 fully yanked (the real 2026-07 incident); two installable newer.
RELEASES = {
    "2026.7.6.649": _files(True, True),
    "2026.7.9.1": _files(False, False),
    "2026.7.15.1": _files(False),
}


@pytest.mark.parametrize("floor,releases,expected", [
    ("2026.7.9.1", RELEASES, "OK"),                    # floor installable
    ("2026.7.6.649", RELEASES, "FLOOR_YANKED"),        # floor yanked, newer installable
    ("2026.7.1.1", RELEASES, "FLOOR_YANKED"),          # floor absent from PyPI, newer installable
    ("2026.8.1.1", RELEASES, "UNSATISFIABLE"),         # nothing >= floor exists
    ("2026.7.9.1", {"2026.7.9.1": _files(True)}, "UNSATISFIABLE"),  # everything >= floor yanked
    ("2026.7.9.1", {"2026.7.9.1": []}, "UNSATISFIABLE"),            # fileless release installs nothing
    ("not.a.version", RELEASES, "BAD"),
    ("2026.7.9.1", None, "UNKNOWN"),                   # PyPI unreachable → never a false block
])
def test_pypi_floor_status(floor, releases, expected):
    assert vs.pypi_floor_status(floor, releases) == expected


def test_run_pypi_one_fetch_per_package(tmp_path, monkeypatch):
    # autolens_workspace and HowToLens both map to package `autolens` → the
    # probe must fetch each distinct package once, not once per workspace.
    for ws in ("autolens_workspace", "HowToLens"):
        cfg = tmp_path / ws / "config"
        cfg.mkdir(parents=True)
        (cfg / "general.yaml").write_text("version:\n  minimum_library_version: 2026.7.9.1\n")
    calls = []
    monkeypatch.setattr(vs, "fetch_pypi_releases", lambda pkg: calls.append(pkg) or RELEASES)
    result = vs.run_pypi(root=tmp_path)
    assert calls == ["autolens"]
    by_ws = {w["workspace"]: w for w in result["workspaces"]}
    assert by_ws["autolens_workspace"]["status"] == "OK"
    assert by_ws["HowToLens"]["package"] == "autolens"


def test_run_pypi_offline_is_unknown(tmp_path, monkeypatch):
    ws = tmp_path / "autolens_workspace" / "config"
    ws.mkdir(parents=True)
    (ws / "general.yaml").write_text("version:\n  minimum_library_version: 2026.7.9.1\n")
    monkeypatch.setattr(vs, "fetch_pypi_releases", lambda pkg: None)
    result = vs.run_pypi(root=tmp_path)
    w = {x["workspace"]: x for x in result["workspaces"]}["autolens_workspace"]
    assert w["status"] == "UNKNOWN"


def test_run_pypi_flags_yanked_floor(tmp_path, monkeypatch):
    # The 2026-07 incident shape: floor names the yanked release while newer
    # installable releases exist → FLOOR_YANKED (warn), not a hard block.
    ws = tmp_path / "autolens_workspace" / "config"
    ws.mkdir(parents=True)
    (ws / "general.yaml").write_text("version:\n  minimum_library_version: 2026.7.6.649\n")
    monkeypatch.setattr(vs, "fetch_pypi_releases", lambda pkg: RELEASES)
    result = vs.run_pypi(root=tmp_path)
    w = {x["workspace"]: x for x in result["workspaces"]}["autolens_workspace"]
    assert w["status"] == "FLOOR_YANKED"


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


def test_main_pypi_persists_to_sibling_file(monkeypatch):
    """--pypi writes version_skew_pypi.json and never touches the tick's
    version_skew.json — the tick must not clobber on-demand PyPI evidence and
    vice versa."""
    import json
    import os
    from pathlib import Path
    state_dir = Path(os.environ["HEART_STATE_DIR"])
    tick_file = state_dir / "version_skew.json"
    tick_before = tick_file.read_text() if tick_file.is_file() else None
    payload = {"workspaces": [{"workspace": "autolens_workspace", "status": "FLOOR_YANKED"}]}
    monkeypatch.setattr(vs, "run_pypi", lambda root=vs.PYAUTO_ROOT: payload)
    assert vs.main(["version_skew", "--pypi"]) == 0
    written = json.loads((state_dir / "version_skew_pypi.json").read_text())
    assert written == payload
    tick_after = tick_file.read_text() if tick_file.is_file() else None
    assert tick_after == tick_before
