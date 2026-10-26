import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from heart import result_provenance as provenance
from heart.checks import script_timing, test_run


def shard(path):
    return {"project": "WorkspaceA", "directory": "examples", "summary": {"passed": 1},
            "results": [{"file": str(path), "status": "passed", "duration_seconds": 4}]}


@pytest.mark.parametrize("foreign", ["/home/other/scripts/test.py", "/runner-extra/test.py", "/runner/../outside/test.py", "scripts/relative.py"])
def test_foreign_paths_warn_and_invalidate_shard(foreign, capsys):
    assert not provenance.trusted_results(shard(foreign), Path("shard.json"), "/runner")
    warning = capsys.readouterr().err
    assert "shard.json" in warning and foreign in warning


def test_collectors_exclude_foreign_counts_and_timings(tmp_path, monkeypatch, capsys):
    runner = tmp_path / "runner"
    monkeypatch.setenv("PYAUTO_RESULT_ROOT", str(runner))
    (tmp_path / "good__script.json").write_text(json.dumps(shard(runner / "scripts/test.py")))
    (tmp_path / "stale__script.json").write_text(json.dumps(shard(tmp_path / "foreign/test.py")))
    assert len(script_timing.scan_latest_results(tmp_path)) == 1
    result = test_run._from_per_job(tmp_path)
    assert result["passed"] == 1
    assert "stale__script.json" in capsys.readouterr().err
    assert provenance.main([str(tmp_path), "--root", str(runner)]) == 1


def test_clean_shard_and_explicit_local_replay(tmp_path, monkeypatch):
    monkeypatch.delenv("PYAUTO_RESULT_ROOT", raising=False)
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    data = shard(tmp_path / "scripts/test.py")
    assert provenance.trusted_results(data, Path("local.json"))
    assert provenance.trusted_results(data, Path("local.json"), tmp_path)
    (tmp_path / "good.json").write_text(json.dumps(data))
    assert provenance.main([str(tmp_path), "--root", str(tmp_path)]) == 0


def test_nested_artifacts_keep_same_named_sidecars_and_validate_every_shard(tmp_path):
    runner = tmp_path / "runner"
    results = tmp_path / "all-results"
    for name in ("first", "second"):
        artifact = results / f"results-scripts-{name}"
        artifact.mkdir(parents=True)
        (artifact / "report.json").write_text(json.dumps(shard(runner / name / "test.py")))
        (artifact / "smoke_timings.json").write_text(json.dumps({"timings": [name]}))

    assert len(list(results.rglob("smoke_timings.json"))) == 2
    assert provenance.main([str(results), "--root", str(runner)]) == 0

    # Nesting must not hide a foreign shard behind a passing sibling.
    foreign = results / "results-scripts-second" / "report.json"
    foreign.write_text(json.dumps(shard(tmp_path / "outside/test.py")))
    assert provenance.main([str(results), "--root", str(runner)]) == 1


@pytest.mark.parametrize("payload", ['{"results":', '{"results": {}}'])
def test_nested_malformed_result_shard_still_fails(tmp_path, payload, capsys):
    artifact = tmp_path / "all-results" / "results-scripts-example"
    artifact.mkdir(parents=True)
    report = artifact / "report.json"
    report.write_text(payload)
    assert provenance.main([str(artifact.parent), "--root", str(tmp_path)]) == 1
    assert str(report) in capsys.readouterr().err


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "runner"
    root.mkdir()
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    assert not provenance.trusted_results(shard(root / "link/test.py"), Path("a.json"), root)


def test_workflow_lint_rejects_tracked_not_untracked_reports(tmp_path):
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/smoke-tests.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["smoke"]["steps"]
    command = next(s["run"] for s in steps if s.get("name") == "Reject tracked validation output")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path)}
    results = workspace / "test-results"
    results.mkdir()
    (results / "stale__script.json").write_text("{}")
    assert subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env).returncode == 0
    subprocess.run(["git", "-C", str(workspace), "add", "test-results"], check=True)
    run = subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert run.returncode == 1
    assert "test-results/stale__script.json" in run.stdout


def test_foreign_shard_cannot_leave_passing_validation(tmp_path, monkeypatch):
    from heart import validate

    monkeypatch.setenv("PYAUTO_RESULT_ROOT", str(tmp_path / "runner"))
    (tmp_path / "stage.json").write_text(json.dumps({"mode": "rehearsal", "index": "testpypi", "version": "1", "packages": ["demo"]}))
    assert validate.ingest([tmp_path])["release_ready"] is True
    (tmp_path / "foreign__script.json").write_text(json.dumps(shard("/home/other/stale.py")))
    report = validate.ingest([tmp_path])
    assert report["release_ready"] is False
    assert report["validation_outcome"] == "fail"
