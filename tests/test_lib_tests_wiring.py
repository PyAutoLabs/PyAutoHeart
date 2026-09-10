"""tests/test_lib_tests_wiring.py — the libraries' reusable gate stays wired.

`lib-tests.yml` is referenced `@main` by six library repos, so a mistake in it
reds six required checks at once. These tests parse the YAML and pin the three
things #206 added to the `unittest` job — the fresh-process import timing, the
junit XML, and the `unit-timings-<py>` artifact the Heart's `unit_timings` check
ingests — plus the two properties that keep the addition harmless: every new
step is non-fatal, and the pytest exit path is untouched apart from one flag.

They also pin the TWO CACHES the job gained — a JAX compile cache and a numba
function cache, with `smoke-tests.yml`'s discipline (one epoch salt, the
accumulating run-id key, a single prefix fallback, a `cache_state.json` sidecar
riding the existing artifact, saves gated on a measured change) — and the one
thing that makes the numba half work at all: the source mtimes are stamped from
CONTENT, because numba keys every cache entry on its source file's
`(mtime, size)` and a fresh clone or a fresh `pip install` changes every mtime.
That stamp is what makes the prefix fallback safe and lets the numba key name
no library commit.

The `unittest-nojax` job is asserted UNCHANGED on purpose: it exists to prove
the jax-absent import path, it runs one python version, and a second dataset
from it would be a second channel measuring a different environment.

PyYAML parses the bare `on:` key as boolean True, hence the `data[True]` reads
in the sibling wiring tests; nothing here needs the triggers.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

ARTIFACT_NAME = "unit-timings-${{ matrix.python-version }}"
IMPORT_STEP = "Time a fresh-process import"
UPLOAD_STEP = "Upload the unit timings"

INSTALL_STEP = "Install (deps + package from source"
KEYS_STEP = "Resolve cache keys"
JAX_RESTORE_STEP = "Restore the JAX compile cache"
NUMBA_RESTORE_STEP = "Restore the numba cache"
STAMP_STEP = "Stamp source mtimes from content"
BEFORE_STEP = "Measure the caches before the run"
RECORD_STEP = "Record cache state"
JAX_SAVE_STEP = "Save the JAX compile cache"
NUMBA_SAVE_STEP = "Save the numba cache"

NEW_STEPS = (KEYS_STEP, JAX_RESTORE_STEP, NUMBA_RESTORE_STEP, STAMP_STEP,
             BEFORE_STEP, RECORD_STEP, JAX_SAVE_STEP, NUMBA_SAVE_STEP)


def _jobs():
    return yaml.safe_load((WORKFLOWS / "lib-tests.yml").read_text())["jobs"]


def _steps(job_name):
    return _jobs()[job_name]["steps"]


def _index(steps, needle):
    for i, step in enumerate(steps):
        if needle in step.get("name", ""):
            return i
    raise AssertionError(f"no step named like {needle!r} in lib-tests.yml")


def _step(steps, needle):
    return steps[_index(steps, needle)]


def test_the_import_timing_step_runs_before_the_suite_and_cannot_red_the_gate():
    steps = _steps("unittest")
    assert _index(steps, IMPORT_STEP) < _index(steps, "Run tests")
    step = _step(steps, IMPORT_STEP)
    # A dataset, not a gate: a package that cannot be imported fails the suite
    # on its own merits two steps down.
    assert step["continue-on-error"] is True
    assert step["working-directory"] == "${{ steps.map.outputs.repo }}"
    # The package name reaches Python through the environment, never
    # interpolated into the heredoc's source.
    assert step["env"]["PKG"] == "${{ inputs.package }}"
    assert "${{ inputs.package }}" not in step["run"]
    assert '"schema": "import_time/1"' in step["run"]
    assert "test-results" in step["run"]


def test_the_suite_writes_pytest_s_own_junit_record():
    """--junitxml is the built-in per-test record; --durations is a console
    tail that has to be scraped out of a log that expires with the run."""
    run = _step(_steps("unittest"), "Run tests")["run"]
    assert "--junitxml=test-results/junit.xml" in run
    # The exit path is untouched apart from that one flag.
    assert "--cov ${{ inputs.package }} --cov-report xml:coverage.xml" in run
    assert "continue-on-error" not in _step(_steps("unittest"), "Run tests")


def test_the_artifact_is_named_per_python_leg_and_never_fails_the_gate():
    steps = _steps("unittest")
    step = _step(steps, UPLOAD_STEP)
    assert step["if"] == "always()"          # a red suite still has timings
    assert step["uses"].startswith("actions/upload-artifact@")
    assert step["with"]["name"] == ARTIFACT_NAME
    assert step["with"]["path"] == "${{ steps.map.outputs.repo }}/test-results/"
    # A leg that died before the first write must not red six libraries.
    assert step["with"]["if-no-files-found"] == "ignore"
    # No retention cap: the dataset is the point (smoke-tests.yml reasons the
    # same way about its own timings artifact).
    assert "retention-days" not in step["with"]
    assert _index(steps, "Run tests") < _index(steps, UPLOAD_STEP)


def test_the_nojax_job_is_unchanged():
    """One dataset, one environment: the jax-absent leg proves imports work
    without jax, and timing it would measure a different thing under the same
    name."""
    steps = _steps("unittest-nojax")
    names = [step.get("name", "") for step in steps]
    assert not any(IMPORT_STEP in name or UPLOAD_STEP in name for name in names)
    run = _step(steps, "Run tests (jax absent)")["run"]
    assert "--junitxml" not in run
    assert not any(str(step.get("uses", "")).startswith("actions/upload-artifact")
                   for step in steps)


def test_the_artifact_name_is_what_the_heart_check_selects():
    """One name, declared in two places that must agree — the emitter here and
    the ingest's ARTIFACT_RE."""
    from heart.checks.unit_timings import ARTIFACT_RE

    assert ARTIFACT_RE.match("unit-timings-3.12")
    assert ARTIFACT_NAME.startswith("unit-timings-")


# --- the two caches ---------------------------------------------------------

def test_the_epoch_salt_is_declared_once_at_the_job_level():
    """One knob invalidates every cache below it — a runner-image change, a
    cache-format change — instead of an edit per key with one of them missed.
    Declared on `unittest` alone: the no-jax leg caches nothing.

    The VALUE is deliberately not pinned: the salt exists to be bumped, so a
    test that names the current number fails on the one edit it is there to
    permit (it did, on the 1 -> 2 bump that came with the CPU pin below). What
    must hold is that there is exactly one of them, it is a non-empty salt, and
    every key reads it from the job."""
    jobs = _jobs()
    epoch = jobs["unittest"]["env"]["PYAUTO_CACHE_EPOCH"]
    assert isinstance(epoch, str) and epoch.strip()
    assert "env" not in jobs["unittest-nojax"]
    for name in (JAX_RESTORE_STEP, NUMBA_RESTORE_STEP,
                 JAX_SAVE_STEP, NUMBA_SAVE_STEP):
        key = _step(_steps("unittest"), name)["with"]["key"]
        assert "${{ env.PYAUTO_CACHE_EPOCH }}" in key, name


def test_the_cache_steps_sit_in_order_between_the_install_and_the_upload():
    steps = _steps("unittest")
    order = [_index(steps, name) for name in
             (INSTALL_STEP, KEYS_STEP, JAX_RESTORE_STEP, NUMBA_RESTORE_STEP,
              STAMP_STEP, BEFORE_STEP, "Run tests", RECORD_STEP, UPLOAD_STEP)]
    assert order == sorted(order)
    # The saves come after the state they are gated on, and the notifier stays
    # last so a failure is still announced.
    assert _index(steps, RECORD_STEP) < _index(steps, JAX_SAVE_STEP)
    assert _index(steps, RECORD_STEP) < _index(steps, NUMBA_SAVE_STEP)
    assert _index(steps, NUMBA_SAVE_STEP) < _index(steps, "Slack on failure")
    assert _index(steps, "Slack on failure") == len(steps) - 1


def test_every_step_the_caches_added_is_non_fatal():
    """Six required checks ride this file: a cache miss, a cache-service outage
    or a `du` that failed must cost a warm run, never a green gate."""
    steps = _steps("unittest")
    for name in NEW_STEPS:
        assert _step(steps, name)["continue-on-error"] is True, name


def test_the_key_resolver_publishes_the_two_components_the_keys_need():
    step = _step(_steps("unittest"), KEYS_STEP)
    assert step["id"] == "keys"
    run = step["run"]
    assert "jaxlib=" in run and "pyfull=" in run
    # A jaxlib that will not import is a key component, not a failure.
    assert "nojax" in run
    # The full version, patch included: numba's entries hash the site-packages
    # path, which carries it.
    assert "sys.version.split()[0]" in run
    # No chain hash here — there is no dataset cache in this workflow.
    assert "sims" not in run and "chain" not in run


def test_the_jax_key_is_per_python_leg_per_jaxlib_and_carries_the_salt():
    """A compiled executable is jaxlib-specific and the interpreter's minor
    version is part of what produced it: neither may read the other's entries."""
    step = _step(_steps("unittest"), JAX_RESTORE_STEP)
    assert step["uses"].startswith("actions/cache/restore@")
    assert step["id"] == "jax-cache"
    assert step["with"]["path"] == "${{ github.workspace }}/.pyauto_jax_cache"
    key = step["with"]["key"]
    for component in ("${{ runner.os }}", "${{ matrix.python-version }}",
                      "${{ steps.keys.outputs.jaxlib }}",
                      "${{ env.PYAUTO_CACHE_EPOCH }}", "${{ github.run_id }}"):
        assert component in key, component
    _assert_one_prefix_fallback(step)


def test_the_numba_key_is_per_full_python_version_and_carries_the_salt():
    """numba keys every cache entry on the directory its source was found in,
    and the site-packages path carries the PATCH version — so 3.12.3 and 3.12.7
    are different caches, not one."""
    step = _step(_steps("unittest"), NUMBA_RESTORE_STEP)
    assert step["uses"].startswith("actions/cache/restore@")
    assert step["id"] == "numba-cache"
    assert step["with"]["path"] == "${{ github.workspace }}/.numba_cache"
    key = step["with"]["key"]
    for component in ("${{ runner.os }}", "${{ steps.keys.outputs.pyfull }}",
                      "${{ env.PYAUTO_CACHE_EPOCH }}", "${{ github.run_id }}"):
        assert component in key, component
    _assert_one_prefix_fallback(step)


def _assert_one_prefix_fallback(step):
    """The accumulating-cache pattern: the exact key (run id) never hits, the
    single prefix always does, and each save is a superset of what was
    restored. Exactly one fallback — a second, broader line would be a claim
    nobody made about what may be read."""
    key = step["with"]["key"]
    fallbacks = [line for line in step["with"]["restore-keys"].split("\n")
                 if line.strip()]
    assert len(fallbacks) == 1
    (fallback,) = fallbacks
    assert key.startswith(fallback)
    assert "${{ github.run_id }}" not in fallback
    assert fallback.endswith("-e${{ env.PYAUTO_CACHE_EPOCH }}-")


def test_the_stamp_step_makes_the_numba_fallback_safe():
    """numba stamps every entry with its source's (mtime, size), and a fresh
    clone or a fresh `pip install` changes every mtime — so without this the
    restored cache would miss every entry it holds, and with it a changed file
    misses its own entry however old the restored directory is. That is what
    lets the key above carry a prefix fallback and name no library commit.

    Necessary, not sufficient: the CPU pin in the next test is the other half
    (numba's index key carries the host CPU as well as the source stamp)."""
    step = _step(_steps("unittest"), STAMP_STEP)
    run = step["run"]
    # The roots reach the script through the environment, never spliced into
    # the heredoc's source (the import-timing step records the lesson).
    assert step["env"]["STAMP_DIRS"] == \
        "${{ steps.map.outputs.repo }} ${{ steps.map.outputs.deps }}"
    assert "${{" not in run
    # A content hash, not a clock reading.
    assert "sha1" in run and "os.utime" in run
    # The installed copies too, resolved WITHOUT importing them.
    assert "find_spec" in run and "submodule_search_locations" in run


def test_the_suite_pins_numbas_cpu_so_the_restored_numba_cache_can_be_read():
    """The other half of the stamp above, and the reason a warm-looking numba
    cache was never read.

    numba's per-entry index key is `(signature, codegen.magic_tuple(),
    sha256(co_code), sha256(closure))`, and `magic_tuple()` is `(llvm triple,
    host CPU name, host CPU feature string)`. `ubuntu-latest` is a
    heterogeneous pool, so an entry written on one CPU model is unreadable on
    another: the job recompiles and `IndexDataCacheFile.save` APPENDS a second
    `.nbc` beside the first, which is how a cache grows every run and is never
    read. Measured on PyAutoLens (`cache_state.json`, both python legs): numba
    entries 0 -> 38 cold, then 57, then 76 — plus exactly 19 every run, one per
    cached function, zero hits — while PyAutoGalaxy runs 2918 and 2920 restored
    the SAME cache and added 0 and 3 files respectively, a hit and a miss off
    identical bytes.

    Pinning both components removes the host from the key and makes the
    artefact genuinely host-independent, which is sound in a way a broader
    cache key would not be: nothing else that invalidates an entry is
    weakened (the content stamp, the bytecode hash, the python version in the
    cache key, numba's own version check). The cost is generic codegen, which
    this gate can afford and `smoke-tests.yml` — whose per-script wall-clock IS
    the Heart's dataset — deliberately does not take."""
    step = _step(_steps("unittest"), "Run tests")
    assert step["env"]["NUMBA_CPU_NAME"] == "generic"
    assert step["env"]["NUMBA_CPU_FEATURES"] == ""


def test_the_suite_runs_under_both_cache_dirs_and_is_otherwise_untouched():
    step = _step(_steps("unittest"), "Run tests")
    for name in ("JAX_COMPILATION_CACHE_DIR", "NUMBA_CACHE_DIR"):
        assert step["env"][name].startswith("${{ github.workspace }}/"), name
    # The command itself is untouched — the env block is the whole change.
    assert "export JAX_ENABLE_X64=True" in step["run"]
    assert "--junitxml=test-results/junit.xml" in step["run"]


def test_the_record_step_runs_on_a_red_run_too_and_writes_the_sidecar():
    """A red run's cache state is exactly as interesting as a green one's — and
    the artifact it rides in is uploaded either way."""
    from heart.checks.smoke_timings import CACHE_STATE_FILENAME, CACHE_STATE_SCHEMA

    step = _step(_steps("unittest"), RECORD_STEP)
    assert step["if"] == "always()"
    assert step["id"] == "cache-state"
    run = step["run"]
    # One filename and one schema string, declared in two places that must
    # agree — the emitter here and the ingest in the check.
    assert CACHE_STATE_FILENAME in run
    assert f'"schema": "{CACHE_STATE_SCHEMA}"' in run
    # Two sections, no `datasets`: there is no dataset cache in this workflow,
    # so there would be nothing honest to say under it.
    assert '"jax": jax' in run and '"numba": numba' in run
    assert '"datasets"' not in run
    assert '"epoch"' in run
    # It writes beside the timings the upload step already collects.
    assert step["env"]["OUT_DIR"] == "${{ steps.map.outputs.repo }}/test-results"
    # Every input reaches Python through the environment, never spliced into
    # the heredoc's source.
    for key in ("EPOCH", "OUT_DIR",
                "JAX_PATH", "JAX_KEY_RESTORED", "JAX_EXACT", "JAX_MB_BEFORE",
                "JAX_N_BEFORE", "NUMBA_PATH", "NUMBA_KEY_RESTORED",
                "NUMBA_EXACT", "NUMBA_MB_BEFORE", "NUMBA_N_BEFORE"):
        assert key in step["env"], key
    assert "${{" not in run
    assert step["env"]["JAX_KEY_RESTORED"] == \
        "${{ steps.jax-cache.outputs.cache-matched-key }}"
    assert step["env"]["NUMBA_EXACT"] == \
        "${{ steps.numba-cache.outputs.cache-hit }}"
    assert "jax_changed" in run and "numba_changed" in run


def test_both_saves_run_on_a_red_run_and_mirror_their_restores():
    """A compile-cache entry — jax or numba — is complete or absent, never
    half-written, so a failing run's compiling is still worth keeping. There is
    no dataset here whose truncation would have to be guarded against."""
    steps = _steps("unittest")
    jax = _step(steps, JAX_SAVE_STEP)
    numba = _step(steps, NUMBA_SAVE_STEP)
    assert jax["if"] == "always() && steps.cache-state.outputs.jax_changed == 'true'"
    assert numba["if"] == \
        "always() && steps.cache-state.outputs.numba_changed == 'true'"
    for step, restore in ((jax, JAX_RESTORE_STEP), (numba, NUMBA_RESTORE_STEP)):
        assert step["uses"].startswith("actions/cache/save@")
        source = _step(steps, restore)["with"]
        assert step["with"]["path"] == source["path"]
        assert step["with"]["key"] == source["key"]


def test_the_nojax_job_gained_no_cache_step():
    """It exists to prove the jax-absent import path on one python version. A
    cache there would key on a jaxlib that is deliberately not installed, and
    the epoch salt it would need is not even declared on the job."""
    names = [step.get("name", "") for step in _steps("unittest-nojax")]
    for name in NEW_STEPS:
        assert not any(name in candidate for candidate in names), name
