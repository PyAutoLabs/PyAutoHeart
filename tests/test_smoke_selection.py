"""Workspace selection, parallel runs and the targeted mode of heart/smoke.py
(PyAutoHeart#352).

A shift worker runs a targeted smoke (only the curated scripts that touch the
change); the close-out runs every affected workspace, ``--jobs N`` at a time,
before an unattended merge. Fixture repo names are synthetic (LibraryA,
LibraryB) for the tenant-firewall reason given in test_smoke.py; the one test
against the real policy file reads library names out of the config rather
than spelling them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from heart import smoke


# --------------------------------------------------------------------------
# --affected-by: chain-based workspace selection
# --------------------------------------------------------------------------


def test_affected_by_the_real_chain_table():
    workspaces = smoke.WORKSPACES
    # The galaxy library is the last link of the autogalaxy chain; the fit
    # library the last link of the autofit chain. Read, not spelled.
    galaxy = workspaces["autogalaxy"].chain[-1]
    fit = workspaces["autofit"].chain[-1]

    selected, unmatched = smoke.affected_workspaces([galaxy])
    assert unmatched == []
    for skipped in ("autofit", "autocti", "autocti_test"):
        assert skipped not in selected
    assert {"autogalaxy", "autolens", "autolens_test"} <= set(selected)

    selected, _ = smoke.affected_workspaces([fit])
    assert selected == list(workspaces)


def test_affected_by_reports_names_that_select_nothing():
    workspaces = {
        "a": smoke.WorkspaceSpec("a", "a_workspace", ("LibraryA",)),
        "b": smoke.WorkspaceSpec("b", "b_workspace", ("LibraryA", "LibraryB")),
    }
    selected, unmatched = smoke.affected_workspaces(
        ["LibraryB", "SomeOrgan", "a_workspace"], workspaces
    )
    # a_workspace is selected by its own directory name, b by its chain.
    assert selected == ["a", "b"]
    assert unmatched == ["SomeOrgan"]


def test_affected_by_cli_selects_only_affected_workspaces(tmp_path, monkeypatch):
    workspaces = {
        "a": smoke.WorkspaceSpec("a", "a_workspace", ("LibraryA",)),
        "b": smoke.WorkspaceSpec("b", "b_workspace", ("LibraryA", "LibraryB")),
    }
    for spec in workspaces.values():
        (tmp_path / spec.directory).mkdir()
    monkeypatch.setattr(smoke, "WORKSPACES", workspaces)
    prepared = []
    monkeypatch.setattr(
        smoke,
        "prepare_environment",
        lambda root, state, spec, **_: prepared.append(spec.key) or (tmp_path, False),
    )
    code = smoke.main(
        ["--affected-by", "LibraryB", "--prepare-only",
         "--root", str(tmp_path), "--state-dir", str(tmp_path / "state")]
    )
    assert code == 0
    assert prepared == ["b"]


# --------------------------------------------------------------------------
# --jobs N: concurrent workspaces, same verdict and result files as serial
# --------------------------------------------------------------------------

RUN_SMOKE = textwrap.dedent(
    """
    import json, os, pathlib, sys
    ws = pathlib.Path(__file__).resolve().parents[2]
    out = ws / "test-results"
    out.mkdir(exist_ok=True)
    fail = (ws / "FAIL").exists()
    caps = {k: os.environ.get(k) for k in
            ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
             "NUMBA_NUM_THREADS", "XLA_FLAGS")}
    (out / "result.json").write_text(json.dumps({"workspace": ws.name, "failed": fail}))
    (out / "env.json").write_text(json.dumps(caps))
    print("ran", ws.name)
    sys.exit(1 if fail else 0)
    """
)

# The parallel path starts one `heart.smoke` process per workspace, so pytest's
# monkeypatching cannot reach it. This child re-creates the same fake world —
# the workspace table and a no-op environment build — then runs the real main.
FAKE_CHILD = textwrap.dedent(
    """
    import json, os, pathlib, sys
    from heart import smoke
    table = json.loads(os.environ["FAKE_SMOKE_TABLE"])
    smoke.WORKSPACES = {k: smoke.WorkspaceSpec(k, v["directory"], tuple(v["chain"]))
                        for k, v in table.items()}
    environment = pathlib.Path(os.environ["FAKE_SMOKE_ENV"])
    smoke.prepare_environment = lambda *a, **k: (environment, False)
    sys.exit(smoke.main(sys.argv[1:]))
    """
)


def make_world(tmp_path: Path, failing: set[str] = frozenset()):
    root = tmp_path / "organism"
    workspaces = {}
    for key, chain in {
        "one": ("LibraryA",),
        "two": ("LibraryA", "LibraryB"),
        "three": ("LibraryA", "LibraryB"),
    }.items():
        spec = smoke.WorkspaceSpec(key, f"{key}_workspace", chain)
        workspaces[key] = spec
        scripts = root / spec.directory / ".github" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "run_smoke.py").write_text(RUN_SMOKE)
        if key in failing:
            (root / spec.directory / "FAIL").write_text("")
    for repo in ("LibraryA", "LibraryB"):
        (root / repo).mkdir()
    (root / "PyAutoHands" / "autohands").mkdir(parents=True)
    environment = tmp_path / "environment"
    python = smoke._environment_python(environment)
    python.parent.mkdir(parents=True)
    # The base interpreter, not a venv's: a venv may pin thread variables at
    # startup (a site .pth), which would mask the caps under test.
    python.symlink_to(getattr(sys, "_base_executable", sys.executable))
    return root, workspaces, environment


def results(root: Path, workspaces) -> dict[str, dict]:
    return {
        key: json.loads(
            (root / spec.directory / "test-results" / "result.json").read_text()
        )
        for key, spec in workspaces.items()
    }


def run_both(tmp_path, monkeypatch, failing=frozenset()):
    """Serial in-process, then --jobs 3 through real child processes."""
    root, workspaces, environment = make_world(tmp_path, set(failing))
    monkeypatch.setattr(smoke, "WORKSPACES", workspaces)
    monkeypatch.setattr(
        smoke, "prepare_environment", lambda *a, **k: (environment, False)
    )
    common = ["--root", str(root), "--state-dir", str(tmp_path / "state")]

    serial_code = smoke.main(common)
    serial = results(root, workspaces)
    for spec in workspaces.values():
        (root / spec.directory / "test-results" / "result.json").unlink()

    child = tmp_path / "fake_child.py"
    child.write_text(FAKE_CHILD)
    monkeypatch.setenv(
        "FAKE_SMOKE_TABLE",
        json.dumps({k: {"directory": s.directory, "chain": list(s.chain)}
                    for k, s in workspaces.items()}),
    )
    monkeypatch.setenv("FAKE_SMOKE_ENV", str(environment))
    real = smoke._child_command
    monkeypatch.setattr(
        smoke,
        "_child_command",
        lambda *a: [sys.executable, str(child), *real(*a)[3:]],
    )
    parallel_code = smoke.main([*common, "--jobs", "3"])
    parallel = results(root, workspaces)
    return root, workspaces, serial_code, serial, parallel_code, parallel


def test_jobs_gives_the_same_verdict_and_result_files_as_serial(
    tmp_path, monkeypatch, capsys
):
    root, workspaces, serial_code, serial, parallel_code, parallel = run_both(
        tmp_path, monkeypatch
    )
    assert serial_code == parallel_code == 0
    assert serial == parallel
    assert set(parallel) == {"one", "two", "three"}
    out = capsys.readouterr().out
    assert "3/3 workspaces passed" in out
    # Each workspace's output went to its own log, not interleaved stdout.
    for key in workspaces:
        log = tmp_path / "state" / "smoke-runtime" / key / "smoke.log"
        assert f"ran {key}_workspace" in log.read_text()


def test_one_failing_workspace_fails_the_parallel_run(tmp_path, monkeypatch, capsys):
    _, _, serial_code, serial, parallel_code, parallel = run_both(
        tmp_path, monkeypatch, failing={"two"}
    )
    assert serial_code == parallel_code == 1
    assert serial == parallel
    assert [k for k, r in parallel.items() if r["failed"]] == ["two"]
    out = capsys.readouterr().out
    assert "2/3 workspaces passed" in out
    assert "--- two: last lines of" in out


def test_parallel_children_get_thread_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke.os, "cpu_count", lambda: 12)
    monkeypatch.setenv("XLA_FLAGS", "--xla_dump_to=/x intra_op_parallelism_threads=64")
    root, workspaces, *_ = run_both(tmp_path, monkeypatch)
    for spec in workspaces.values():
        caps = json.loads(
            (root / spec.directory / "test-results" / "env.json").read_text()
        )
        for name in smoke.THREAD_CAP_VARIABLES:
            assert caps[name] == "4", name
        flags = caps["XLA_FLAGS"].split()
        assert "intra_op_parallelism_threads=4" in flags
        assert "intra_op_parallelism_threads=64" not in flags
        assert "--xla_dump_to=/x" in flags


def test_thread_caps_keep_jobs_times_threads_within_cores():
    for jobs, cores in [(1, 8), (3, 8), (3, 12), (4, 2), (16, 16)]:
        caps = smoke.thread_caps(jobs, cpu_count=cores)
        threads = int(caps["OMP_NUM_THREADS"])
        assert threads >= 1
        assert jobs * threads <= max(cores, jobs)
    assert "--xla_cpu_multi_thread_eigen=false" in smoke.thread_caps(8, 8)["XLA_FLAGS"]


# --------------------------------------------------------------------------
# --targeted: curated scripts that reference the changed code
# --------------------------------------------------------------------------

LIBRARY_MODULE = textwrap.dedent(
    """
    def alpha_profile():
        return 1


    def beta_profile():
        return 2


    class GammaModel:
        def fit(self):
            return 3
    """
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def targeted_world(tmp_path: Path):
    root = tmp_path / "organism"
    library = root / "LibraryA"
    module = library / "librarya" / "sub" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text(LIBRARY_MODULE)
    git(library, "init", "-b", "main")
    git(library, "add", ".")
    git(library, "commit", "-m", "base")
    git(library, "checkout", "-b", "feature")
    # Touch alpha_profile only.
    module.write_text(LIBRARY_MODULE.replace("return 1", "return 10"))
    git(library, "commit", "-am", "change alpha")

    workspaces = {
        "own": smoke.WorkspaceSpec("own", "own_workspace", ("LibraryA",)),
        "other": smoke.WorkspaceSpec("other", "other_workspace", ("LibraryB",)),
    }
    scripts = {
        "uses_alpha.py": "import librarya as la\nla.sub.mod.alpha_profile()\n",
        "uses_beta.py": "import librarya as la\nla.sub.mod.beta_profile()\n",
        "imports_module.py": "from librarya.sub.mod import beta_profile\n",
        "unrelated.py": "import librarya\nprint('alpha_profiles')\n",
        "skipped.py": "alpha_profile()\n",
    }
    for spec in workspaces.values():
        folder = root / spec.directory / "scripts"
        folder.mkdir(parents=True)
        for name, body in scripts.items():
            (folder / name).write_text(body)
        (root / spec.directory / "smoke_tests.txt").write_text(
            "# curated\nunrelated.py\nuses_beta.py\nuses_alpha.py\n"
            "imports_module.py\n# skipped.py\n"
        )
    (root / "LibraryB").mkdir()
    return root, workspaces, module


def test_targeted_selects_scripts_by_import_path_and_touched_names(tmp_path):
    root, workspaces, module = targeted_world(tmp_path)
    changes = smoke.summarise_changes(
        root, [module], base="main", workspaces=workspaces,
        import_names={"LibraryA": "librarya"},
    )
    assert changes.repos == ["LibraryA"]
    # alpha_profile was touched; beta_profile and GammaModel were not.
    assert changes.tokens == {"librarya.sub.mod", "alpha_profile"}

    keys, _ = smoke.affected_workspaces(changes.repos, workspaces)
    assert keys == ["own"]
    selection, reason = smoke.targeted_selection(root, changes, keys, workspaces)
    # List order kept; commented-out entries never run; `alpha_profiles` is a
    # different word, so `unrelated.py` does not match.
    assert selection == {"own": ["uses_alpha.py", "imports_module.py"]}
    assert "referencing" in reason


def test_targeted_falls_back_to_the_cheapest_own_script(tmp_path):
    root, workspaces, _ = targeted_world(tmp_path)
    timings = root / "own_workspace" / "test-results" / "smoke_timings.json"
    timings.parent.mkdir()
    timings.write_text(json.dumps({"entries": [
        {"entry": "scripts/unrelated.py", "kind": "script", "status": "passed", "seconds": 9.0},
        {"entry": "scripts/uses_beta.py", "kind": "script", "status": "passed", "seconds": 2.5},
        {"entry": "scripts/uses_alpha.py", "kind": "script", "status": "failed", "seconds": 0.1},
    ]}))
    changes = smoke.ChangeSummary(["LibraryA"], {"nothing_mentions_this"}, set())
    selection, reason = smoke.targeted_selection(root, changes, ["own"], workspaces)
    assert selection == {"own": ["uses_beta.py"]}
    assert "import check" in reason


def test_a_changed_workspace_script_selects_itself(tmp_path):
    root, workspaces, _ = targeted_world(tmp_path)
    changes = smoke.summarise_changes(
        root, [root / "other_workspace" / "scripts" / "unrelated.py"],
        workspaces=workspaces, import_names={"LibraryA": "librarya"},
    )
    assert changes.repos == ["other_workspace"]
    keys, _ = smoke.affected_workspaces(changes.repos, workspaces)
    selection, _ = smoke.targeted_selection(root, changes, keys, workspaces)
    assert selection == {"other": ["unrelated.py"]}


def test_targeted_cli_runs_the_filtered_list_through_hands(tmp_path, monkeypatch):
    root, workspaces, module = targeted_world(tmp_path)
    monkeypatch.setattr(smoke, "WORKSPACES", workspaces)
    monkeypatch.setattr(smoke, "IMPORT_NAMES", {"LibraryA": "librarya"})
    environment = tmp_path / "environment"
    monkeypatch.setattr(
        smoke, "prepare_environment", lambda *a, **k: (environment, False)
    )
    calls = []
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(smoke, "_changed_ranges", lambda *a: [(2, 3)])
    state = tmp_path / "state"
    code = smoke.main(
        ["--targeted", "--changed-files", str(module),
         "--root", str(root), "--state-dir", str(state)]
    )
    assert code == 0
    (command, kwargs), = calls
    list_file = state / "smoke-runtime" / "own" / "targeted_smoke_tests.txt"
    assert list_file.read_text().split() == ["uses_alpha.py", "imports_module.py"]
    assert command[1].endswith(os.path.join("PyAutoHands", "autohands", "run_python.py"))
    assert command[2:] == [
        "own_workspace", "scripts", "--list", str(list_file),
        "--report-dir", str(root / "own_workspace" / "test-results" / "targeted"),
    ]
    assert kwargs["cwd"] == root / "own_workspace"


def test_targeted_needs_changed_files():
    with pytest.raises(SystemExit):
        smoke.main(["--targeted"])


def test_scripts_dir_is_read_from_config(tmp_path):
    config = tmp_path / "repos.yaml"
    config.write_text(
        "smoke:\n"
        "  import_names: {}\n"
        "  workspaces:\n"
        "    flat: {directory: flat_ws, chain: [LibraryA], scripts_dir: '.'}\n"
        "    usual: {directory: usual_ws, chain: [LibraryA]}\n"
    )
    workspaces, _ = smoke.load_smoke_config(config)
    assert workspaces["flat"].scripts_dir == "."
    assert workspaces["usual"].scripts_dir == "scripts"


def test_cli_help_documents_the_new_flags():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["bash", str(root / "bin" / "pyauto-heart"), "help", "smoke"],
        capture_output=True, text=True,
    )
    for flag in ("--affected-by", "--jobs", "--targeted", "--changed-files"):
        assert flag in result.stdout, flag
