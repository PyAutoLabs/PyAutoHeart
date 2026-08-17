from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from heart import smoke


# Fixture repo and root names are deliberately synthetic (LibraryA/LibraryB
# chains under an `organism` root): nothing in smoke.py matches a spec against
# a real repo list, so a real name here would be an instance fact in organ code
# — the tenant firewall's concern (PyAutoMind/scripts/repos_sync.py) — for no
# test value. Keep them synthetic.
def make_tree(tmp_path: Path, spec: smoke.WorkspaceSpec) -> Path:
    root = tmp_path / "organism"
    workspace = root / spec.directory
    (workspace / ".github" / "scripts").mkdir(parents=True)
    (workspace / ".github" / "scripts" / "smoke_install.sh").write_text(
        "#!/usr/bin/env bash\nset -e\n"
    )
    for repo in spec.chain:
        repo_root = root / repo
        repo_root.mkdir(parents=True)
        (repo_root / "pyproject.toml").write_text(
            f'[project]\nname = "{repo.lower()}"\nversion = "1"\n'
        )
    (root / "PyAutoHands" / "autohands").mkdir(parents=True)
    return root


def completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def test_fingerprint_changes_with_installer_and_dependency_metadata(tmp_path):
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",))
    root = make_tree(tmp_path, spec)
    identity = {"executable": "/usr/bin/python3", "version": "3.12.8"}

    original = smoke.fingerprint_digest(
        smoke.environment_fingerprint(root, spec, identity)
    )
    installer = root / spec.directory / ".github" / "scripts" / "smoke_install.sh"
    installer.write_text(installer.read_text() + "pip install example\n")
    installer_changed = smoke.fingerprint_digest(
        smoke.environment_fingerprint(root, spec, identity)
    )
    (root / "LibraryB" / "pyproject.toml").write_text(
        '[project]\nname = "libraryb"\nversion = "2"\n'
    )
    metadata_changed = smoke.fingerprint_digest(
        smoke.environment_fingerprint(root, spec, identity)
    )

    assert original != installer_changed
    assert installer_changed != metadata_changed


def test_runtime_environment_replaces_ambient_python_and_pyauto_state(
    tmp_path, monkeypatch
):
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryA", "LibraryB"))
    root = make_tree(tmp_path, spec)
    environment = tmp_path / "environment"
    smoke._environment_bin(environment).mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", "/ambient/leak")
    monkeypatch.setenv("PYAUTO_TEST_MODE", "unexpected")

    env = smoke.runtime_environment(environment, root, spec, tmp_path / "state")

    assert "/ambient/leak" not in env["PYTHONPATH"]
    assert env["PYTHONPATH"].split(os.pathsep) == [
        str(root / "LibraryA"),
        str(root / "LibraryB"),
        str(root / "PyAutoHands" / "autohands"),
    ]
    assert "PYAUTO_TEST_MODE" not in env
    assert env["PATH"].split(os.pathsep)[0] == str(smoke._environment_bin(environment))
    assert Path(env["NUMBA_CACHE_DIR"]).is_relative_to(tmp_path / "state")


def test_prepare_reuses_cache_then_rebuilds_after_metadata_change(
    tmp_path, monkeypatch
):
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",))
    root = make_tree(tmp_path, spec)
    state = tmp_path / "state"
    identity = {"executable": "/fake/python", "version": "3.12.8"}
    installs: list[Path] = []
    preflights: list[Path] = []

    monkeypatch.setattr(smoke, "_python_identity", lambda _: identity)

    def fake_run(command, **kwargs):
        if "venv" in [str(part) for part in command]:
            environment = Path(command[-1])
            python = smoke._environment_python(environment)
            python.parent.mkdir(parents=True)
            python.write_text("")
        return completed()

    monkeypatch.setattr(smoke, "_run", fake_run)
    monkeypatch.setattr(
        smoke,
        "_install_environment",
        lambda environment, *_: installs.append(environment),
    )
    monkeypatch.setattr(
        smoke,
        "_preflight",
        lambda environment, *_: preflights.append(environment),
    )

    first, first_built = smoke.prepare_environment(root, state, spec)
    second, second_built = smoke.prepare_environment(root, state, spec)
    (root / "LibraryB" / "pyproject.toml").write_text(
        '[project]\nname = "libraryb"\nversion = "2"\n'
    )
    third, third_built = smoke.prepare_environment(root, state, spec)

    assert first == second == third
    assert (first_built, second_built, third_built) == (True, False, True)
    assert len(installs) == 2
    assert len(preflights) == 3
    assert smoke.cache_matches(
        third, smoke.environment_fingerprint(root, spec, identity)
    )


def test_failed_rebuild_restores_previous_complete_environment(tmp_path, monkeypatch):
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",))
    root = make_tree(tmp_path, spec)
    state = tmp_path / "state"
    identity = {"executable": "/fake/python", "version": "3.12.8"}
    monkeypatch.setattr(smoke, "_python_identity", lambda _: identity)

    def fake_run(command, **kwargs):
        if "venv" in [str(part) for part in command]:
            environment = Path(command[-1])
            python = smoke._environment_python(environment)
            python.parent.mkdir(parents=True)
            python.write_text("")
        return completed()

    monkeypatch.setattr(smoke, "_run", fake_run)
    monkeypatch.setattr(smoke, "_install_environment", lambda *_: None)
    monkeypatch.setattr(smoke, "_preflight", lambda *_: None)
    target, _ = smoke.prepare_environment(root, state, spec)
    old_marker = (target / smoke.MARKER_NAME).read_text()
    (root / "LibraryB" / "pyproject.toml").write_text(
        '[project]\nname = "libraryb"\nversion = "2"\n'
    )
    monkeypatch.setattr(
        smoke,
        "_install_environment",
        lambda *_: (_ for _ in ()).throw(smoke.SmokeEnvironmentError("install failed")),
    )

    with pytest.raises(smoke.SmokeEnvironmentError, match="install failed"):
        smoke.prepare_environment(root, state, spec)

    assert smoke._environment_python(target).is_file()
    assert (target / smoke.MARKER_NAME).read_text() == old_marker


def test_workspace_installer_is_the_dependency_source_of_truth(tmp_path, monkeypatch):
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryA",))
    root = make_tree(tmp_path, spec)
    environment = tmp_path / "environment"
    python = smoke._environment_python(environment)
    python.parent.mkdir(parents=True)
    python.write_text("")
    calls = []
    monkeypatch.setattr(
        smoke,
        "_run",
        lambda command, **kwargs: calls.append(
            ([str(item) for item in command], kwargs)
        )
        or completed(),
    )

    smoke._install_environment(
        environment,
        root,
        spec,
        {"executable": "/fake/python", "version": "3.12.8"},
    )

    installer = root / spec.directory / ".github" / "scripts" / "smoke_install.sh"
    assert any(command == ["bash", str(installer)] for command, _ in calls)
    assert all("./LibraryA" not in command for command, _ in calls)
    installer_call = next(kwargs for command, kwargs in calls if command[0] == "bash")
    assert installer_call["cwd"] == root
    assert installer_call["env"]["PYTHON_VERSION"] == "3.12"


def test_legacy_installer_derives_optional_extras_from_pyproject(tmp_path, monkeypatch):
    spec = smoke.WorkspaceSpec("legacy", "legacy_workspace", ("LibraryB",))
    root = make_tree(tmp_path, spec)
    (root / spec.directory / ".github" / "scripts" / "smoke_install.sh").unlink()
    (root / "LibraryB" / "pyproject.toml").write_text("""
[project]
name = "libraryb"
version = "1"

[project.optional-dependencies]
optional = ["nufftax>=0.6"]
""".strip() + "\n")
    environment = tmp_path / "environment"
    python = smoke._environment_python(environment)
    python.parent.mkdir(parents=True)
    python.write_text("")
    commands = []
    monkeypatch.setattr(
        smoke,
        "_run",
        lambda command, **kwargs: commands.append([str(item) for item in command])
        or completed(),
    )

    smoke._install_environment(
        environment,
        root,
        spec,
        {"executable": "/fake/python", "version": "3.12.8"},
    )

    assert any("./LibraryB" in command for command in commands)
    assert any("./LibraryB[optional]" in command for command in commands)
    assert all("nufftax" not in command for command in commands)


def test_preflight_rejects_jupyter_kernel_from_another_interpreter(
    tmp_path, monkeypatch
):
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ())
    root = make_tree(tmp_path, spec)
    (root / spec.directory / "smoke_notebooks.txt").write_text("intro.ipynb\n")
    environment = tmp_path / "environment"
    python = smoke._environment_python(environment)
    python.parent.mkdir(parents=True)
    python.write_text("")
    jupyter = smoke._environment_bin(environment) / "jupyter"
    jupyter.write_text("")

    def fake_run(command, **kwargs):
        words = [str(item) for item in command]
        if words[-3:] == ["kernelspec", "list", "--json"]:
            return completed(
                json.dumps(
                    {
                        "kernelspecs": {
                            "python3": {
                                "spec": {
                                    "argv": ["/usr/bin/python3", "-m", "ipykernel"]
                                }
                            }
                        }
                    }
                )
            )
        if "pathlib.Path(sys.executable).absolute()" in " ".join(words):
            return completed(str(python.absolute()) + "\n")
        return completed("{}\n")

    monkeypatch.setattr(smoke, "_run", fake_run)

    with pytest.raises(smoke.SmokeEnvironmentError, match="Jupyter kernel"):
        smoke._preflight(environment, root, spec, tmp_path / "state")


def test_preflight_keeps_venv_python_path_when_it_is_a_symlink(tmp_path, monkeypatch):
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ())
    root = make_tree(tmp_path, spec)
    environment = tmp_path / "environment"
    python = smoke._environment_python(environment)
    python.parent.mkdir(parents=True)
    python.symlink_to("/usr/bin/python3.12")
    commands = []

    def fake_run(command, **kwargs):
        words = [str(item) for item in command]
        commands.append(words)
        if "pathlib.Path(sys.executable).absolute()" in " ".join(words):
            return completed(str(python.absolute()) + "\n")
        return completed("{}\n")

    monkeypatch.setattr(smoke, "_run", fake_run)

    smoke._preflight(environment, root, spec, tmp_path / "state")

    assert commands
    assert all(command[0] == str(python.absolute()) for command in commands)


def test_preflight_accepts_relative_kernel_resolved_inside_environment(
    tmp_path, monkeypatch
):
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ())
    root = make_tree(tmp_path, spec)
    (root / spec.directory / "smoke_notebooks.txt").write_text("intro.ipynb\n")
    environment = tmp_path / "environment"
    python = smoke._environment_python(environment)
    python.parent.mkdir(parents=True)
    python.write_text("")
    python.chmod(0o755)
    (smoke._environment_bin(environment) / "jupyter").write_text("")

    def fake_run(command, **kwargs):
        words = [str(item) for item in command]
        if words[-3:] == ["kernelspec", "list", "--json"]:
            return completed(
                json.dumps(
                    {
                        "kernelspecs": {
                            "python3": {"spec": {"argv": ["python", "-m", "ipykernel"]}}
                        }
                    }
                )
            )
        if "pathlib.Path(sys.executable).absolute()" in " ".join(words):
            return completed(str(python.absolute()) + "\n")
        return completed("{}\n")

    monkeypatch.setattr(smoke, "_run", fake_run)

    smoke._preflight(environment, root, spec, tmp_path / "state")


def test_safe_remove_refuses_paths_outside_smoke_cache(tmp_path):
    cache = tmp_path / "state" / "smoke-envs"
    outside = tmp_path / "do-not-delete"
    outside.mkdir()

    with pytest.raises(smoke.SmokeEnvironmentError, match="unsafe cache path"):
        smoke._safe_remove_environment(outside, cache)

    assert outside.is_dir()


def test_run_workspace_uses_prepared_python_and_isolated_environment(
    tmp_path, monkeypatch
):
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryA",))
    root = make_tree(tmp_path, spec)
    runner = root / spec.directory / ".github" / "scripts" / "run_smoke.py"
    runner.write_text("")
    environment = tmp_path / "environment"
    python = smoke._environment_python(environment)
    python.parent.mkdir(parents=True)
    python.write_text("")
    calls = []
    monkeypatch.setenv("PYTHONPATH", "/ambient/leak")
    monkeypatch.setattr(smoke, "_wipe_output", lambda _: None)
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0),
    )

    result = smoke.run_workspace(environment, root, tmp_path / "state", spec)

    assert result == 0
    assert calls[0][0] == [str(python), str(runner)]
    assert calls[0][1]["cwd"] == root / spec.directory
    assert "/ambient/leak" not in calls[0][1]["env"]["PYTHONPATH"]


def test_cli_help_exposes_isolation_and_rebuild_contract():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["bash", str(root / "bin" / "pyauto-heart"), "help", "smoke"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "separate, cached Python environment" in result.stdout
    assert "--prepare-only" in result.stdout
    assert "--rebuild" in result.stdout


def test_shell_wrapper_uses_explicit_smoke_python_before_module_import():
    root = Path(__file__).resolve().parents[1]
    script = (root / "bin" / "pyauto-heart").read_text()
    body = script[script.index("cmd_smoke()") : script.index("help_url_check()")]

    assert "--python) next_is_python=1" in body
    assert 'exec env PYTHONPATH="$HEART_HOME" "$runner_python" -m heart.smoke' in body
