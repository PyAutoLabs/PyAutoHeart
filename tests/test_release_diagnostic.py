"""The diagnostic must fail closed and capture the live child before killing it."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_diagnostic", ROOT / ".github/scripts/release_diagnostic.py"
)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def manifest():
    return diag.load_manifest(ROOT / "diagnostics/release-37199991757.json")


@pytest.mark.parametrize(
    "key,value",
    [
        ("workspace_sha", "main"),
        ("hands_sha", "../bad"),
        ("python", "3.12"),
        ("script", "../../outside.py"),
        ("script", "/tmp/outside.py"),
        ("workspace_repo", "../repo"),
        ("packages", {}),
        ("schema_version", 2),
    ],
)
def test_rejects_unpinned_or_escaping_manifest(tmp_path, key, value):
    data = manifest()
    data[key] = value
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        diag.load_manifest(path)


def test_prepared_pins_retain_failed_runtime_and_no_release_artifact(tmp_path):
    data = manifest()
    diag.prepare(data, tmp_path)
    requirements = (tmp_path / "requirements.txt").read_text()
    assert "jax==0.10.2\n" in requirements
    assert "jaxlib==0.10.2\n" in requirements
    assert len(requirements.splitlines()) == 115
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "manifest.json",
        "requirements.txt",
    ]


def test_runtime_and_source_shadowing_fail_provenance(monkeypatch, tmp_path):
    data = manifest()
    monkeypatch.setattr(diag.subprocess, "check_output", lambda *a, **kw: "0" * 40)
    monkeypatch.setattr(diag.platform, "python_version", lambda: "3.12.10")
    monkeypatch.setattr(
        diag.importlib.metadata, "version", lambda name: data["packages"][name]
    )
    monkeypatch.setattr(
        diag.importlib.util,
        "find_spec",
        lambda name: type("Spec", (), {"origin": f"/source/{name}/__init__.py"})(),
    )
    receipt = diag.provenance(data, tmp_path, tmp_path)
    assert receipt["diagnostic_only"]
    assert "Python patch version differs from manifest" in receipt["errors"]
    assert "workspace commit differs from manifest" in receipt["errors"]
    assert any("autolens is not imported" in error for error in receipt["errors"])


def test_live_dump_precedes_timeout_and_child_is_reaped(tmp_path):
    captured = []

    def dump(pid, output):
        os.kill(pid, 0)  # must still exist when sampled
        captured.append(pid)
        output.write_text("live stack witness")

    result = diag.trial(
        [
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(20)",
        ],
        tmp_path,
        os.environ.copy(),
        tmp_path / "trial.log",
        cap=0.3,
        dump_after=0.1,
        dump=dump,
    )
    assert result["status"] == "timeout"
    assert result["elapsed_s"] < 3
    assert "started" in (tmp_path / "trial.log").read_text()
    assert (tmp_path / "trial.native.txt").read_text() == "live stack witness"
    with pytest.raises(ProcessLookupError):
        os.kill(captured[0], 0)


def test_failure_exit_and_output_are_not_reported_as_pass(tmp_path):
    result = diag.trial(
        [sys.executable, "-c", "print('adverse evidence'); raise SystemExit(7)"],
        tmp_path,
        os.environ.copy(),
        tmp_path / "failed.log",
        cap=3,
        dump_after=1,
    )
    assert result["status"] == "fail"
    assert result["returncode"] == 7
    assert "adverse evidence" in (tmp_path / "failed.log").read_text()


def test_native_helper_failure_is_preserved(monkeypatch, tmp_path):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("diagnostic helper unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    output = tmp_path / "native.txt"
    diag.native_dump(os.getpid(), output)
    assert output.read_text().count("diagnostic helper unavailable") == 2


def test_repeats_cannot_exceed_bounded_budget(tmp_path):
    with pytest.raises(SystemExit) as exc:
        diag.main(
            [
                "prepare",
                "--manifest",
                str(ROOT / "diagnostics/release-37199991757.json"),
                "--output",
                str(tmp_path),
                "--repeats",
                "7",
            ]
        )
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "control,candidate,signature,expected",
    [
        ("timeout", "pass", True, "captured-stall-avoided"),
        ("timeout", "pass", False, "inconclusive"),
        ("pass", "pass", False, "inconclusive"),
        ("timeout", "timeout", True, "candidate-failed"),
        ("fail", "pass", False, "control-failed"),
    ],
)
def test_comparison_requires_native_control_and_passing_candidate(
    control, candidate, signature, expected
):
    rows = [
        {"arm": "control", "status": control, "cholesky_pool_stall": signature},
        {"arm": "candidate", "status": candidate},
    ]
    assert diag.comparison_verdict(rows, 2) == expected
    partial = (
        expected if expected in {"candidate-failed", "control-failed"} else "incomplete"
    )
    assert diag.comparison_verdict(rows, 4) == partial
    assert diag.comparison_verdict(list(reversed(rows)), 2) == "invalid-order"


def test_comparison_interleaves_and_preserves_control_failures_and_venv_path(
    tmp_path, monkeypatch
):
    from argparse import Namespace

    python = tmp_path / "candidate-python"
    python.symlink_to(sys.executable)
    args = Namespace(
        candidate_python=python,
        repeats=6,
        manifest=ROOT / "diagnostics/release-37199991757.json",
        workspace=tmp_path,
        hands=tmp_path,
    )
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        candidate = "--candidate" in command
        directory = Path(command[command.index("--output") + 1])
        directory.mkdir()
        result = {
            "status": "pass" if candidate else "timeout",
            "returncode": 0 if candidate else -9,
        }
        (directory / "diagnostic-results.json").write_text(
            json.dumps({"trials": [result]})
        )
        if not candidate:
            (directory / "trial-1.native.txt").write_text(
                "BlockingCounter::Wait ParallelBatchMap CholeskyFactorization\n" * 4
            )
        return subprocess.CompletedProcess(command, 0 if candidate else 1)

    monkeypatch.setattr(diag.subprocess, "run", run)
    assert diag.compare(args, manifest(), tmp_path) == 0
    assert ["--candidate" in command for command in commands] == [False, True] * 3
    assert all(command[0] == str(python.absolute()) for command in commands[1::2])
    report = json.loads((tmp_path / "comparison.json").read_text())
    assert [row["status"] for row in report["trials"]] == ["timeout", "pass"] * 3
    with pytest.raises(ValueError, match="preserve previous evidence"):
        diag.compare(args, manifest(), tmp_path)


def test_candidate_preparation_changes_only_the_runtime_pair(tmp_path):
    diag.main(
        [
            "prepare",
            "--candidate",
            "--manifest",
            str(ROOT / "diagnostics/release-37199991757.json"),
            "--output",
            str(tmp_path),
        ]
    )
    actual = json.loads((tmp_path / "manifest.json").read_text())
    original = manifest()
    changed = {
        key
        for key in original["packages"]
        if actual["packages"][key] != original["packages"][key]
    }
    assert changed == {"jax", "jaxlib"}
    assert actual["packages"]["jax"] == actual["packages"]["jaxlib"] == "0.11.2"
    assert actual["workspace_sha"] == original["workspace_sha"]


def test_early_candidate_failure_has_adverse_durable_verdict(tmp_path, monkeypatch):
    from argparse import Namespace

    args = Namespace(
        candidate_python=Path(sys.executable),
        repeats=6,
        manifest=ROOT / "diagnostics/release-37199991757.json",
        workspace=tmp_path,
        hands=tmp_path,
    )

    def run(command, **kwargs):
        candidate = "--candidate" in command
        directory = Path(command[command.index("--output") + 1])
        directory.mkdir()
        result = {
            "status": "fail" if candidate else "pass",
            "returncode": 7 if candidate else 0,
        }
        (directory / "diagnostic-results.json").write_text(
            json.dumps({"trials": [result]})
        )
        return subprocess.CompletedProcess(command, 1 if candidate else 0)

    monkeypatch.setattr(diag.subprocess, "run", run)
    assert diag.compare(args, manifest(), tmp_path) == 1
    report = json.loads((tmp_path / "comparison.json").read_text())
    assert report["verdict"] == "candidate-failed"
    assert len(report["trials"]) == 2
    assert report["trials"][1]["returncode"] == 7
