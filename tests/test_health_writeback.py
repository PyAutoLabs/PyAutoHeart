"""Run the workflow's publication step against a bare remote and two clones."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from heart.timings import append_gates

ROOT = Path(__file__).resolve().parents[1]
README = "# Heart\n\n<!-- heart:begin -->\nold\n<!-- heart:end -->\n"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def clones(tmp_path, monkeypatch):
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    collector = tmp_path / "collector"
    git(tmp_path, "clone", str(remote), str(collector))
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "test writer")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "test@example.org")
    (collector / "README.md").write_text(README)
    (collector / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/prepare_health_writeback.py", collector / "scripts")
    git(collector, "add", ".")
    git(collector, "commit", "-m", "baseline")
    git(collector, "push", "origin", "main")
    writer = tmp_path / "writer"
    git(tmp_path, "clone", str(remote), str(writer))
    (collector / "readme_strip.md").write_text("new")
    # The production step installs Git credentials. A local remote needs none.
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\ntest "$*" = "auth setup-git"\n')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return remote, collector, writer


def publish(collector):
    workflow = yaml.safe_load((ROOT / ".github/workflows/heart-health.yml").read_text())
    steps = workflow["jobs"]["heart-health"]["steps"]
    assert steps[0]["with"]["ref"] == "${{ github.ref == 'refs/heads/main' && 'refs/heads/main' || github.sha }}"
    step = next(s for s in steps if s["name"].startswith("Commit the README block"))
    return subprocess.run(["bash", "-eu", "-c", step["run"]], cwd=collector,
                          capture_output=True, text=True)


def advance(writer, name, content):
    path = writer / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)
    git(writer, "add", name)
    git(writer, "commit", "-m", "independent advance")
    git(writer, "push")


def test_publish_over_prose_and_repeat_without_duplicate(clones):
    remote, collector, writer = clones
    advance(writer, "README.md", README + "New prose.\n")
    record = collector / "timings/gates.jsonl"
    observation = {"date": "2026-10-25", "gates": {}}
    assert append_gates(record, observation)
    result = publish(collector)
    assert result.returncode == 0, result.stderr
    published = git(remote, "rev-parse", "main")
    assert git(remote, "rev-parse", "main^") == git(writer, "rev-parse", "HEAD")
    assert git(remote, "show", "main:README.md") == README.replace("old", "new").strip() + "\nNew prose."
    assert not append_gates(record, observation)
    result = publish(collector)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "main") == published
    assert len(git(remote, "show", "main:timings/gates.jsonl").splitlines()) == 1


@pytest.mark.parametrize(("name", "content"), [
    ("README.md", README.replace("old", "concurrent board")),
    ("timings/gates.jsonl", '{"date":"2026-10-25"}\n'),
    ("collector.py", "changed input"),
])
def test_conflict_stops_publication_even_with_unchanged_output(clones, name, content):
    remote, collector, writer = clones
    (collector / "readme_strip.md").write_text("old")
    advance(writer, name, content)
    result = publish(collector)
    assert result.returncode != 0
    assert "Dispatch Heart Health on current main" in result.stderr
    assert git(remote, "rev-parse", "main") == git(writer, "rev-parse", "HEAD")
