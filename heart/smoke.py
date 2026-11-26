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
import tempfile
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml
from heart import _workspace

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
    #: The directory ``smoke_tests.txt`` entries are relative to — the
    #: ``directory`` argument the workspace's own ``run_smoke.py`` passes to
    #: PyAutoHands' ``run_python.py``. Only the targeted mode reads it (it runs
    #: a filtered list through that same entry point); a full run still goes
    #: through the workspace-owned runner.
    scripts_dir: str = "scripts"


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
            str(spec.get("scripts_dir", "scripts")),
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
    workspace = _workspace.repo_path(organism_root, spec.directory)
    watched = [workspace / ".github" / "scripts" / "smoke_install.sh"]
    watched.extend(_workspace.repo_path(organism_root, repo) / "pyproject.toml" for repo in spec.chain)
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
    source_paths = [str(_workspace.repo_path(organism_root, repo)) for repo in spec.chain]
    source_paths.append(str(_workspace.repo_path(organism_root, "PyAutoHands") / "autohands"))
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
    # A `--jobs N` parent hands its thread caps down explicitly and they are
    # applied last: the interpreter running this child may itself pin thread
    # variables at startup (a venv site hook), which must not undo the cap.
    caps = os.environ.get(THREAD_CAPS_VARIABLE)
    if caps:
        env.update(json.loads(caps))
    return env


def _optional_local_targets(organism_root: Path, chain: Iterable[str]) -> list[str]:
    targets: list[str] = []
    for repo in chain:
        pyproject = _workspace.repo_path(organism_root, repo) / "pyproject.toml"
        if not pyproject.is_file():
            continue
        with pyproject.open("rb") as stream:
            data = tomllib.load(stream)
        extras = data.get("project", {}).get("optional-dependencies", {})
        if "optional" in extras:
            targets.append(f"{pyproject.parent.resolve()}[optional]")
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

    workspace = _workspace.repo_path(organism_root, spec.directory)
    installer = workspace / ".github" / "scripts" / "smoke_install.sh"
    if installer.is_file():
        # The workspace-owned epilogue has the same flat contract as CI.
        # Resolve from the real root first, then expose only this dependency
        # chain. Never alter the root or leave symlinks behind after failure.
        resolved = {}
        for repo in spec.chain:
            if Path(repo).name != repo or repo in {".", ".."}:
                raise SmokeEnvironmentError(f"Invalid dependency checkout name: {repo}")
            path = _workspace.repo_path(organism_root, repo).resolve()
            if not path.is_dir():
                raise SmokeEnvironmentError(f"Missing dependency checkout {repo}: {path}")
            resolved[repo] = path
        with tempfile.TemporaryDirectory(prefix="pyauto-smoke-install-") as directory:
            staging = Path(directory)
            for repo, path in resolved.items():
                (staging / repo).symlink_to(path, target_is_directory=True)
            _run(["bash", installer.resolve()], cwd=staging, env=env)
        return

    # Legacy workspaces have no CI epilogue yet.  Install their local chain and
    # discover the conventional `optional` extras from package metadata; Heart
    # does not carry a second list of third-party packages.
    local_targets = [str(_workspace.repo_path(organism_root, repo).resolve()) for repo in spec.chain]
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
#: arcticpy <= 2.6 declares ``numpy~=1.21`` but is installed with ``--no-deps`` on
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
#:
#: arcticpy 2.7 (the current pin) allows numpy<3 and no longer triggers this;
#: the exception stays for the ARCTICPY_VERSION=2.6 PyPI override.
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
        IMPORT_NAMES[repo]: str((_workspace.repo_path(organism_root, repo)).resolve())
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

    notebook_list = _workspace.repo_path(organism_root, spec.directory) / "smoke_notebooks.txt"
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
    # Read the index before deleting anything. A failed git read must not turn
    # an unknown tracked set into permission to wipe user-controlled files.
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "output/"],
        cwd=workspace, capture_output=True, check=True,
    )
    tracked = {
        workspace / os.fsdecode(name)
        for name in result.stdout.split(b"\0") if name
    }
    protected = tracked | {parent for path in tracked for parent in path.parents}

    def remove_untracked(directory: Path) -> None:
        for child in directory.iterdir():
            if child in tracked:
                continue
            if child in protected:
                # A tracked descendant can have a symlink in its place in the
                # working tree. Preserve it; never follow it outside output/.
                if child.is_dir() and not child.is_symlink():
                    remove_untracked(child)
            elif child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)

    remove_untracked(output)


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
    hands_path = _workspace.repo_path(organism_root, "PyAutoHands") / "autohands"
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
    workspace = _workspace.repo_path(organism_root, spec.directory)
    _wipe_output(workspace)
    env = runtime_environment(environment, organism_root, spec, state_root)
    python = _environment_python(environment)
    runner = workspace / ".github" / "scripts" / "run_smoke.py"
    if runner.is_file():
        return subprocess.run(
            [str(python), str(runner)], cwd=workspace, env=env
        ).returncode
    return _run_legacy_workspace(python, workspace, organism_root, env)


# ---------------------------------------------------------------------------
# Selection: --affected-by and --targeted (PyAutoHeart#352)
#
# A shift worker runs a *targeted* smoke (only the curated scripts that touch
# the changed code); the shift close-out runs every *affected* workspace, in
# parallel, before an unattended merge. A human `pyauto-heart smoke` with no
# flags keeps the full serial sweep.
# ---------------------------------------------------------------------------


def affected_workspaces(
    repos: Iterable[str],
    workspaces: Mapping[str, WorkspaceSpec] | None = None,
) -> tuple[list[str], list[str]]:
    """Workspace keys whose install ``chain`` contains any of ``repos`` (or
    whose own ``directory`` is one of them), in config order.

    Returns ``(selected, unmatched)``: ``unmatched`` are the names that select
    nothing — an organ or a typo. They are reported, never an error, so a
    close-out can pass every changed repo without first filtering out the ones
    no smoke workspace installs.
    """
    workspaces = WORKSPACES if workspaces is None else workspaces
    wanted = [r for r in (r.strip() for r in repos) if r]
    selected = [
        key
        for key, spec in workspaces.items()
        if any(r in spec.chain or r == spec.directory for r in wanted)
    ]
    known = {r for spec in workspaces.values() for r in spec.chain}
    known |= {spec.directory for spec in workspaces.values()}
    return selected, [r for r in wanted if r not in known]


def own_workspace(
    repo: str, workspaces: Mapping[str, WorkspaceSpec] | None = None
) -> str | None:
    """The workspace that is ``repo``'s own: the workspace repo itself, else
    the first (config order) whose chain ENDS in ``repo``, else the first whose
    chain contains it."""
    workspaces = WORKSPACES if workspaces is None else workspaces
    for key, spec in workspaces.items():
        if spec.directory == repo:
            return key
    for key, spec in workspaces.items():
        if spec.chain and spec.chain[-1] == repo:
            return key
    for key, spec in workspaces.items():
        if repo in spec.chain:
            return key
    return None


@dataclass
class ChangeSummary:
    """What a set of changed files touches, for targeted script matching."""

    #: repos the files live in — chain libraries or workspace directories
    repos: list[str]
    #: dotted import paths and public names; a script mentioning any matches
    tokens: set[str]
    #: changed files that are themselves workspace scripts (resolved paths)
    scripts: set[Path]


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def _changed_ranges(
    repo_dir: Path, relative: str, base: str
) -> list[tuple[int, int]] | None:
    """New-side line ranges the branch changed in one file, against the merge
    base of ``base`` and HEAD (working-tree edits included). ``None`` when git
    cannot say — the caller then treats the whole file as touched."""
    try:
        merge_base = subprocess.run(
            ["git", "merge-base", base, "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, check=True,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "-U0", merge_base, "--", relative],
            cwd=repo_dir, capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    ranges = []
    for match in _HUNK.finditer(diff):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        # A pure deletion (count 0) sits between two lines: mark the line it
        # follows, so the enclosing definition still counts as touched.
        ranges.append((max(start, 1), max(start + count - 1, start, 1)))
    return ranges


def _public_names(source: str, ranges: list[tuple[int, int]] | None) -> set[str]:
    """Public module-level names (``def``/``class``/``NAME =``) whose span
    meets a changed range; every public one when ``ranges`` is None. A change
    inside a method counts for its class: method names alone are too generic
    (``fit``, ``image``) to select scripts by."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found = [t.id for t in targets if isinstance(t, ast.Name)]
        else:
            continue
        start = min(
            [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
        )
        end = node.end_lineno or node.lineno
        if ranges is not None and not any(a <= end and b >= start for a, b in ranges):
            continue
        names.update(n for n in found if not n.startswith("_"))
    return names


def summarise_changes(
    organism_root: Path,
    changed_files: Iterable[str | os.PathLike[str]],
    *,
    base: str = "origin/main",
    names: Iterable[str] = (),
    workspaces: Mapping[str, WorkspaceSpec] | None = None,
    import_names: Mapping[str, str] | None = None,
) -> ChangeSummary:
    """Map changed files to the repos they live in and the tokens a script
    must mention to be affected: each changed library module's dotted import
    path (``pkg.sub.module``) and the public names the diff touches in it,
    plus any ``names`` given explicitly."""
    workspaces = WORKSPACES if workspaces is None else workspaces
    import_names = IMPORT_NAMES if import_names is None else import_names
    candidates = {r for spec in workspaces.values() for r in spec.chain}
    candidates |= {spec.directory for spec in workspaces.values()}
    homes = {}
    for repo in sorted(candidates):
        try:
            homes[repo] = _workspace.repo_path(organism_root, repo).resolve()
        except (RuntimeError, FileNotFoundError):
            continue
    repos: list[str] = []
    tokens = {n.strip() for n in names if n.strip()}
    scripts: set[Path] = set()
    for raw in changed_files:
        path = Path(raw)
        if not path.is_absolute() and not path.exists():
            path = organism_root / path
        path = path.resolve()
        owner = next(
            (repo for repo, home in homes.items() if path.is_relative_to(home)), None
        )
        if owner is None:
            continue
        if owner not in repos:
            repos.append(owner)
        if path.suffix != ".py":
            continue
        relative = path.relative_to(homes[owner])
        package = import_names.get(owner)
        if package is None:
            scripts.add(path)  # a workspace file: it may itself be a curated script
            continue
        if not relative.parts or relative.parts[0] != package:
            continue  # tests/, docs, tooling — not importable library code
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if len(parts) > 1:  # the bare package name would match every script
            tokens.add(".".join(parts))
        if path.is_file():
            ranges = _changed_ranges(homes[owner], relative.as_posix(), base)
            tokens |= _public_names(path.read_text(errors="replace"), ranges)
    return ChangeSummary(repos, tokens, scripts)


def curated_scripts(workspace: Path) -> list[str]:
    """The entries of a workspace's ``smoke_tests.txt``, in list order."""
    return _load_nonempty_lines(workspace / "smoke_tests.txt")


def _mentions(source: str, tokens: Iterable[str]) -> bool:
    for token in tokens:
        if re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", source):
            return True
    return False


def cheapest_script(workspace: Path, spec: WorkspaceSpec) -> str | None:
    """The curated entry with the smallest recorded passing time in the
    workspace's ``test-results/smoke_timings.json``; the list's first entry
    when there is no timing record."""
    entries = curated_scripts(workspace)
    if not entries:
        return None
    timings = {}
    try:
        record = json.loads((workspace / "test-results" / "smoke_timings.json").read_text())
        for item in record.get("entries", []):
            if item.get("kind", "script") == "script" and item.get("status") == "passed":
                timings[str(item["entry"])] = float(item["seconds"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    prefix = "" if spec.scripts_dir in ("", ".") else spec.scripts_dir.rstrip("/") + "/"
    timed = [
        (timings[key], index, entry)
        for index, entry in enumerate(entries)
        for key in (prefix + entry, entry)
        if key in timings
    ]
    return min(timed)[2] if timed else entries[0]


def targeted_selection(
    organism_root: Path,
    changes: ChangeSummary,
    keys: Iterable[str],
    workspaces: Mapping[str, WorkspaceSpec] | None = None,
) -> tuple[dict[str, list[str]], str]:
    """Per workspace, the curated entries that reference the changed code.

    An entry matches when the script IS a changed file, or its source mentions
    a change token (a dotted module path or a touched public name, as a whole
    word). List order is kept. When nothing matches anywhere, the single
    cheapest curated script of the first changed repo's own workspace runs as
    an import check. Returns ``(selection, reason)``; workspaces with no
    ``smoke_tests.txt`` are not selectable."""
    workspaces = WORKSPACES if workspaces is None else workspaces
    selection: dict[str, list[str]] = {}
    for key in keys:
        spec = workspaces[key]
        workspace = _workspace.repo_path(organism_root, spec.directory)
        script_root = workspace / spec.scripts_dir
        matched = []
        for entry in curated_scripts(workspace):
            script = (script_root / entry).resolve()
            if script in changes.scripts:
                matched.append(entry)
                continue
            try:
                source = script.read_text(errors="replace")
            except OSError:
                continue
            if _mentions(source, changes.tokens):
                matched.append(entry)
        if matched:
            selection[key] = matched
    if selection:
        return selection, "scripts referencing the changed code"
    for repo in changes.repos:
        key = own_workspace(repo, workspaces)
        if key is None:
            continue
        spec = workspaces[key]
        entry = cheapest_script(_workspace.repo_path(organism_root, spec.directory), spec)
        if entry:
            return {key: [entry]}, f"no script references the change; import check in {key}"
    return {}, "no curated script references the change and no own workspace to import-check"


def run_script_list(
    environment: Path,
    organism_root: Path,
    state_root: Path,
    spec: WorkspaceSpec,
    list_file: Path,
) -> int:
    """Run an explicit script list through the same PyAutoHands entry point the
    workspace runners use, reporting to ``test-results/targeted/`` so the full
    run's result files and timings are never overwritten. ``output/`` is not
    wiped: a targeted script may read an earlier entry's committed or
    previously generated output."""
    workspace = _workspace.repo_path(organism_root, spec.directory)
    env = runtime_environment(environment, organism_root, spec, state_root)
    python = _environment_python(environment)
    runner = _workspace.repo_path(organism_root, "PyAutoHands") / "autohands" / "run_python.py"
    command = [
        str(python), str(runner), spec.directory, spec.scripts_dir,
        "--list", str(list_file),
        "--report-dir", str(workspace / "test-results" / "targeted"),
    ]
    return subprocess.run(command, cwd=workspace, env=env).returncode


# ---------------------------------------------------------------------------
# Parallel workspaces: --jobs N
# ---------------------------------------------------------------------------

#: Thread-pool variables capped in each concurrent workspace's process so N
#: workspaces never oversubscribe the machine. XLA is capped via XLA_FLAGS.
THREAD_CAP_VARIABLES = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)


#: Carries ``thread_caps`` from a ``--jobs N`` parent to each workspace child.
THREAD_CAPS_VARIABLE = "HEART_SMOKE_THREAD_CAPS"


def thread_caps(
    jobs: int, cpu_count: int | None = None, xla_flags: str = ""
) -> dict[str, str]:
    """Per-process thread limits so ``jobs`` × threads ≤ the machine's cores."""
    cores = cpu_count or os.cpu_count() or 1
    threads = max(1, cores // max(1, jobs))
    caps = {name: str(threads) for name in THREAD_CAP_VARIABLES}
    xla = [f for f in xla_flags.split() if not f.startswith(
        ("--xla_cpu_multi_thread_eigen", "intra_op_parallelism_threads")
    )]
    xla.append(f"--xla_cpu_multi_thread_eigen={'true' if threads > 1 else 'false'}")
    xla.append(f"intra_op_parallelism_threads={threads}")
    caps["XLA_FLAGS"] = " ".join(xla)
    return caps


def _child_command(args: argparse.Namespace, key: str, list_file: Path | None) -> list[str]:
    command = [
        sys.executable, "-m", "heart.smoke", key, "--jobs", "1",
        "--python", str(args.python),
        "--root", str(args.root.resolve()),
        "--state-dir", str(args.state_dir.resolve()),
    ]
    if args.rebuild:
        command.append("--rebuild")
    if args.prepare_only:
        command.append("--prepare-only")
    if list_file is not None:
        command += ["--script-list", str(list_file)]
    return command


def _log_tail(path: Path, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def run_parallel(
    args: argparse.Namespace,
    selected: Sequence[str],
    jobs: int,
    list_files: Mapping[str, Path] | None = None,
) -> int:
    """Run each selected workspace as its own ``heart.smoke`` process, ``jobs``
    at a time, thread-capped, its output in its own log file. Each child
    prepares (or reuses) its own cached environment and writes its own
    ``test-results/``; the run fails when any workspace fails."""
    from concurrent.futures import ThreadPoolExecutor
    import time

    state_root = args.state_dir.resolve()
    env = os.environ.copy()
    caps = thread_caps(jobs, xla_flags=env.get("XLA_FLAGS", ""))
    env.update(caps)
    env[THREAD_CAPS_VARIABLE] = json.dumps(caps)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(HEART_HOME), env.get("PYTHONPATH", "")) if p
    )
    list_files = list_files or {}
    print(
        f"running {len(selected)} workspace(s), {jobs} at a time, "
        f"{env['OMP_NUM_THREADS']} thread(s) each"
    )

    def one(key: str) -> tuple[str, int, float, Path]:
        log = state_root / "smoke-runtime" / key / "smoke.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with log.open("w") as stream:
            code = subprocess.run(
                _child_command(args, key, list_files.get(key)),
                cwd=HEART_HOME, env=env, stdout=stream, stderr=subprocess.STDOUT,
            ).returncode
        result = (key, code, time.monotonic() - started, log)
        print(f"[{key}] {'PASS' if code == 0 else f'FAIL ({code})'} "
              f"in {result[2]:.0f}s — log {log}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(one, selected))

    failed = [r for r in results if r[1] != 0]
    print("\n=== Smoke summary (parallel) ===")
    for key, code, seconds, log in results:
        print(f"  {key:<16} {'PASS' if code == 0 else 'FAIL':<5} {seconds:7.0f}s  {log}")
    for key, _, _, log in failed:
        print(f"\n--- {key}: last lines of {log} ---\n{_log_tail(log)}")
    print(f"=== {len(results) - len(failed)}/{len(results)} workspaces passed ===")
    return 1 if failed else 0


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
    parser.add_argument(
        "--affected-by",
        action="append",
        default=[],
        metavar="REPO[,REPO...]",
        help="only workspaces whose install chain contains (or which are) one of these repos",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="workspaces to run at once (default 1 = serial); each runs in its own "
        "process with thread pools capped so jobs x threads <= cores",
    )
    parser.add_argument(
        "--targeted",
        action="store_true",
        help="run only the curated scripts that reference the changed code "
        "(needs --changed-files); none match -> the cheapest script of the "
        "changed repo's own workspace, as an import check",
    )
    parser.add_argument(
        "--changed-files",
        nargs="+",
        default=[],
        metavar="PATH",
        help="files the change touches (absolute, or relative to cwd / --root)",
    )
    parser.add_argument(
        "--names",
        action="append",
        default=[],
        metavar="NAME[,NAME...]",
        help="extra public names a matching script may mention (--targeted)",
    )
    parser.add_argument(
        "--base",
        default="origin/main",
        help="ref the change is diffed against to find touched names (--targeted)",
    )
    parser.add_argument("--script-list", type=Path, help=argparse.SUPPRESS)
    return parser


def _repo_list(values: Sequence[str]) -> list[str]:
    return [r for value in values for r in value.split(",") if r.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    root = args.root.resolve()
    state_root = args.state_dir.resolve()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.targeted and not args.changed_files:
        parser.error("--targeted needs --changed-files")
    if args.script_list and len(args.workspaces) != 1:
        parser.error("--script-list runs exactly one workspace")

    selected = list(args.workspaces) or list(WORKSPACES)
    if args.affected_by:
        affected, unmatched = affected_workspaces(_repo_list(args.affected_by))
        for repo in unmatched:
            print(f"--affected-by {repo}: no smoke workspace installs it")
        selected = [key for key in selected if key in affected]

    list_files: dict[str, Path] = {}
    if args.targeted:
        changes = summarise_changes(
            root, args.changed_files, base=args.base, names=_repo_list(args.names)
        )
        affected, _ = affected_workspaces(changes.repos)
        selected = [key for key in selected if key in affected]
        selection, reason = targeted_selection(root, changes, selected)
        print(f"targeted smoke: {reason}")
        for key in selected:
            if key not in selection:
                print(f"[{key}] no curated script references the change; skipped")
        for key, entries in selection.items():
            list_file = state_root / "smoke-runtime" / key / "targeted_smoke_tests.txt"
            list_file.parent.mkdir(parents=True, exist_ok=True)
            list_file.write_text("\n".join(entries) + "\n")
            list_files[key] = list_file
            print(f"[{key}] {len(entries)} script(s): " + ", ".join(entries))
        selected = [key for key in selected if key in selection]
    elif args.script_list:
        list_files[selected[0]] = args.script_list.resolve()

    if not selected:
        print("no smoke workspace selected; nothing to run")
        return 0

    jobs = min(args.jobs, len(selected))
    if jobs > 1:
        return run_parallel(args, selected, jobs, list_files)

    failures = 0
    for key in selected:
        spec = WORKSPACES[key]
        workspace = _workspace.repo_path(root, spec.directory)
        if not workspace.is_dir():
            print(f"[{key}] missing workspace: {workspace}", file=sys.stderr)
            failures += 1
            continue
        try:
            environment, built = prepare_environment(
                root,
                state_root,
                spec,
                python=args.python,
                rebuild=args.rebuild,
            )
            print(f"[{key}] {'built' if built else 'reused'} {environment}")
            if args.prepare_only:
                continue
            if key in list_files:
                code = run_script_list(environment, root, state_root, spec, list_files[key])
            else:
                code = run_workspace(environment, root, state_root, spec)
            failures += code != 0
        except (SmokeEnvironmentError, subprocess.CalledProcessError, OSError) as exc:
            print(
                f"[{key}] environment FAILED before smoke execution: {exc}",
                file=sys.stderr,
            )
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
