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
