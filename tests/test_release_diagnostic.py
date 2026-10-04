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
