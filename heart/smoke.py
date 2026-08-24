"""Reproducible local execution for the workspace smoke suites.

CI already gives every workspace a clean interpreter and lets the workspace
own its dependency epilogue.  The old local skill skipped that first half and
ran against whichever environment invoked it.  This module makes the local
contract the same as CI: one isolated environment per workspace, prepared from
the workspace-owned installer and checked before an expensive script starts.

Heart itself still depends only on the standard library plus PyYAML.  The
scientific stack is installed into child environments under HEART_STATE_DIR.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

FINGERPRINT_SCHEMA = 1
MARKER_NAME = ".pyauto-smoke-environment.json"

HEART_HOME = Path(__file__).resolve().parents[1]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"


@dataclass(frozen=True)
class WorkspaceSpec:
    key: str
    directory: str
    chain: tuple[str, ...]
    #: Build arcticpy into this workspace's environment before running its
    #: install epilogue. Mirrors the ``arcticpy: true`` input the CTI repos'
    #: CI callers already pass to the reusable ``smoke-tests.yml`` — declared
    #: rather than inferred from the chain's contents, so the local runner
    #: and CI are configured by the same explicit statement.
    arcticpy: bool = False


#: The one place the arcticpy recipe lives, shared with the CI composite action
#: that wraps it (``.github/actions/install-arcticpy/action.yml``). A composite
#: action cannot be invoked from Python, so the *script* is what the two
#: consumers have in common; duplicating the recipe here in Python would
#: re-create the divergence PyAutoHeart#170 removed.
ARCTICPY_INSTALLER = (
    HEART_HOME / ".github" / "actions" / "install-arcticpy" / "install_arcticpy.sh"
)


def load_smoke_config(
    config_path: Path | str = CONFIG_PATH,
) -> tuple[dict[str, WorkspaceSpec], dict[str, str]]:
    """Workspace specs + repo -> import-name map, from the policy file's
    ``smoke`` block. Strict: a missing block is a config bug and fails
    loudly rather than silently preparing nothing (the ``version_skew``
    idiom). Which workspaces exist is instance policy, so it lives in
    ``config/repos.yaml`` — the declared surface an adopting fork replaces —
    not in this module."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    block = cfg["smoke"]
    workspaces = {
        key: WorkspaceSpec(
            key,
            spec["directory"],
            tuple(spec["chain"]),
            bool(spec.get("arcticpy", False)),
        )
        for key, spec in block["workspaces"].items()
    }
    return workspaces, dict(block["import_names"])


WORKSPACES, IMPORT_NAMES = load_smoke_config()


class SmokeEnvironmentError(RuntimeError):
    """Raised before smoke execution when its environment is not trustworthy."""


def _run(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def _python_identity(python: str) -> dict[str, str]:
    result = _run(
        [
            python,
            "-c",
            "import json,platform,sys; "
            "print(json.dumps({'executable':sys.executable,"
            "'version':platform.python_version()}))",
        ],
        capture_output=True,
    )
    return json.loads(result.stdout)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def environment_fingerprint(
    organism_root: Path,
    spec: WorkspaceSpec,
    python_identity: Mapping[str, str],
) -> dict:
    """Return the complete, serialisable cache contract for one workspace."""
    workspace = organism_root / spec.directory
    watched = [workspace / ".github" / "scripts" / "smoke_install.sh"]
    watched.extend(organism_root / repo / "pyproject.toml" for repo in spec.chain)
    files = {
        str(path.relative_to(organism_root)): _sha256(path)
        for path in watched
        if path.is_file()
    }
    if spec.arcticpy and ARCTICPY_INSTALLER.is_file():
        # Keyed by a fixed label rather than a path relative to organism_root:
        # the installer lives in the Heart checkout, which is not required to
        # sit under the organism root at all. Hashing it means editing the
        # recipe -- or bumping the arcticpy pin -- invalidates the cached
        # environment, instead of silently reusing one built from the old one.
        files["PyAutoHeart:install_arcticpy.sh"] = _sha256(ARCTICPY_INSTALLER)
    return {
        "schema": FINGERPRINT_SCHEMA,
        "workspace": spec.key,
        "chain": list(spec.chain),
        "python": dict(python_identity),
        "platform": platform.platform(),
        "files": files,
    }


def fingerprint_digest(fingerprint: Mapping) -> str:
    encoded = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _environment_python(environment: Path) -> Path:
    folder = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return environment / folder / executable


def _environment_bin(environment: Path) -> Path:
    return environment / ("Scripts" if os.name == "nt" else "bin")


def environment_path(cache_root: Path, spec: WorkspaceSpec, identity: Mapping) -> Path:
    major_minor = ".".join(str(identity["version"]).split(".")[:2])
    return cache_root / spec.key / f"py{major_minor}"


def _read_marker(environment: Path) -> dict | None:
    marker = environment / MARKER_NAME
    try:
        return json.loads(marker.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def cache_matches(environment: Path, fingerprint: Mapping) -> bool:
    marker = _read_marker(environment)
    return bool(
        _environment_python(environment).is_file()
        and marker
        and marker.get("digest") == fingerprint_digest(fingerprint)
    )


def _installer_environment(environment: Path, python_version: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    for name in tuple(env):
        if name.startswith("PYAUTO_") and name not in {"PYAUTO_ROOT"}:
            env.pop(name)
    env.update(
        {
            "PATH": os.pathsep.join(
                (str(_environment_bin(environment)), env.get("PATH", ""))
            ),
            "VIRTUAL_ENV": str(environment),
            "PYTHONNOUSERSITE": "1",
            "PYTHON_VERSION": ".".join(python_version.split(".")[:2]),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    return env


def runtime_environment(
    environment: Path,
    organism_root: Path,
    spec: WorkspaceSpec,
    state_root: Path,
) -> dict[str, str]:
    """Build a leak-resistant child environment using live local source."""
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("PYAUTO_"):
            env.pop(name)
    source_paths = [str(organism_root / repo) for repo in spec.chain]
    source_paths.append(str(organism_root / "PyAutoHands" / "autohands"))
    cache_dir = state_root / "smoke-runtime" / spec.key
    (cache_dir / "numba").mkdir(parents=True, exist_ok=True)
    (cache_dir / "matplotlib").mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "PATH": os.pathsep.join(
                (str(_environment_bin(environment)), env.get("PATH", ""))
            ),
            "VIRTUAL_ENV": str(environment),
            "PYTHONNOUSERSITE": "1",
            # Deliberately replace, rather than extend, ambient PYTHONPATH.
            "PYTHONPATH": os.pathsep.join(source_paths),
            "PYAUTO_ROOT": str(organism_root),
            "JAX_ENABLE_X64": "True",
            "NUMBA_CACHE_DIR": str(cache_dir / "numba"),
            "MPLCONFIGDIR": str(cache_dir / "matplotlib"),
        }
    )
    return env


def _optional_local_targets(organism_root: Path, chain: Iterable[str]) -> list[str]:
    targets: list[str] = []
    for repo in chain:
        pyproject = organism_root / repo / "pyproject.toml"
        if not pyproject.is_file():
            continue
        with pyproject.open("rb") as stream:
            data = tomllib.load(stream)
        extras = data.get("project", {}).get("optional-dependencies", {})
        if "optional" in extras:
            targets.append(f"./{repo}[optional]")
    return targets


def _install_arcticpy(python: Path, env: dict[str, str]) -> None:
    """Build arcticpy into ``python``'s environment via the shared recipe.

    Runs the very script the CI composite action runs, so there is exactly one
    recipe, and one pin, in the organism. The GSL leg is disabled: a local
    ``pyauto-heart smoke`` must not mutate system packages
    (and ``apt-get`` does not exist on macOS), so the script proves the headers
    are present and fails with the install line rather than reaching for sudo.

    ``PYTHON`` is passed explicitly rather than relying on the venv being first
    on PATH -- this environment is prepared, not activated, and a bare
    ``python`` resolving to the wrong interpreter would install arcticpy
    somewhere the smoke run never looks.
    """
    if not ARCTICPY_INSTALLER.is_file():
        raise SmokeEnvironmentError(
            f"arcticpy installer missing: {ARCTICPY_INSTALLER}. This workspace "
            "declares `arcticpy: true` in the `smoke:` block of "
            "config/repos.yaml, which requires the shared recipe."
        )
    _run(
        ["bash", str(ARCTICPY_INSTALLER)],
        env={
            **env,
            "PYTHON": str(python),
            "ARCTICPY_INSTALL_GSL": "false",
        },
    )


def _install_environment(
    environment: Path,
    organism_root: Path,
    spec: WorkspaceSpec,
    identity: Mapping[str, str],
) -> None:
    python = _environment_python(environment)
    env = _installer_environment(environment, identity["version"])
    _run(
        [python, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
        env=env,
    )
    _run([python, "-m", "pip", "install", "pyyaml"], env=env)

    if spec.arcticpy:
        _install_arcticpy(python, env)

    workspace = organism_root / spec.directory
    installer = workspace / ".github" / "scripts" / "smoke_install.sh"
    if installer.is_file():
        _run(["bash", installer], cwd=organism_root, env=env)
        return

    # Legacy workspaces have no CI epilogue yet.  Install their local chain and
    # discover the conventional `optional` extras from package metadata; Heart
    # does not carry a second list of third-party packages.
    local_targets = [f"./{repo}" for repo in spec.chain]
    _run([python, "-m", "pip", "install", *local_targets], cwd=organism_root, env=env)
    optional_targets = _optional_local_targets(organism_root, spec.chain)
    if optional_targets:
        _run(
            [python, "-m", "pip", "install", *optional_targets],
            cwd=organism_root,
            env=env,
        )
    notebooks = workspace / "smoke_notebooks.txt"
    if notebooks.is_file() and notebooks.read_text().strip():
        _run(
            [
                python,
                "-m",
                "pip",
                "install",
                "jupyter",
                "nbconvert",
                "ipynb-py-convert",
            ],
            env=env,
        )


#: `pip check` lines that a CTI environment ALWAYS produces, and must.
#:
#: arcticpy declares ``numpy~=1.21`` but is installed with ``--no-deps`` on
#: purpose: honouring that requirement downgrades numpy below 2.0 and breaks the
#: rest of the PyAuto stack. The dependency metadata therefore stays permanently
#: unsatisfied, and ``pip check`` reports it every single time:
#:
#:     arcticpy 2.6 has requirement numpy~=1.21, but you have numpy 2.5.2.
#:
#: Without this exception the preflight kills every CTI environment immediately
#: after building it, which would make `arcticpy: true` useless. The exception is
#: deliberately narrow -- one package, one requirement -- so a real conflict, in
#: arcticpy or anything else, still fails the preflight.
_ARCTICPY_EXPECTED_CONFLICT = re.compile(
    r"^arcticpy \S+ has requirement numpy\S+, but you have numpy \S+\.?$"
)


def _pip_check(python: Path, env: Mapping[str, str], spec: WorkspaceSpec) -> None:
    """Run ``pip check``, tolerating only the CTI stack's designed-in conflict."""
    # Goes through _run (not subprocess directly) so it stays consistent with
    # every other command here -- same env handling, same mockability.
    try:
        _run([python, "-m", "pip", "check"], env=env, capture_output=True)
        return
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or "") + (exc.stderr or "")

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if spec.arcticpy:
        lines = [
            line
            for line in lines
            if not _ARCTICPY_EXPECTED_CONFLICT.match(line)
            and line != "No broken requirements found."
        ]
    if lines:
        raise SmokeEnvironmentError(
            "pip check reported broken requirements in "
            f"{python}:\n  " + "\n  ".join(lines)
        )


def _preflight(
    environment: Path,
    organism_root: Path,
    spec: WorkspaceSpec,
    state_root: Path,
) -> None:
    """Prove dependencies and executables resolve inside the intended env."""
    # Do not resolve this symlink: venv/bin/python commonly points at the base
    # interpreter, and collapsing it would make every following `-m` command
    # escape the environment we are trying to prove.
    python = _environment_python(environment).absolute()
    env = runtime_environment(environment, organism_root, spec, state_root)
    executable = _run(
        [
            python,
            "-c",
            "import pathlib,sys; print(pathlib.Path(sys.executable).absolute())",
        ],
        env=env,
        capture_output=True,
    ).stdout.strip()
    if Path(executable) != python:
        raise SmokeEnvironmentError(
            f"interpreter leak: expected {python}, subprocess used {executable}"
        )
    _pip_check(python, env, spec)

    expected = {
        IMPORT_NAMES[repo]: str((organism_root / repo).resolve())
        for repo in spec.chain
        if repo in IMPORT_NAMES
    }
    probe = (
        "import importlib,json,pathlib,sys; expected=json.loads(sys.argv[1]); "
        "bad=[]; "
        "[(bad.append(f'{name} -> {path}') if not pathlib.Path(path).resolve().is_relative_to(pathlib.Path(root)) else None) "
        "for name,root in expected.items() "
        "for path in [importlib.import_module(name).__file__]]; "
        "print(json.dumps({'imports':expected,'errors':bad})); "
        "raise SystemExit(bool(bad))"
    )
    _run([python, "-c", probe, json.dumps(expected)], env=env, capture_output=True)

    notebook_list = organism_root / spec.directory / "smoke_notebooks.txt"
    if notebook_list.is_file() and notebook_list.read_text().strip():
        jupyter = _environment_bin(environment) / "jupyter"
        if not jupyter.is_file():
            raise SmokeEnvironmentError(f"jupyter is missing from {environment}")
        kernels = json.loads(
            _run(
                [jupyter, "kernelspec", "list", "--json"],
                env=env,
                capture_output=True,
            ).stdout
        ).get("kernelspecs", {})
        kernel = kernels.get("python3", {}).get("spec", {}).get("argv", [])
        kernel_python = None
        if kernel:
            candidate = Path(kernel[0])
            if candidate.is_absolute():
                kernel_python = candidate
            else:
                found = shutil.which(kernel[0], path=env["PATH"])
                kernel_python = Path(found) if found else None
        if kernel_python is None or kernel_python.absolute() != python:
            raise SmokeEnvironmentError(
                f"python3 Jupyter kernel does not use {python}: {kernel or 'missing'}"
            )


def _safe_remove_environment(path: Path, cache_root: Path) -> None:
    resolved = path.resolve()
    root = cache_root.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise SmokeEnvironmentError(f"refusing to remove unsafe cache path: {path}")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


@contextlib.contextmanager
def _environment_lock(target: Path):
    """Serialise preparation without placing a lock inside the replaceable env."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f".{target.name}.lock"
    with lock_path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _prune_abandoned_stages(target: Path, cache_root: Path) -> None:
    """Clean staging directories left by the pre-v1, relocatable-venv prototype."""
    for stale in target.parent.glob(f".{target.name}.build-*"):
        _safe_remove_environment(stale, cache_root)


def prepare_environment(
    organism_root: Path,
    state_root: Path,
    spec: WorkspaceSpec,
    *,
    python: str = sys.executable,
    rebuild: bool = False,
) -> tuple[Path, bool]:
    identity = _python_identity(python)
    cache_root = state_root / "smoke-envs"
    target = environment_path(cache_root, spec, identity)
    with _environment_lock(target):
        _prune_abandoned_stages(target, cache_root)
        fingerprint = environment_fingerprint(organism_root, spec, identity)
        if not rebuild and cache_matches(target, fingerprint):
            try:
                _preflight(target, organism_root, spec, state_root)
                return target, False
            except (SmokeEnvironmentError, subprocess.CalledProcessError, OSError):
                print(f"[{spec.key}] cached environment failed preflight; rebuilding")

        # A venv is not relocatable: console-script shebangs embed its creation
        # path. Build at the final path under a lock, retaining the previous
        # complete environment as a rollback backup until preflight succeeds.
        backup = target.with_name(f".{target.name}.old-{uuid.uuid4().hex}")
        if target.exists() or target.is_symlink():
            target.rename(backup)
        try:
            _run([python, "-m", "venv", target])
            _install_environment(target, organism_root, spec, identity)
            _preflight(target, organism_root, spec, state_root)
            marker = {
                "digest": fingerprint_digest(fingerprint),
                "fingerprint": fingerprint,
            }
            (target / MARKER_NAME).write_text(
                json.dumps(marker, indent=2, sort_keys=True) + "\n"
            )
        except BaseException:
            if target.exists() or target.is_symlink():
                _safe_remove_environment(target, cache_root)
            if backup.exists() or backup.is_symlink():
                backup.rename(target)
            raise
        if backup.exists() or backup.is_symlink():
            _safe_remove_environment(backup, cache_root)
        return target, True


def _wipe_output(workspace: Path) -> None:
    output = workspace / "output"
    if not output.is_dir() or output.is_symlink():
        return
    for child in output.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def _load_nonempty_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _run_legacy_workspace(
    python: Path,
    workspace: Path,
    organism_root: Path,
    env: Mapping[str, str],
) -> int:
    """Run an allowlist for the one legacy workspace without a runner."""
    hands_path = organism_root / "PyAutoHands" / "autohands"
    sys.path.insert(0, str(hands_path))
    try:
        from env_config import build_env_for_script, load_env_config
        from build_util import should_skip
        import yaml
    finally:
        sys.path.pop(0)

    profile_path = workspace / "config" / "build" / "profile_smoke.yaml"
    config = load_env_config(profile_path) if profile_path.is_file() else None
    no_run_path = workspace / "config" / "build" / "no_run.yaml"
    no_run = yaml.safe_load(no_run_path.read_text()) if no_run_path.is_file() else []
    if isinstance(no_run, dict):
        no_run = next(iter(no_run.values()), [])
    args = shlex.split((config or {}).get("args_default", ""))
    failures = 0
    scripts = _load_nonempty_lines(workspace / "smoke_tests.txt")
    for entry in scripts:
        script = workspace / entry
        if not script.is_file():
            script = workspace / "scripts" / entry
        if should_skip(script, no_run or []):
            print(f"[SKIP] {entry}")
            continue
        if not script.is_file():
            print(f"[MISSING] {entry}")
            failures += 1
            continue
        child_env = build_env_for_script(Path(entry), config) or dict(env)
        # build_env_for_script starts from os.environ; restore the isolation
        # variables that identify this prepared smoke environment.
        child_env.update(env)
        result = subprocess.run(
            [str(python), str(script), *args], cwd=workspace, env=child_env
        )
        label = "PASS" if result.returncode == 0 else f"FAIL {result.returncode}"
        print(f"[{label}] {entry}")
        failures += result.returncode != 0
    print(
        f"=== Smoke test summary: {len(scripts) - failures}/{len(scripts)} passed ==="
    )
    return 1 if failures else 0


def run_workspace(
    environment: Path,
    organism_root: Path,
    state_root: Path,
    spec: WorkspaceSpec,
) -> int:
    workspace = organism_root / spec.directory
    _wipe_output(workspace)
    env = runtime_environment(environment, organism_root, spec, state_root)
    python = _environment_python(environment)
    runner = workspace / ".github" / "scripts" / "run_smoke.py"
    if runner.is_file():
        return subprocess.run(
            [str(python), str(runner)], cwd=workspace, env=env
        ).returncode
    return _run_legacy_workspace(python, workspace, organism_root, env)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare isolated workspace smoke environments and run their suites."
    )
    parser.add_argument(
        "workspaces",
        nargs="*",
        choices=tuple(WORKSPACES),
        help="workspace keys (default: all)",
    )
    parser.add_argument(
        "--rebuild", action="store_true", help="rebuild selected environments"
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="prepare and preflight without running scripts",
    )
    parser.add_argument(
        "--python", default=sys.executable, help="base Python interpreter"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(
            os.environ.get("PYAUTO_ROOT", Path(__file__).resolve().parents[2])
        ),
        help="organism root",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("HEART_STATE_DIR", Path.home() / ".pyauto-heart")),
        help="Heart state/cache directory",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    selected = args.workspaces or list(WORKSPACES)
    failures = 0
    for key in selected:
        spec = WORKSPACES[key]
        workspace = root / spec.directory
        if not workspace.is_dir():
            print(f"[{key}] missing workspace: {workspace}", file=sys.stderr)
            failures += 1
            continue
        try:
            environment, built = prepare_environment(
                root,
                args.state_dir.resolve(),
                spec,
                python=args.python,
                rebuild=args.rebuild,
            )
            print(f"[{key}] {'built' if built else 'reused'} {environment}")
            if not args.prepare_only:
                failures += (
                    run_workspace(environment, root, args.state_dir.resolve(), spec)
                    != 0
                )
        except (SmokeEnvironmentError, subprocess.CalledProcessError, OSError) as exc:
            print(
                f"[{key}] environment FAILED before smoke execution: {exc}",
                file=sys.stderr,
            )
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
