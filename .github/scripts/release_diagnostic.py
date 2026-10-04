"""Bounded wheel-only investigation; never emits release-validation evidence."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time


LIBRARIES = ("autonerves", "autofit", "autoarray", "autogalaxy", "autolens")


def load_manifest(path):
    data = json.loads(Path(path).read_text())
    if data.get("schema_version") != 1:
        raise ValueError("unsupported diagnostic manifest")
    for key in ("workspace_sha", "hands_sha"):
        if not re.fullmatch(r"[0-9a-f]{40}", data.get(key, "")):
            raise ValueError(f"{key} must be an immutable commit")
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", data.get("workspace_repo", "")):
        raise ValueError("invalid workspace repository")
    if not re.fullmatch(r"3\.\d+\.\d+", data.get("python", "")):
        raise ValueError("pin the Python patch version")
    script = Path(data.get("script", ""))
    if script.is_absolute() or ".." in script.parts or script.suffix != ".py":
        raise ValueError("script must be a relative Python path inside scripts/")
    packages = data.get("packages", {})
    if not all(name in packages for name in (*LIBRARIES, "jax", "jaxlib")):
        raise ValueError("missing library/runtime package pins")
    for name, version in packages.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(
            r"[0-9][A-Za-z0-9_.+!-]*", version
        ):
            raise ValueError("package pins must be names and exact versions")
    return data


def prepare(manifest, output, github_output=None):
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "requirements.txt").write_text(
        "".join(
            f"{name}=={version}\n"
            for name, version in sorted(manifest["packages"].items())
        )
    )
    if github_output:
        with Path(github_output).open("a") as stream:
            for key in ("python", "workspace_repo", "workspace_sha", "hands_sha"):
                print(f"{key}={manifest[key]}", file=stream)


def provenance(manifest, workspace, hands):
    errors = []
    receipt = {
        "diagnostic_only": True,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "packages": {},
        "imports": {},
    }
    receipt["runner_image"] = {
        key: os.environ.get(key) for key in ("ImageOS", "ImageVersion")
    }
    receipt["cpu_quota"] = {}
    for path in ("/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpuset.cpus.effective"):
        try:
            receipt["cpu_quota"][path] = Path(path).read_text().strip()
        except OSError:
            receipt["cpu_quota"][path] = None
    for label, root, expected in (
        ("workspace", workspace, manifest["workspace_sha"]),
        ("hands", hands, manifest["hands_sha"]),
    ):
        actual = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        receipt[label + "_sha"] = actual
        if actual != expected:
            errors.append(f"{label} commit differs from manifest")
    if receipt["python"] != manifest["python"]:
        errors.append("Python patch version differs from manifest")
    for name, expected in manifest["packages"].items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        receipt["packages"][name] = actual
        if actual != expected:
            errors.append(f"{name}: expected {expected}, installed {actual}")
    for name in (*LIBRARIES, "jax", "jaxlib"):
        spec = importlib.util.find_spec(name)
        origin = spec.origin if spec else None
        receipt["imports"][name] = origin
        if not origin or "site-packages" not in Path(origin).parts:
            errors.append(f"{name} is not imported from installed wheels")
    receipt["errors"] = errors
    return receipt


def native_dump(pid, output):
    """Collect while the child lives; each helper has its own short deadline."""
    blocks = []
    for task in sorted(Path(f"/proc/{pid}/task").glob("*")):
        try:
            blocks.append(
                f"{task.name}: {(task / 'comm').read_text().strip()} "
                f"{(task / 'wchan').read_text().strip()}"
            )
        except OSError as error:
            blocks.append(str(error))
    commands = [
        ["py-spy", "dump", "--native", "--pid", str(pid)],
        [
            "gdb",
            "--batch",
            "-p",
            str(pid),
            "-ex",
            "set pagination off",
            "-ex",
            "thread apply all bt",
            "-ex",
            "detach",
        ],
    ]
    for command in commands:
        blocks.append("$ " + " ".join(command))
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            blocks.extend([f"exit={result.returncode}", result.stdout, result.stderr])
        except (OSError, subprocess.TimeoutExpired) as error:
            blocks.append(str(error))
            if isinstance(error, subprocess.TimeoutExpired):
                for partial in (error.stdout, error.stderr):
                    if partial:
                        blocks.append(
                            partial.decode(errors="replace")
                            if isinstance(partial, bytes)
                            else partial
                        )
    output.write_text("\n".join(blocks))


def trial(command, cwd, env, output, *, cap=300, dump_after=120, dump=native_dump):
    started = time.monotonic()
    timed_out = False
    with output.open("w") as log:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            try:
                proc.wait(timeout=dump_after)
            except subprocess.TimeoutExpired:
                dump(proc.pid, output.with_suffix(".native.txt"))
                try:
                    proc.wait(timeout=max(0, cap - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    timed_out = True
        finally:
            # Also retire any simulator descendants; none may survive this trial.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    return {
        "status": (
            "timeout" if timed_out else "pass" if proc.returncode == 0 else "fail"
        ),
        "returncode": proc.returncode,
        "elapsed_s": round(time.monotonic() - started, 3),
        "cap_s": cap,
        "log": output.name,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "run"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--hands", type=Path)
    parser.add_argument("--repeats", type=int, default=6)
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 6:
        parser.error("repeats must be between 1 and 6")
    manifest = load_manifest(args.manifest)
    output = args.output.resolve()
    prepare(manifest, output, args.github_output)
    if args.mode == "prepare":
        return 0
    if not args.workspace or not args.hands:
        parser.error("run needs --workspace and --hands")
    workspace, hands = args.workspace.resolve(), args.hands.resolve()
    receipt = provenance(manifest, workspace, hands)
    (output / "provenance.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if receipt["errors"]:
        print("Provenance mismatch: " + "; ".join(receipt["errors"]), flush=True)
        return 2
    # Import only the pinned Hands resolver into this parent. Child imports
    # never inherit source directories from sys.path.
    sys.path.insert(0, str(hands / "autohands"))
    from env_config import build_env_for_script, load_env_config

    script = workspace / "scripts" / manifest["script"]
    if not script.is_file() or not script.resolve().is_relative_to(
        workspace / "scripts"
    ):
        raise ValueError("script missing or outside workspace")
    config = load_env_config(workspace / "config/build/profile_release.yaml")
    env = build_env_for_script(script, config)
    env.pop("PYTHONPATH", None)
    env.update(PYTHONUNBUFFERED="1", JAX_TRACEBACK_FILTERING="off")
    receipt["environment"] = {
        key: value
        for key, value in env.items()
        if key.startswith(("PYAUTO_", "JAX_", "XLA_", "OMP_", "MKL_", "OPENBLAS_"))
    }
    (output / "provenance.json").write_text(json.dumps(receipt, indent=2) + "\n")
    results = {
        "diagnostic_only": True,
        "source_run": manifest["source_run"],
        "trials": [],
    }
    for index in range(args.repeats):
        result = trial(
            [sys.executable, str(script)],
            workspace,
            env,
            output / f"trial-{index + 1}.log",
        )
        results["trials"].append(result)
        (output / "diagnostic-results.json").write_text(
            json.dumps(results, indent=2) + "\n"
        )
        print(json.dumps(result), flush=True)
        if result["status"] != "pass":
            return 1
    print(
        "Diagnostic repetitions passed; this is not release-validation clearance.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
