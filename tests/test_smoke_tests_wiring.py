"""tests/test_smoke_tests_wiring.py — the workspaces' reusable gate stays wired.

`smoke-tests.yml` is referenced `@main` by eleven workspace repos, so a mistake
in it reds eleven required checks at once. These tests parse the YAML and pin
what the cache work added — a JAX compile cache, a numba function cache and a
simulated-dataset cache restored and saved across runs, plus the
`cache_state.json` sidecar that rides the timings artifact — together with the
three properties that keep the addition harmless:

* **every new step is non-fatal** (`continue-on-error: true`), so a cache
  service outage degrades a run to cold rather than to red;
* **the runner's command is untouched** — the step gained one environment
  variable and nothing else;
* **the dataset cache has exactly one fallback key.** That is the correctness
  property of the set: the JAX cache may read anything compiled for the same
  jaxlib and the numba cache anything stamped from the same source content
  (see the stamp step), but a dataset restored under a different simulator or a
  different library commit is not the dataset this run would have written, so a
  broader fallback would be silent corruption of the thing being measured.

The last section pins the `changes` job's two skip gates by RUNNING their
shell — the classification predicate is a `case` glob list, and a glob list is
exactly the kind of thing a string assertion can agree with while the shell
disagrees.

Fake names only, as in the sibling wiring tests: nothing here names a repo, an
owner or a package — the workflow file is the declared surface, the callers are
not.

PyYAML parses the bare `on:` key as boolean True, hence the `data[True]` reads
in the sibling wiring tests; nothing here needs the triggers.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

INSTALL_STEP = "Install (base + the workspace's own epilogue)"
RUNNER_STEP = "Run the workspace runner"
UPLOAD_STEP = "Upload the smoke report dir"

KEYS_STEP = "Resolve cache keys"
JAX_RESTORE_STEP = "Restore the JAX compile cache"
DS_RESTORE_STEP = "Restore simulated datasets"
NUMBA_RESTORE_STEP = "Restore the numba cache"
BEFORE_STEP = "Measure the caches before the run"
STAMP_STEP = "Stamp source mtimes from content"
RECORD_STEP = "Record cache state"
JAX_SAVE_STEP = "Save the JAX compile cache"
NUMBA_SAVE_STEP = "Save the numba cache"
DS_SAVE_STEP = "Save simulated datasets"
PREPARE_STEP = "Prepare cache dirs"

NEW_STEPS = (KEYS_STEP, JAX_RESTORE_STEP, DS_RESTORE_STEP, NUMBA_RESTORE_STEP,
             BEFORE_STEP, STAMP_STEP, RECORD_STEP, JAX_SAVE_STEP,
             NUMBA_SAVE_STEP, DS_SAVE_STEP)


def _job():
    data = yaml.safe_load((WORKFLOWS / "smoke-tests.yml").read_text())
    return data["jobs"]["smoke"]


def _steps():
    return _job()["steps"]


def _index(steps, needle):
    for i, step in enumerate(steps):
        if needle in step.get("name", ""):
            return i
    raise AssertionError(f"no step named like {needle!r} in smoke-tests.yml")


def _step(steps, needle):
    return steps[_index(steps, needle)]


# --- the salt and the cache directory ---------------------------------------

def test_the_epoch_salt_is_declared_once_at_the_job_level():
    """One knob invalidates every cache below it — a runner-image change, a
    cache-format change — instead of an edit per key with one of them missed."""
    assert _job()["env"]["PYAUTO_CACHE_EPOCH"] == "1"


def test_the_runner_step_names_both_cache_dirs_and_is_otherwise_untouched():
    step = _step(_steps(), RUNNER_STEP)
    cache_dir = step["env"]["JAX_COMPILATION_CACHE_DIR"]
    # numba's directory moved out of /tmp: a per-run temporary is neither
    # restorable nor saveable, so it bought nothing across runs.
    numba_dir = step["env"]["NUMBA_CACHE_DIR"]
    assert numba_dir.startswith("${{ github.workspace }}/")
    assert not numba_dir.startswith("/tmp")
    # An explicit path under the workspace: the default is a home-directory
    # cache the cache steps could not name, and the runner copies this job env
    # into every script subprocess, so all of them share the one directory.
    assert cache_dir.startswith("${{ github.workspace }}/")
    # The command itself is untouched — one env key is the whole change.
    assert 'python "$RUNNER" $RUNNER_ARGS' in step["run"]
    assert "continue-on-error" not in step


# --- placement, and non-fatality --------------------------------------------

def test_the_cache_steps_sit_between_the_install_and_the_upload_in_order():
    steps = _steps()
    order = [_index(steps, name) for name in
             (INSTALL_STEP, KEYS_STEP, JAX_RESTORE_STEP, DS_RESTORE_STEP,
              NUMBA_RESTORE_STEP, BEFORE_STEP, STAMP_STEP, PREPARE_STEP,
              RUNNER_STEP, RECORD_STEP, UPLOAD_STEP)]
    assert order == sorted(order)
    # The saves come after the state they are gated on, and before the notifier.
    assert _index(steps, RECORD_STEP) < _index(steps, JAX_SAVE_STEP)
    assert _index(steps, RECORD_STEP) < _index(steps, NUMBA_SAVE_STEP)
    assert _index(steps, RECORD_STEP) < _index(steps, DS_SAVE_STEP)
    assert _index(steps, DS_SAVE_STEP) < _index(steps, "Slack notify")


def test_every_step_the_caches_added_is_non_fatal():
    """Eleven required checks ride this file: a cache miss, a cache-service
    outage or a `du` that failed must cost a warm run, never a green gate."""
    steps = _steps()
    for name in NEW_STEPS:
        assert _step(steps, name)["continue-on-error"] is True, name


# --- the keys ----------------------------------------------------------------

def test_the_key_resolver_takes_its_inputs_through_the_environment():
    step = _step(_steps(), KEYS_STEP)
    # The chain is caller-supplied text; interpolating it into `run:` would
    # splice it straight into the shell (the clone step records the lesson).
    assert step["env"]["CHAIN"] == "${{ inputs.chain }}"
    assert "${{ inputs.chain }}" not in step["run"]
    # hashFiles() is an expression, so it resolves in `env:` and the script
    # reads a plain variable back.
    assert step["env"]["SIMS_HASH"].startswith("${{ hashFiles(")
    assert "hashFiles" not in step["run"]
    for output in ("jaxlib=", "pyfull=", "chain=", "sims=$SIMS_HASH"):
        assert output in step["run"]
    # A jaxlib that will not import is a key component, not a failure.
    assert "nojax" in step["run"]


def test_the_jax_key_is_per_python_leg_per_jaxlib_and_carries_the_salt():
    """A compiled executable is jaxlib-specific and the interpreter's minor
    version is part of what produced it: neither may read the other's entries."""
    step = _step(_steps(), JAX_RESTORE_STEP)
    assert step["uses"].startswith("actions/cache/restore@")
    assert step["id"] == "jax-cache"
    key = step["with"]["key"]
    for component in ("${{ runner.os }}", "${{ matrix.python-version }}",
                      "${{ steps.keys.outputs.jaxlib }}",
                      "${{ env.PYAUTO_CACHE_EPOCH }}", "${{ github.run_id }}"):
        assert component in key, component
    # The accumulating-cache pattern: the exact key (run id) never hits, the
    # prefix always does, and each save is a superset of what was restored.
    (fallback,) = [line for line in step["with"]["restore-keys"].split("\n") if line.strip()]
    assert key.startswith(fallback)
    assert "${{ github.run_id }}" not in fallback
    assert fallback.endswith("-e${{ env.PYAUTO_CACHE_EPOCH }}-")


def test_the_numba_key_is_per_full_python_version_and_carries_the_salt():
    """numba keys every cache entry on the directory its source was found in,
    and the site-packages path carries the PATCH version — so 3.12.3 and 3.12.7
    are different caches, not one."""
    step = _step(_steps(), NUMBA_RESTORE_STEP)
    assert step["uses"].startswith("actions/cache/restore@")
    assert step["id"] == "numba-cache"
    assert step["with"]["path"] == "${{ github.workspace }}/.numba_cache"
    key = step["with"]["key"]
    for component in ("${{ runner.os }}", "${{ steps.keys.outputs.pyfull }}",
                      "${{ env.PYAUTO_CACHE_EPOCH }}", "${{ github.run_id }}"):
        assert component in key, component
    (fallback,) = [line for line in step["with"]["restore-keys"].split("\n")
                   if line.strip()]
    assert key.startswith(fallback)
    assert "${{ github.run_id }}" not in fallback
    assert fallback.endswith("-e${{ env.PYAUTO_CACHE_EPOCH }}-")


def test_the_stamp_step_makes_the_numba_fallback_safe():
    """numba stamps every entry with its source's (mtime, size), and a fresh
    `pip install` changes every mtime — so without this the restored cache
    would miss every entry it holds, and with it a changed file misses its own
    entry however old the restored directory is. That is what lets the key
    above carry a prefix fallback and name no library commit."""
    step = _step(_steps(), STAMP_STEP)
    run = step["run"]
    # Roots reach the script through the environment, never spliced into the
    # heredoc's source — the house rule the key resolver follows too.
    assert "STAMP_DIRS" in step["env"]
    assert "${{" not in run
    # The stamp is a content hash, not a clock reading.
    assert "sha1" in run and "os.utime" in run
    # `find_spec` resolves an installed package WITHOUT importing it.
    assert "find_spec" in run and "submodule_search_locations" in run
    # It has to run before anything writes a numba entry.
    steps = _steps()
    assert _index(steps, STAMP_STEP) < _index(steps, RUNNER_STEP)


def test_the_dataset_key_has_exactly_one_fallback_and_no_broader_one():
    """Correctness over hit rate: a different simulator or a different library
    commit MUST miss, because the FITS it would restore are not the FITS this
    run would have written."""
    step = _step(_steps(), DS_RESTORE_STEP)
    assert step["uses"].startswith("actions/cache/restore@")
    assert step["id"] == "dataset-cache"
    assert step["with"]["path"] == "workspace/dataset"
    key = step["with"]["key"]
    for component in ("${{ steps.keys.outputs.sims }}",
                      "${{ steps.keys.outputs.chain }}",
                      "${{ env.PYAUTO_CACHE_EPOCH }}"):
        assert component in key, component
    # Not keyed on the python leg: simulated FITS do not depend on the
    # interpreter's minor version, so both legs share one entry.
    assert "${{ matrix.python-version }}" not in key
    fallbacks = [line for line in step["with"]["restore-keys"].split("\n") if line.strip()]
    assert len(fallbacks) == 1
    assert fallbacks[0].endswith("-chain${{ steps.keys.outputs.chain }}-")
    assert key.startswith(fallbacks[0])


# --- the sidecar and the saves ----------------------------------------------

def test_the_record_step_runs_on_a_red_run_too_and_writes_the_sidecar():
    """A red run's cache state is exactly as interesting as a green one's — and
    the timings artifact it rides in is uploaded either way."""
    step = _step(_steps(), RECORD_STEP)
    assert step["if"] == "always()"
    assert step["id"] == "cache-state"
    run = step["run"]
    assert '"schema": "cache_state/1"' in run
    assert "workspace/test-results" in run and "cache_state.json" in run
    # Every input reaches Python through the environment, never spliced into
    # the heredoc's source.
    for key in ("EPOCH", "JAX_KEY_RESTORED", "JAX_EXACT", "DS_KEY_RESTORED",
                "DS_EXACT", "JAX_MB_BEFORE", "DS_N_BEFORE",
                "NUMBA_PATH", "NUMBA_KEY_RESTORED", "NUMBA_EXACT",
                "NUMBA_MB_BEFORE", "NUMBA_N_BEFORE"):
        assert key in step["env"], key
    assert "${{" not in run
    assert step["env"]["JAX_KEY_RESTORED"] == \
        "${{ steps.jax-cache.outputs.cache-matched-key }}"
    assert step["env"]["DS_EXACT"] == "${{ steps.dataset-cache.outputs.cache-hit }}"
    assert step["env"]["NUMBA_KEY_RESTORED"] == \
        "${{ steps.numba-cache.outputs.cache-matched-key }}"
    assert "jax_changed" in run and "ds_changed" in run
    assert "numba_changed" in run
    # The numba section is ADDITIVE: the schema string does not move, so an
    # ingest that predates it still reads the sidecar.
    assert '"numba": numba' in run


def test_the_saves_differ_on_exactly_one_thing_a_red_run():
    """A compile-cache entry is complete or absent, so a red run's compiling is
    still worth keeping. A killed simulator leaves a truncated FITS that
    `should_simulate` would happily keep, so datasets save only when green."""
    steps = _steps()
    jax = _step(steps, JAX_SAVE_STEP)
    numba = _step(steps, NUMBA_SAVE_STEP)
    datasets = _step(steps, DS_SAVE_STEP)
    assert jax["if"].startswith("always()")
    # A numba entry is an index line plus a complete artefact, or it is absent:
    # the compile cache's rule, not the dataset's.
    assert numba["if"].startswith("always()")
    assert datasets["if"].startswith("success()")
    # An unchanged cache is not re-uploaded under a new key.
    assert "steps.cache-state.outputs.jax_changed == 'true'" in jax["if"]
    assert "steps.cache-state.outputs.numba_changed == 'true'" in numba["if"]
    assert "steps.cache-state.outputs.ds_changed == 'true'" in datasets["if"]
    for step, restore in ((jax, JAX_RESTORE_STEP), (numba, NUMBA_RESTORE_STEP),
                          (datasets, DS_RESTORE_STEP)):
        assert step["uses"].startswith("actions/cache/save@")
        source = _step(steps, restore)["with"]
        assert step["with"]["path"] == source["path"]
        assert step["with"]["key"] == source["key"]


def test_the_upload_step_is_untouched_by_the_cache_work():
    """The sidecar rides the artifact that already exists — a second upload
    would be a second channel for one measurement."""
    step = _step(_steps(), UPLOAD_STEP)
    assert step["if"] == "always()"
    assert step["with"]["if-no-files-found"] == "ignore"
    assert "workspace/test-results/" in step["with"]["path"]
    assert "retention-days" not in step["with"]


def test_the_sidecar_name_and_schema_are_what_the_heart_check_reads():
    """One filename and one schema string, declared in two places that must
    agree — the emitter in the workflow and the ingest in the check."""
    from heart.checks.smoke_timings import CACHE_STATE_FILENAME, CACHE_STATE_SCHEMA

    run = _step(_steps(), RECORD_STEP)["run"]
    assert CACHE_STATE_FILENAME in run
    assert f'"schema": "{CACHE_STATE_SCHEMA}"' in run


# --- the fixed overhead: what the gate spends before the first script -------
# Measured over six runs of one `_test` workspace (12 legs): 104 s mean from
# job start to the first script, 22.6% of the job, of which the install is
# ~84 s and the dependency-chain clone ~16.5 s. These tests pin the three
# things done about it — shallow clones, a pip wheel cache, and the marks that
# make the overhead a measured field instead of a hand-scraped one.

PIP_CACHE_STEP = "Restore the pip wheel cache"
CLONE_STEP = "Clone dependency chain"
JOB_MARK_STEP = "Mark job start"
SCRIPTS_MARK_STEP = "Mark scripts start"

OVERHEAD_STEPS = (JOB_MARK_STEP, PIP_CACHE_STEP, SCRIPTS_MARK_STEP)


def test_the_chain_is_cloned_shallow_on_both_the_clone_and_the_fallback():
    """Nothing downstream reads this history: the libraries are installed from
    the trees, and the only other reader is `rev-parse HEAD`, which answers on
    a shallow clone. So both network legs are depth 1 — a full clone in the
    fallback would hand back every byte the shallow clone saved."""
    run = _step(_steps(), CLONE_STEP)["run"]
    assert "git clone --depth 1 " in run
    assert "git clone \"https" not in run
    assert 'fetch --depth 1 origin "$BRANCH"' in run
    # A shallow clone is single-branch, so there is no `origin/$BRANCH` for
    # `git checkout` to DWIM from — FETCH_HEAD is what the fetch above leaves.
    assert 'checkout -B "$BRANCH" FETCH_HEAD' in run


def test_the_matching_branch_fallback_survives_the_shallow_clone():
    """The reason this step exists at all: a library PR must still be tested
    against its workspace. Depth is an optimisation; this is the semantics."""
    run = _step(_steps(), CLONE_STEP)["run"]
    assert 'ls-remote --exit-code --heads origin "$BRANCH"' in run
    assert "PyAutoHands" in run          # cloned in the same loop, same depth
    # Still env-not-interpolation for the branch and the owner, and the chain
    # still reaches the loop as the caller-supplied text it is.
    assert 'BRANCH="${{ github.head_ref || github.ref_name }}"' in run
    assert 'OWNER="${{ github.repository_owner }}"' in run


def test_the_pip_cache_is_a_fourth_cache_under_the_same_rules():
    """Non-fatal, salted with the same epoch, one prefix fallback: the three
    properties every cache in this file already holds. `setup-python`'s own
    `cache:` can hold none of them — it cannot be `continue-on-error`, it
    cannot carry the salt, and a `cache-dependency-path` matching no file is a
    hard error in an action that eleven required checks ride."""
    steps = _steps()
    step = _step(steps, PIP_CACHE_STEP)
    assert step["uses"].startswith("actions/cache@")
    assert step["id"] == "pip-cache"
    assert step["continue-on-error"] is True
    assert step["with"]["path"] == "~/.cache/pip"
    key = step["with"]["key"]
    for component in ("${{ runner.os }}", "${{ matrix.python-version }}",
                      "${{ env.PYAUTO_CACHE_EPOCH }}", "hashFiles("):
        assert component in key, component
    (fallback,) = [line for line in step["with"]["restore-keys"].split("\n")
                   if line.strip()]
    assert key.startswith(fallback)
    assert "hashFiles(" not in fallback
    assert fallback.endswith("-e${{ env.PYAUTO_CACHE_EPOCH }}-")
    # setup-python is left alone: no `cache:` of its own to race this one.
    assert "cache" not in _step(steps, "Set up Python")["with"]


def test_the_pip_key_hashes_the_dependency_declarations_it_can_see():
    """The libraries ship their dependencies in `pyproject.toml`, not in a
    requirements file, and the workspace may carry either or neither. All the
    shapes are named; `hashFiles()` returns "" for the ones that match nothing,
    which is a constant key rather than the error `setup-python` would raise."""
    key = _step(_steps(), PIP_CACHE_STEP)["with"]["key"]
    for pattern in ("*/pyproject.toml", "*/setup.py",
                    "workspace/requirements*.txt"):
        assert pattern in key, pattern
    # The globs only resolve if the chain is already on disk: this step must
    # come after the clone, and before the install whose downloads it caches.
    steps = _steps()
    assert _index(steps, CLONE_STEP) < _index(steps, PIP_CACHE_STEP)
    assert _index(steps, PIP_CACHE_STEP) < _index(steps, INSTALL_STEP)


def test_the_two_marks_bracket_exactly_the_setup():
    """`setup_s` is only honest if the first mark is the first thing the job
    does and the second is the last thing before the scripts."""
    steps = _steps()
    assert _index(steps, JOB_MARK_STEP) == 0
    assert _index(steps, SCRIPTS_MARK_STEP) + 1 == _index(steps, RUNNER_STEP)
    for name, stamp in ((JOB_MARK_STEP, "job_start"),
                        (SCRIPTS_MARK_STEP, "scripts_start")):
        step = _step(steps, name)
        run = step["run"]
        assert "date +%s" in run and stamp in run
        assert "$RUNNER_TEMP" in run
        # Env-free and expression-free: nothing here to interpolate.
        assert "env" not in step
        assert "${{" not in run


def test_every_step_the_overhead_work_added_is_non_fatal():
    """Same rule as the caches: a mark we could not write or a cache service
    outage costs a measurement, never one of eleven required checks."""
    steps = _steps()
    for name in OVERHEAD_STEPS:
        assert _step(steps, name)["continue-on-error"] is True, name


def test_the_recorder_carries_setup_s_and_stays_expression_free():
    """The overhead becomes DATA in the sidecar the Heart already ingests,
    rather than a number scraped out of the Actions UI by hand."""
    step = _step(_steps(), RECORD_STEP)
    run = step["run"]
    assert '"setup_s": setup_seconds()' in run
    assert "job_start" in run and "scripts_start" in run
    # Read from the runner's own environment, not spliced in — the house rule.
    assert 'os.environ.get("RUNNER_TEMP")' in run
    assert "${{" not in run
    # Absent or unreadable marks are None, never a fabricated zero.
    assert "return None" in run


def test_the_runner_command_is_still_byte_identical_and_still_fatal():
    """The one thing in this workflow that must not move: a workspace owns its
    runner. The second mark is a step BEFORE it, not an edit to it."""
    step = _step(_steps(), RUNNER_STEP)
    assert 'python "$RUNNER" $RUNNER_ARGS' in step["run"]
    assert "date +%s" not in step["run"]
    assert "continue-on-error" not in step


# --- the skip gates: run the classifier, do not just read it ----------------
#
# `changes` decides whether eleven required checks do any work. Its verdict is
# a pair of `case` glob lists in a shell heredoc, so the honest test executes
# the step's own `run:` body against a synthetic diff rather than asserting on
# its text. `git` is the only thing stubbed — the fetch's exit status and the
# `--name-only` output ARE the two facts the guard reads — so the branch
# structure, the two loops, the `$GITHUB_OUTPUT` writes and the step summary
# are all the real ones from the file.
#
# The two reasons (PyAutoHeart#126, #219):
#   docs_only                 — the diff is prose and nothing else.
#   no_smoke_relevant_changes — nothing under scripts/, config/, .github/ and
#                               neither entry list, so no smoke script this
#                               gate runs can be affected.

CLASSIFY_STEP = "Classify the diff"
A_BASE = "1" * 40
ZERO_SHA = "0" * 40

GIT_STUB = """#!/usr/bin/env bash
case "$1" in
  fetch) exit "${FAKE_GIT_FETCH_RC:-0}" ;;
  diff)  printf '%s' "$FAKE_GIT_FILES" ;;
  *)     exit 1 ;;
esac
"""


def _changes_job():
    data = yaml.safe_load((WORKFLOWS / "smoke-tests.yml").read_text())
    return data["jobs"]["changes"]


def _classify_step():
    return _step(_changes_job()["steps"], CLASSIFY_STEP)


def _classify(tmp_path, files, *, event_name="pull_request", base=A_BASE,
              fetch_ok=True):
    """Run the real `Classify the diff` body over a synthetic file list.

    Returns ``(flags, step_summary)`` — the `$GITHUB_OUTPUT` pairs the step
    wrote, and the markdown a reader of the run would see.
    """
    bindir = tmp_path / "bin"
    # exist_ok: a test may classify several diffs under the one tmp_path.
    bindir.mkdir(exist_ok=True)
    (bindir / "git").write_text(GIT_STUB)
    (bindir / "git").chmod(0o755)
    out = tmp_path / "github_output"
    summary = tmp_path / "step_summary"
    out.write_text("")
    summary.write_text("")

    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}{os.pathsep}{env['PATH']}",
        BASE=base,
        EVENT_NAME=event_name,
        GITHUB_OUTPUT=str(out),
        GITHUB_STEP_SUMMARY=str(summary),
        FAKE_GIT_FILES="\n".join(files),
        FAKE_GIT_FETCH_RC="0" if fetch_ok else "1",
    )
    # `-e` is the shell GitHub runs `run:` under; a body that only works
    # without it would pass here and fail on the runner.
    result = subprocess.run(
        ["bash", "-e", "-c", _classify_step()["run"]],
        env=env, cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    flags = dict(
        line.split("=", 1) for line in out.read_text().splitlines() if line
    )
    return flags, summary.read_text()


def test_the_classifier_reads_the_event_name_and_the_base_from_the_env():
    """Both are `${{ }}` expressions, and both belong in `env:` rather than
    spliced into the shell — the house rule the key resolver follows too."""
    step = _classify_step()
    assert step["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert step["env"]["BASE"] == (
        "${{ github.event.pull_request.base.sha || github.event.before }}"
    )
    run = step["run"]
    assert "${{" not in run
    # One diff, two verdicts: the second pass must not re-derive the file list,
    # and the two-dot form against the base TIP is deliberate — upstream drift
    # then shows up as extra files and conservatively runs the matrix.
    assert run.count("git diff --name-only") == 1
    assert 'git diff --name-only "$BASE" HEAD' in run
    assert "merge-base" not in run


@pytest.mark.parametrize("path", [
    "scripts/imaging/start_here.py",
    "scripts/misc/x.py",
    "config/grids.yaml",
    "config/visualize/plots.yaml",
    "smoke_tests.txt",
    "smoke_notebooks.txt",
    ".github/scripts/run_smoke.py",
    ".github/workflows/smoke_tests.yml",
])
def test_a_smoke_relevant_path_runs_the_matrix(tmp_path, path):
    """The entries are paths in the two lists, resolved under `scripts/`, run
    with the workspace's `config/` and this file's own ceremony (which the
    caller workflow and the vendored runner under `.github/` supply)."""
    flags, _ = _classify(tmp_path, [path])
    assert flags["no_smoke_relevant_changes"] == "false", path


@pytest.mark.parametrize("files", [
    ["notebooks/imaging/start_here.ipynb"],
    ["setup.py"],
    ["dataset/imaging/simple/data.fits"],
    ["output/.gitkeep", "requirements.txt"],
    # A single relevant path anywhere in the diff is enough to run everything.
])
def test_a_diff_touching_no_smoke_relevant_path_skips(tmp_path, files):
    flags, _ = _classify(tmp_path, files)
    assert flags["no_smoke_relevant_changes"] == "true"


def test_one_relevant_path_among_many_irrelevant_ones_runs_the_matrix(tmp_path):
    """The loop breaks on the FIRST match, so the verdict must not depend on
    where in the diff that match happens to sit."""
    irrelevant = ["notebooks/a.ipynb", "setup.py", "output/keep"]
    for i in range(len(irrelevant) + 1):
        files = irrelevant[:i] + ["scripts/imaging/x.py"] + irrelevant[i:]
        flags, _ = _classify(tmp_path, files)
        assert flags["no_smoke_relevant_changes"] == "false", files


def test_the_two_reasons_are_independent_and_a_docs_diff_satisfies_both(tmp_path):
    """Nothing has to choose between them: they are ORed in the `smoke` job's
    `if:`, so a diff that is both prose AND smoke-irrelevant is simply skipped
    twice over."""
    flags, summary = _classify(tmp_path, ["README.md", "docs/index.rst"])
    assert flags["docs_only"] == "true"
    assert flags["no_smoke_relevant_changes"] == "true"
    # One reason per run, and the narrower, older one wins the summary.
    assert "docs/metadata-only change" in summary
    assert "no smoke-relevant change" not in summary


def test_the_docs_only_verdict_is_unchanged_by_the_relevance_gate(tmp_path):
    """#219 added a reason; it must not have edited the existing one. A config
    sidecar is smoke-RELEVANT and not prose, so it flips both the other way."""
    flags, _ = _classify(tmp_path, ["README.md", "config/grids.yaml"])
    assert flags["docs_only"] == "false"
    assert flags["no_smoke_relevant_changes"] == "false"


@pytest.mark.parametrize("kwargs", [
    {"base": ""},                    # no base sha at all
    {"base": ZERO_SHA},              # the all-zero sha of a branch's first push
    {"fetch_ok": False},             # a base the shallow fetch cannot reach
])
def test_every_unresolvable_base_runs_the_whole_matrix(tmp_path, kwargs):
    """FAIL CLOSED. Both flags are initialised `false` BEFORE the guard, so
    every early exit out of it leaves the matrix running — a diff that could
    not be read is not a diff that touched nothing."""
    flags, summary = _classify(
        tmp_path, ["notebooks/a.ipynb"], **kwargs
    )
    assert flags == {"docs_only": "false", "no_smoke_relevant_changes": "false"}
    assert summary == ""


def test_an_empty_diff_runs_the_whole_matrix(tmp_path):
    """A reachable base that reports no files is the same unknown: vacuously
    'no path is smoke-relevant' must not read as a verdict."""
    flags, _ = _classify(tmp_path, [])
    assert flags == {"docs_only": "false", "no_smoke_relevant_changes": "false"}


@pytest.mark.parametrize("event_name", ["push", "workflow_dispatch", "schedule"])
def test_the_relevance_gate_is_pull_request_only(tmp_path, event_name):
    """The `main` push must keep running the full matrix. `config/repos.yaml`
    lists "Smoke Tests" under `required_workflows` for the workspace groups;
    `ci_status` reads its conclusion on the `main` HEAD commit and only an
    explicit `success` rolls up green. Narrowing the PR side is free — Heart
    never reads it — so the saving is taken there and only there."""
    flags, summary = _classify(
        tmp_path, ["notebooks/a.ipynb"], event_name=event_name
    )
    assert flags["no_smoke_relevant_changes"] == "false"
    assert summary == ""


def test_the_docs_only_gate_is_not_narrowed_to_pull_request(tmp_path):
    """The event test belongs to the NEW reason only: gating the old one would
    be a change to push-to-`main` behaviour, which #219 explicitly is not."""
    flags, summary = _classify(tmp_path, ["README.md"], event_name="push")
    assert flags["docs_only"] == "true"
    assert "docs/metadata-only change" in summary


def test_a_relevance_skip_shows_the_reader_what_it_classified(tmp_path):
    """A skipped run has to explain itself the way the docs-only path does, or
    the next person to wonder why the matrix did not run has only the `if:`."""
    files = ["notebooks/imaging/start_here.ipynb", "setup.py"]
    _, summary = _classify(tmp_path, files)
    assert "### Smoke matrix skipped — no smoke-relevant change" in summary
    for path in files:
        assert f"`{path}`" in summary
    # The reason names the allowlist, so the summary answers "why" and not just
    # "which" — every directory the predicate actually tests.
    for pattern in ("scripts/", "config/", ".github/",
                    "smoke_tests.txt", "smoke_notebooks.txt"):
        assert pattern in summary, pattern
