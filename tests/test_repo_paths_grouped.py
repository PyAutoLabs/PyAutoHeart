"""Heart's checkout resolver works with the canonical family layout."""
from pathlib import Path
import subprocess

from heart import _workspace


def test_grouped_checkout_is_used_for_smoke_and_checks(tmp_path):
    family = tmp_path / "workspaces"
    checkout = family / "demo_workspace"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir()
    assert _workspace.repo_path(tmp_path, "demo_workspace", required=True) == checkout
    assert checkout in _workspace.iter_checkouts(tmp_path)


def test_canonical_root_follows_marker_above_grouped_organ(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".pyauto-root").touch()
    heart = root / "organs" / "PyAutoHeart"
    heart.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(heart)], check=True)
    assert _workspace._canonical_from_git(heart) == root
def test_distinct_flat_and_grouped_brain_checkouts_are_rejected(tmp_path, monkeypatch):
    import pytest

    for path in (tmp_path / "PyAutoBrain", tmp_path / "organs" / "PyAutoBrain"):
        (path / ".git").mkdir(parents=True)
    monkeypatch.setattr(_workspace, "HEART_HOME", tmp_path / "organs" / "PyAutoHeart")
    with pytest.raises(ValueError, match="ambiguous checkouts"):
        _workspace._shared()
    with pytest.raises(ValueError, match="ambiguous checkouts"):
        _workspace._repo_paths()
