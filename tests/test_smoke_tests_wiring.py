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

Fake names only, as in the sibling wiring tests: nothing here names a repo, an
owner or a package — the workflow file is the declared surface, the callers are
not.

PyYAML parses the bare `on:` key as boolean True, hence the `data[True]` reads
in the sibling wiring tests; nothing here needs the triggers.
"""

from __future__ import annotations

from pathlib import Path

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
