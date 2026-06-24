"""tests/test_version_skew.py — workspace pin vs installed library compare."""

from __future__ import annotations

import pytest

from heart.checks import version_skew as vs


@pytest.mark.parametrize("pinned,installed,expected", [
    ("2026.5.29.4", "2026.5.29.4", "MATCH"),
    ("2026.6.1.1", "2026.5.29.4", "AHEAD"),
    ("2026.5.1.1", "2026.5.29.4", "BEHIND"),
    ("2026.5.29.4", None, "BAD"),
    (None, "2026.5.29.4", "BAD"),
    ("not.a.version", "2026.5.29.4", "BAD"),
    ("2026.5.29", "2026.5.29.4", "BEHIND"),   # shorter tuple compares as less
])
def test_compare(pinned, installed, expected):
    assert vs.compare(pinned, installed) == expected


def test_read_library_version_regex(tmp_path):
    repo = tmp_path / "PyAutoFit" / "autofit"
    repo.mkdir(parents=True)
    (repo / "__init__.py").write_text(
        'from x import y\n__version__ = "2026.5.29.4"\nfoo = 1\n'
    )
    assert vs.read_library_version("PyAutoFit", "autofit", root=tmp_path) == "2026.5.29.4"


def test_read_library_version_missing(tmp_path):
    assert vs.read_library_version("Nope", "nope", root=tmp_path) is None


def test_read_workspace_pin_general_yaml_precedence(tmp_path):
    ws = tmp_path / "autolens_workspace" / "config"
    ws.mkdir(parents=True)
    (ws / "general.yaml").write_text("version:\n  workspace_version: 2026.6.1.2\n")
    # version.txt disagrees; general.yaml must win.
    (tmp_path / "autolens_workspace" / "version.txt").write_text("2026.1.1.1\n")
    assert vs.read_workspace_pin("autolens_workspace", root=tmp_path) == "2026.6.1.2"


def test_read_workspace_pin_version_txt_fallback(tmp_path):
    ws = tmp_path / "autolens_workspace"
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "general.yaml").write_text("version:\n  python_version_check: true\n")
    (ws / "version.txt").write_text("2026.5.29.4\n")
    assert vs.read_workspace_pin("autolens_workspace", root=tmp_path) == "2026.5.29.4"


def test_read_workspace_pin_none_when_unpinned(tmp_path):
    (tmp_path / "autolens_workspace_test").mkdir(parents=True)
    assert vs.read_workspace_pin("autolens_workspace_test", root=tmp_path) is None


def test_run_skips_unpinned_and_classifies(tmp_path):
    # autolens_workspace pinned ahead of installed; HowToFit in sync.
    al = tmp_path / "autolens_workspace" / "config"
    al.mkdir(parents=True)
    (al / "general.yaml").write_text("version:\n  workspace_version: 2026.6.1.1\n")
    lens = tmp_path / "PyAutoLens" / "autolens"
    lens.mkdir(parents=True)
    (lens / "__init__.py").write_text('__version__ = "2026.5.29.4"\n')
    # no autofit_workspace dir at all → skipped silently
    result = vs.run(root=tmp_path)
    by_ws = {w["workspace"]: w for w in result["workspaces"]}
    assert by_ws["autolens_workspace"]["status"] == "AHEAD"
    assert "autofit_workspace" not in by_ws  # unpinned/missing → skipped


def test_read_workspace_pin_sources_returns_both(tmp_path):
    ws = tmp_path / "autolens_workspace"
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "general.yaml").write_text(
        "version:\n  workspace_version: 2026.6.1.2\n"
    )
    (ws / "version.txt").write_text("2026.1.1.1\n")
    assert vs.read_workspace_pin_sources("autolens_workspace", root=tmp_path) == (
        "2026.6.1.2",
        "2026.1.1.1",
    )


def test_run_flags_general_yaml_version_txt_mismatch(tmp_path):
    # general.yaml and version.txt both present and disagree → MISMATCH,
    # the same release-blocking condition verify_workspace_versions.sh fails on.
    ws = tmp_path / "autolens_workspace"
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "general.yaml").write_text(
        "version:\n  workspace_version: 2026.6.1.2\n"
    )
    (ws / "version.txt").write_text("2026.1.1.1\n")
    lens = tmp_path / "PyAutoLens" / "autolens"
    lens.mkdir(parents=True)
    (lens / "__init__.py").write_text('__version__ = "2026.6.1.2"\n')
    result = vs.run(root=tmp_path)
    w = {x["workspace"]: x for x in result["workspaces"]}["autolens_workspace"]
    assert w["status"] == "MISMATCH"
    assert w["pinned"] == "2026.6.1.2" and w["version_txt"] == "2026.1.1.1"


def test_run_unknown_when_library_not_checked_out(tmp_path):
    # Pinned workspace but no library __init__.py to read → UNKNOWN (caution),
    # never a hard block — mirrors the script's "SKIP (cannot import <pkg>)".
    ws = tmp_path / "autolens_workspace" / "config"
    ws.mkdir(parents=True)
    (ws / "general.yaml").write_text("version:\n  workspace_version: 2026.6.1.1\n")
    result = vs.run(root=tmp_path)
    w = {x["workspace"]: x for x in result["workspaces"]}["autolens_workspace"]
    assert w["status"] == "UNKNOWN"
    assert w["installed"] is None


def test_autolens_assistant_is_a_pinned_workspace():
    # Gap closed vs verify_workspace_versions.sh, which covers 8 workspaces.
    assert "autolens_assistant" in vs.WORKSPACE_LIBRARY
    assert vs.WORKSPACE_LIBRARY["autolens_assistant"] == ("PyAutoLens", "autolens")
