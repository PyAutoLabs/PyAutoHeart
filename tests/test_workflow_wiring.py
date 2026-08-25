"""tests/test_workflow_wiring.py — the validation-channel split stays wired.

One workflow file per meaning (PyAutoHeart#121): workspace-validation.yml is a
workflow_call-only body; workspace-smoke.yml (the continuous channel Heart's
test_run leg reads) and release-integrate.yml (the Stage 3 rehearsal channel
Brain dispatches) are the only entries. These tests parse the YAML so a trigger
or mode drifting back onto the body — recreating the one-channel ambiguity —
fails loudly.

PyYAML parses the bare `on:` key as boolean True, hence the `data[True]` reads.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from heart.checks.test_run import VALIDATION_WORKFLOW

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _triggers(data):
    return data.get("on", data.get(True))


def test_body_is_workflow_call_only():
    triggers = _triggers(_load("workspace-validation.yml"))
    assert set(triggers) == {"workflow_call"}


def test_smoke_entry_owns_the_schedule_and_calls_body_in_smoke_mode():
    data = _load("workspace-smoke.yml")
    triggers = _triggers(data)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    (job,) = data["jobs"].values()
    assert job["uses"].endswith("workspace-validation.yml")
    assert job["with"]["mode"] == "smoke"


def test_release_entry_has_no_mode_input_and_calls_body_in_release_mode():
    data = _load("release-integrate.yml")
    triggers = _triggers(data)
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    for trig in triggers.values():
        inputs = (trig or {}).get("inputs", {})
        assert "mode" not in inputs  # the channel IS the mode
        assert set(inputs) == {"testpypi_version", "commit_shas"}
    (job,) = data["jobs"].values()
    assert job["uses"].endswith("workspace-validation.yml")
    assert job["with"]["mode"] == "release"


def test_test_run_reads_an_existing_entry_workflow():
    assert (WORKFLOWS / VALIDATION_WORKFLOW).is_file()
    # ...and specifically the smoke channel, never the body or release entry.
    assert VALIDATION_WORKFLOW == "workspace-smoke.yml"


def test_smoke_reusable_docs_only_gate_is_wired_fail_closed():
    """The docs-only gate must stay fail-closed: the matrix job runs unless
    the changes job explicitly said docs_only == 'true' (PyAutoHeart#126)."""
    data = _load("smoke-tests.yml")
    jobs = data["jobs"]
    assert "changes" in jobs
    smoke = jobs["smoke"]
    assert smoke["needs"] == "changes"
    assert smoke["if"] == "needs.changes.outputs.docs_only != 'true'"


def _step(job, name_fragment):
    for step in job["steps"]:
        if name_fragment in step.get("name", ""):
            return step
    raise AssertionError(f"no step named like {name_fragment!r}")


def test_smoke_reusable_uploads_the_timing_dataset():
    """The per-script timing dataset leaves the job as an artifact.

    PyAutoHands#264: the runner writes smoke_timings.json into its report dir,
    but a report dir dies with the runner unless something uploads it. The
    artifact name carries the matrix python version so the two legs do not
    collide.
    """
    smoke = _load("smoke-tests.yml")["jobs"]["smoke"]
    step = _step(smoke, "Upload the smoke report dir")

    assert step["uses"].startswith("actions/upload-artifact@v4")
    assert step["with"]["name"] == "smoke-timings-${{ matrix.python-version }}"
    assert "test-results/" in step["with"]["path"]
    assert "smoke_timings.json" in step["with"]["path"]


def test_smoke_timings_upload_cannot_fail_the_gate():
    """A run with no timings is not a red PR.

    Both guards matter: `always()` so a failing script still yields its
    timings, and `if-no-files-found: ignore` so a pre-report crash (or a
    workspace whose runner wrote nothing) does not turn a missing dataset into
    a failed job.
    """
    smoke = _load("smoke-tests.yml")["jobs"]["smoke"]
    step = _step(smoke, "Upload the smoke report dir")

    assert step["if"] == "always()"
    assert step["with"]["if-no-files-found"] == "ignore"


def test_smoke_timings_upload_runs_before_the_slack_notifier():
    """Ordering is the reason the artifact survives a failing run.

    The Slack step is the job's terminal `failure()` hook; the upload has to
    sit ahead of it so the timings for the run that just failed are collected
    rather than skipped.
    """
    names = [s.get("name", "") for s in _load("smoke-tests.yml")["jobs"]["smoke"]["steps"]]
    upload = next(i for i, n in enumerate(names) if "Upload the smoke report dir" in n)
    slack = next(i for i, n in enumerate(names) if "Slack notify" in n)
    assert upload < slack


# --- the weekly sweep's timings need a name too (PyAutoHeart, follow-on to #167) ---
#
# smoke-tests.yml is the PR gate. The WEEKLY sweep runs through
# workspace-validation.yml, which had no named timing upload at all — its
# smoke_timings.json survived only inside the per-leg `results-*` zips, under no
# name a consumer could glob for. These tests pin the fix in both directions:
# the timings are published as `smoke-timings-*`, and they stay OUT of the
# `results-*` namespace the aggregate consumer reads.

TIMING_LEGS = (("run_scripts", "scripts"), ("run_notebooks", "notebooks"))


def _timing_step(job_name):
    jobs = _load("workspace-validation.yml")["jobs"]
    return _step(jobs[job_name], "Upload the per-script timings")


def test_validation_body_publishes_the_timing_dataset():
    """Both weekly legs upload smoke_timings.json under a `smoke-timings-*` name."""
    for job_name, _leg in TIMING_LEGS:
        step = _timing_step(job_name)
        assert step["uses"].startswith("actions/upload-artifact@v4")
        assert step["with"]["name"].startswith("smoke-timings-")
        assert "smoke_timings.json" in step["with"]["path"]


def test_validation_timing_artifact_names_carry_the_leg():
    """A fixed name is safe on the PR gate and fatal here.

    The gate has one leg per python version; this body runs ~50 legs in one
    weekly run, so the name has to carry the (project, directory) pair the
    script matrix keeps unique — exactly as the sibling `results-*` upload does.
    """
    for job_name, leg in TIMING_LEGS:
        name = _timing_step(job_name)["with"]["name"]
        assert name == (
            f"smoke-timings-{leg}-"
            "${{ matrix.project.name }}-${{ matrix.project.directory }}"
        )


def test_validation_timing_upload_cannot_fail_the_weekly_sweep():
    """No timings is not a red sweep — same reasoning as the PR gate's copy."""
    for job_name, _leg in TIMING_LEGS:
        step = _timing_step(job_name)
        assert step["if"].startswith("always()")
        assert step["with"]["if-no-files-found"] == "ignore"


def test_validation_notebook_timing_upload_keeps_the_no_notebooks_gate():
    """The *_test workspaces publish no notebooks — that leg never executes."""
    assert _timing_step("run_notebooks")["if"] == (
        "always() && steps.gate.outputs.run == 'true'"
    )


def test_validation_timing_dataset_keeps_full_default_retention():
    """The dataset is the point.

    The sibling `results-*` uploads expire at 30 days; the timings deliberately
    carry no retention-days so they keep the repo's full default window and
    outlive the report dirs they were extracted from (the PR gate's upload
    omits it for the same reason).
    """
    for job_name, _leg in TIMING_LEGS:
        assert "retention-days" not in _timing_step(job_name)["with"]


def test_timing_artifacts_stay_out_of_the_aggregate_namespace():
    """`results-*` and `smoke-timings-*` are two different contracts.

    `analyze` downloads `results-*` and hands it to aggregate_results.py, which
    globs `**/*.json` and skips the timing sidecar BY NAME. Naming the timing
    artifacts into that pattern would aim them at the one consumer that
    deliberately excludes them.
    """
    analyze = _load("workspace-validation.yml")["jobs"]["analyze"]
    pattern = _step(analyze, "Download all result artifacts")["with"]["pattern"]
    assert pattern == "results-*"

    prefix = pattern.rstrip("*")
    for job_name, _leg in TIMING_LEGS:
        assert not _timing_step(job_name)["with"]["name"].startswith(prefix)
