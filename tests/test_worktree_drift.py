"""tests/test_worktree_drift.py — categorisation over synthetic worktree trees.

Covers the three noise mechanisms fixed in PyAutoHeart#123: claims outside the
wt root read as missing forever (path-test instead), symlinked canonical
checkouts counted dirty once per linking worktree (dedupe into their own
category), and parked.md worktrees read as orphans.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from heart.checks import worktree_drift as wd


def _git_repo(path: Path, dirty: bool = False) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if dirty:
        (path / "uncommitted.txt").write_text("x")
    return path


def _registry(path: Path, entries: dict[str, str]) -> Path:
    lines = ["# Registry", ""]
    for task, wt in entries.items():
        lines += [f"## {task}", f"- worktree: {wt}", ""]
    path.write_text("\n".join(lines))
    return path


def test_claim_outside_wt_root_is_not_missing_and_not_orphan(tmp_path):
    outside = tmp_path / "codex-worktrees" / "some-task"
    _git_repo(outside / "RepoA")
    active = _registry(tmp_path / "active.md", {"some-task": str(outside)})
    out = wd.scan(tmp_path / "wt-root", active, tmp_path / "parked.md")
    assert out["missing"] == []
    assert out["orphans"] == []
    assert any(d["path"] == str(outside) for d in out["parked"]) is False


def test_nonexistent_claim_is_missing(tmp_path):
    active = _registry(tmp_path / "active.md", {"gone": str(tmp_path / "nope")})
    out = wd.scan(tmp_path / "wt-root", active, tmp_path / "parked.md")
    assert [m["task"] for m in out["missing"]] == ["gone"]


def test_symlinked_canonical_dirt_counted_once_and_not_task_dirt(tmp_path):
    canonical = _git_repo(tmp_path / "canonical" / "PyAutoMind", dirty=True)
    wt_root = tmp_path / "wt-root"
    for task in ("task-a", "task-b"):
        d = wt_root / task
        d.mkdir(parents=True)
        (d / "PyAutoMind").symlink_to(canonical)
    active = _registry(tmp_path / "active.md", {
        "task-a": str(wt_root / "task-a"), "task-b": str(wt_root / "task-b")})
    out = wd.scan(wt_root, active, tmp_path / "parked.md")
    assert out["dirty"] == []                       # never task drift
    assert len(out["canonical_dirty"]) == 1        # deduped across worktrees
    assert out["canonical_dirty"][0]["repo"] == "PyAutoMind"
    assert out["canonical_dirty"][0]["dirty_files"] == 1


def test_real_worktree_dirt_still_attributed(tmp_path):
    wt_root = tmp_path / "wt-root"
    _git_repo(wt_root / "task-a" / "RepoA", dirty=True)
    active = _registry(tmp_path / "active.md", {"task-a": str(wt_root / "task-a")})
    out = wd.scan(wt_root, active, tmp_path / "parked.md")
    assert out["dirty"] == [
        {"worktree": "task-a", "repo": "RepoA", "dirty_files": 1}]
    assert out["canonical_dirty"] == []


def test_parked_worktree_is_not_an_orphan(tmp_path):
    wt_root = tmp_path / "wt-root"
    _git_repo(wt_root / "parked-task" / "RepoA")
    parked = _registry(tmp_path / "parked.md", {"parked-task": str(wt_root / "parked-task")})
    out = wd.scan(wt_root, tmp_path / "active.md", parked)
    assert out["orphans"] == []
    assert [p["name"] for p in out["parked"]] == ["parked-task"]


def test_unclaimed_dir_is_an_orphan(tmp_path):
    wt_root = tmp_path / "wt-root"
    _git_repo(wt_root / "mystery" / "RepoA")
    out = wd.scan(wt_root, tmp_path / "active.md", tmp_path / "parked.md")
    assert [o["name"] for o in out["orphans"]] == ["mystery"]


def test_prose_worktree_values_are_ignored(tmp_path):
    parked = tmp_path / "parked.md"
    parked.write_text("## shipped-task\n- worktree: none (pushed; local removed)\n")
    out = wd.scan(tmp_path / "wt-root", tmp_path / "active.md", parked)
    assert out["missing"] == []
    assert out["claimed_count"] == 0


def test_tilde_claims_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    wt = tmp_path / "Code" / "wt-root" / "task-a"
    _git_repo(wt / "RepoA")
    active = _registry(tmp_path / "active.md", {"task-a": "~/Code/wt-root/task-a"})
    out = wd.scan(tmp_path / "Code" / "wt-root", active, tmp_path / "parked.md")
    assert out["missing"] == []
    assert out["orphans"] == []
