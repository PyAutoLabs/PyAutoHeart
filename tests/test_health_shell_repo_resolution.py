"""Shell dashboards must surface ambiguous checkout placement."""

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_release_default_rejects_ambiguous_hands(tmp_path):
    (tmp_path / ".pyauto-root").touch()
    for path in (tmp_path / "PyAutoHands", tmp_path / "organs" / "PyAutoHands"):
        (path / ".git").mkdir(parents=True)
    env = os.environ.copy()
    env["PYAUTO_ROOT"] = str(tmp_path)
    env.pop("PYAUTO_STATUS_FULL_DEFAULT", None)
    result = subprocess.run(
        ["bash", "-c", 'source "$1/scripts/health_release.sh"', "test", str(ROOT)],
        env=env, text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert "ambiguous" in result.stderr


def test_sync_helper_rejects_ambiguous_hands(tmp_path):
    for path in (tmp_path / "PyAutoHands", tmp_path / "organs" / "PyAutoHands"):
        (path / ".git").mkdir(parents=True)
    env = os.environ.copy()
    env["PYAUTO_STATUS_ROOT"] = str(tmp_path)
    result = subprocess.run(
        ["bash", "-c", 'source "$1/scripts/health_sync.sh"; _health_sync_repo_path PyAutoHands', "test", str(ROOT)],
        env=env, text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert "ambiguous" in result.stderr
