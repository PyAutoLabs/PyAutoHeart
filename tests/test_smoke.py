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


# ---------------------------------------------------------------------------
# arcticpy leg (PyAutoHeart#172)
#
# `import autocti` hard-requires arcticpy, which is not a pip dependency. CI gets
# it from the install-arcticpy composite action; a composite action cannot be
# invoked from Python, so the local runner executes the same underlying shell
# script. These tests pin the three properties that keep the two consumers from
# drifting apart, which is what PyAutoHeart#170 exists to prevent.
# ---------------------------------------------------------------------------


def test_arcticpy_defaults_off_and_is_read_from_config(tmp_path):
    config = tmp_path / "repos.yaml"
    config.write_text(
        "smoke:\n"
        "  import_names: {LibraryB: libraryb}\n"
        "  workspaces:\n"
        "    plain:\n"
        "      directory: plain_workspace\n"
        "      chain: [LibraryB]\n"
        "    ctilike:\n"
        "      directory: ctilike_workspace\n"
        "      chain: [LibraryB]\n"
        "      arcticpy: true\n"
    )
    workspaces, _ = smoke.load_smoke_config(config)

    # Absent means False, so every non-CTI workspace is untouched by this
    # feature — the same default the CI input carries.
    assert workspaces["plain"].arcticpy is False
    assert workspaces["ctilike"].arcticpy is True


def test_fingerprint_tracks_the_shared_arcticpy_recipe(tmp_path, monkeypatch):
    """Editing the recipe (or bumping the pin) must invalidate the cache.

    Without this the local runner would keep reusing an environment built from
    the previous recipe, which is exactly the silent-divergence failure the
    single-owner arrangement is meant to make impossible.
    """
    installer = tmp_path / "install_arcticpy.sh"
    installer.write_text("#!/usr/bin/env bash\nset -e\n")
    monkeypatch.setattr(smoke, "ARCTICPY_INSTALLER", installer)

    identity = {"executable": "/fake/python", "version": "3.12.8"}
    off = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",))
    on = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",), True)
    root = make_tree(tmp_path, off)

    def digest(spec):
        return smoke.fingerprint_digest(
            smoke.environment_fingerprint(root, spec, identity)
        )

    # A workspace that does not use arcticpy is not affected by the recipe.
    off_before = digest(off)
    on_before = digest(on)
    assert off_before != on_before

    installer.write_text("#!/usr/bin/env bash\nset -e\n# bumped pin\n")
    assert digest(off) == off_before
    assert digest(on) != on_before


def test_install_runs_the_shared_recipe_before_the_epilogue(tmp_path, monkeypatch):
    """Order matters: CI installs arcticpy *then* runs the workspace epilogue.

    Also asserts the two things the local invocation must get right — an
    explicit interpreter (the environment is prepared, not activated, so a bare
    `python` could install arcticpy where the smoke run never looks) and
    GSL install disabled (a local dev command must not mutate system packages).
    """
    installer = tmp_path / "install_arcticpy.sh"
    installer.write_text("#!/usr/bin/env bash\nset -e\n")
    monkeypatch.setattr(smoke, "ARCTICPY_INSTALLER", installer)

    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",), True)
    root = make_tree(tmp_path, spec)
    environment = tmp_path / "env"
    smoke._environment_bin(environment).mkdir(parents=True)
    smoke._environment_python(environment).write_text("")

    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append(([str(part) for part in command], kwargs.get("env") or {}))
        return completed()

    monkeypatch.setattr(smoke, "_run", fake_run)
    smoke._install_environment(
        environment, root, spec, {"version": "3.12.8"}
    )

    commands = [command for command, _ in calls]
    arctic = next(i for i, c in enumerate(commands) if str(installer) in c)
    epilogue = next(
        i for i, c in enumerate(commands) if "smoke_install.sh" in " ".join(c)
    )
    assert arctic < epilogue

    _, env = calls[arctic]
    assert env["PYTHON"] == str(smoke._environment_python(environment))
    assert env["ARCTICPY_INSTALL_GSL"] == "false"


def test_install_reports_a_missing_recipe_rather_than_skipping_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(smoke, "ARCTICPY_INSTALLER", tmp_path / "absent.sh")
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",), True)
    root = make_tree(tmp_path, spec)
    environment = tmp_path / "env"
    smoke._environment_bin(environment).mkdir(parents=True)
    smoke._environment_python(environment).write_text("")
    monkeypatch.setattr(smoke, "_run", lambda *a, **k: completed())

    # Silently carrying on would surface much later as a confusing
    # `import autocti` failure inside an unrelated script.
    with pytest.raises(smoke.SmokeEnvironmentError, match="arcticpy installer missing"):
        smoke._install_environment(environment, root, spec, {"version": "3.12.8"})


def test_shipped_arcticpy_installer_exists_and_is_the_only_pin():
    """The declared recipe must actually be on disk where smoke.py looks.

    `arcticpy: true` in config/repos.yaml is a promise that this file exists;
    a rename in .github/ would otherwise only surface when someone ran a CTI
    smoke suite locally.
    """
    assert smoke.ARCTICPY_INSTALLER.is_file()
    body = smoke.ARCTICPY_INSTALLER.read_text()
    assert 'ARCTICPY_VERSION="${ARCTICPY_VERSION:-' in body


def _run_installer(tmp_path, prefixes: str) -> subprocess.CompletedProcess[str]:
    """Run the shared recipe far enough to exercise the GSL probe.

    PYTHON points at a stub that fails, so the run stops at the first pip call.
    Everything after the probe is pip work this test has no interest in.
    """
    stub = tmp_path / "python-stub"
    stub.write_text("#!/usr/bin/env bash\nexit 77\n")
    stub.chmod(0o755)
    return subprocess.run(
        ["bash", str(smoke.ARCTICPY_INSTALLER)],
        env={
            "PATH": os.environ["PATH"],
            "ARCTICPY_INSTALL_GSL": "false",
            "ARCTICPY_GSL_PREFIXES": prefixes,
            "PYTHON": str(stub),
        },
        capture_output=True,
        text=True,
    )


def test_recipe_gsl_probe_accepts_headers_in_any_one_prefix(tmp_path):
    """The probe must test each prefix independently.

    The first cut used `ls a b c`, which exits non-zero when ANY operand is
    missing — so on every machine that actually has GSL (in exactly one prefix)
    it reported the headers absent and refused to build. Caught by running the
    thing rather than reading it.
    """
    present = tmp_path / "present"
    (present / "gsl").mkdir(parents=True)
    (present / "gsl" / "gsl_version.h").write_text("")
    absent = tmp_path / "absent"
    absent.mkdir()

    result = _run_installer(tmp_path, f"{absent} {present}")

    assert "found GSL headers" in result.stdout
    assert "GSL headers not found" not in result.stderr


def test_recipe_refuses_without_gsl_and_says_how_to_fix_it(tmp_path):
    """A missing system package must not surface as a compiler error.

    Without this the failure is hundreds of lines into a C++ build naming a
    header, which reads as "arcticpy is broken" rather than "install libgsl-dev".
    """
    absent = tmp_path / "absent"
    absent.mkdir()

    result = _run_installer(tmp_path, str(absent))

    assert result.returncode == 1
    assert "GSL headers not found" in result.stderr
    assert "apt-get install -y libgsl-dev" in result.stderr
    assert "brew install gsl" in result.stderr


def test_recipe_never_reaches_for_sudo_when_gsl_install_is_disabled(tmp_path):
    """The local path must not mutate system packages, ever."""
    present = tmp_path / "present"
    (present / "gsl").mkdir(parents=True)
    (present / "gsl" / "gsl_version.h").write_text("")

    result = _run_installer(tmp_path, str(present))

    combined = result.stdout + result.stderr
    assert "apt-get update" not in combined
    assert "Installing GSL headers" not in combined


# ---------------------------------------------------------------------------
# pip check vs the CTI stack's designed-in conflict
#
# arcticpy declares numpy~=1.21 and is installed with --no-deps on purpose:
# honouring that requirement downgrades numpy below 2.0 and breaks the rest of
# the stack. So `pip check` reports it in EVERY CTI environment, forever. Found
# by running the local runner end to end, not by reading it — the environment
# built correctly and was then destroyed by its own preflight.
# ---------------------------------------------------------------------------


def _pip_check_failing(monkeypatch, output: str):
    def fake_run(command, **kwargs):
        if "check" in [str(part) for part in command]:
            raise subprocess.CalledProcessError(
                1, command, output=output, stderr=""
            )
        return completed()

    monkeypatch.setattr(smoke, "_run", fake_run)


ARCTICPY_CONFLICT = "arcticpy 2.6 has requirement numpy~=1.21, but you have numpy 2.5.2."


def test_pip_check_tolerates_the_arcticpy_numpy_conflict(tmp_path, monkeypatch):
    _pip_check_failing(monkeypatch, ARCTICPY_CONFLICT + "\n")
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",), True)

    smoke._pip_check(Path("/fake/python"), {}, spec)  # must not raise


def test_pip_check_still_fails_on_any_other_conflict(tmp_path, monkeypatch):
    """The exception is one package and one requirement wide, not a blanket skip."""
    _pip_check_failing(
        monkeypatch,
        ARCTICPY_CONFLICT + "\nautofit 1.0 has requirement autoarray>=9, but you have autoarray 1.\n",
    )
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",), True)

    with pytest.raises(smoke.SmokeEnvironmentError) as excinfo:
        smoke._pip_check(Path("/fake/python"), {}, spec)
    message = str(excinfo.value)
    assert "autofit 1.0 has requirement" in message
    assert "arcticpy" not in message


def test_pip_check_does_not_tolerate_arcticpy_for_a_non_arcticpy_workspace(
    tmp_path, monkeypatch
):
    """A workspace that never asked for arcticpy has no business carrying it."""
    _pip_check_failing(monkeypatch, ARCTICPY_CONFLICT + "\n")
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",))

    with pytest.raises(smoke.SmokeEnvironmentError, match="arcticpy"):
        smoke._pip_check(Path("/fake/python"), {}, spec)


def test_pip_check_passes_through_when_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "_run", lambda *a, **k: completed())
    spec = smoke.WorkspaceSpec("demo", "demo_workspace", ("LibraryB",), True)

    smoke._pip_check(Path("/fake/python"), {}, spec)  # must not raise
