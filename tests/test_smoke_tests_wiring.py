"""tests/test_smoke_tests_wiring.py — the workspaces' reusable gate stays wired.

`smoke-tests.yml` is referenced `@main` by eleven workspace repos, so a mistake
in it reds eleven required checks at once. These tests parse the YAML and pin
what the cache work added — a JAX compile cache and a simulated-dataset cache
restored and saved across runs, plus the `cache_state.json` sidecar that rides
the timings artifact — together with the three properties that keep the
addition harmless:

* **every new step is non-fatal** (`continue-on-error: true`), so a cache
  service outage degrades a run to cold rather than to red;
* **the runner's command is untouched** — the step gained one environment
  variable and nothing else;
* **the dataset cache has exactly one fallback key.** That is the correctness
  property of the pair: the JAX cache may read anything compiled for the same
  jaxlib, but a dataset restored under a different simulator or a different
  library commit is not the dataset this run would have written, so a broader
  fallback would be silent corruption of the thing being measured.

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
BEFORE_STEP = "Measure the caches before the run"
RECORD_STEP = "Record cache state"
JAX_SAVE_STEP = "Save the JAX compile cache"
DS_SAVE_STEP = "Save simulated datasets"

NEW_STEPS = (KEYS_STEP, JAX_RESTORE_STEP, DS_RESTORE_STEP, BEFORE_STEP,
             RECORD_STEP, JAX_SAVE_STEP, DS_SAVE_STEP)


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


def test_the_runner_step_names_the_compile_cache_dir_and_is_otherwise_untouched():
    step = _step(_steps(), RUNNER_STEP)
    cache_dir = step["env"]["JAX_COMPILATION_CACHE_DIR"]
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
              BEFORE_STEP, RUNNER_STEP, RECORD_STEP, UPLOAD_STEP)]
    assert order == sorted(order)
    # The saves come after the state they are gated on, and before the notifier.
    assert _index(steps, RECORD_STEP) < _index(steps, JAX_SAVE_STEP)
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
    for output in ("jaxlib=", "chain=", "sims=$SIMS_HASH"):
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
                "DS_EXACT", "JAX_MB_BEFORE", "DS_N_BEFORE"):
        assert key in step["env"], key
    assert "${{" not in run
    assert step["env"]["JAX_KEY_RESTORED"] == \
        "${{ steps.jax-cache.outputs.cache-matched-key }}"
    assert step["env"]["DS_EXACT"] == "${{ steps.dataset-cache.outputs.cache-hit }}"
    assert "jax_changed" in run and "ds_changed" in run


def test_the_saves_differ_on_exactly_one_thing_a_red_run():
    """A compile-cache entry is complete or absent, so a red run's compiling is
    still worth keeping. A killed simulator leaves a truncated FITS that
    `should_simulate` would happily keep, so datasets save only when green."""
    steps = _steps()
    jax = _step(steps, JAX_SAVE_STEP)
    datasets = _step(steps, DS_SAVE_STEP)
    assert jax["if"].startswith("always()")
    assert datasets["if"].startswith("success()")
    # An unchanged cache is not re-uploaded under a new key.
    assert "steps.cache-state.outputs.jax_changed == 'true'" in jax["if"]
    assert "steps.cache-state.outputs.ds_changed == 'true'" in datasets["if"]
    for step, restore in ((jax, JAX_RESTORE_STEP), (datasets, DS_RESTORE_STEP)):
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
