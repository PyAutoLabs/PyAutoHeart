"""tests/test_lib_tests_wiring.py — the libraries' reusable gate stays wired.

`lib-tests.yml` is referenced `@main` by six library repos, so a mistake in it
reds six required checks at once. These tests parse the YAML and pin the three
things #206 added to the `unittest` job — the fresh-process import timing, the
junit XML, and the `unit-timings-<py>` artifact the Heart's `unit_timings` check
ingests — plus the two properties that keep the addition harmless: every new
step is non-fatal, and the pytest exit path is untouched apart from one flag.

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
