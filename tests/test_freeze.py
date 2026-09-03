"""tests/test_freeze.py — the release-validation freeze flag.

The flag's whole job is to be believed by three surfaces that do not run in
this process, so what is tested here is the contract they read: the file's
shape, the three readings (clear / active / expired), the exit codes, and the
one line every surface prints. `conftest.py` has already pointed
`HEART_STATE_DIR` at a throwaway directory, so none of this can reach the
developer's live state.
"""

from __future__ import annotations

import datetime
import json

import pytest

from heart import freeze


@pytest.fixture(autouse=True)
def _clean_slate():
    """Every test starts with no flag and leaves none behind."""
    freeze.FREEZE_FILE.unlink(missing_ok=True)
    yield
    freeze.FREEZE_FILE.unlink(missing_ok=True)


def _at(hours: float) -> datetime.datetime:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=hours))


# --- reading nothing --------------------------------------------------------
def test_no_file_reads_as_clear():
    assert freeze.read()["state"] == freeze.CLEAR
    assert freeze.is_frozen() is False
    assert freeze.render_line(freeze.read()) == ""


def test_unparseable_file_reads_as_clear_rather_than_blocking():
    # A freeze nobody can parse must not be able to stop a merge — fail open,
    # because the failure mode of the other choice is an unclearable block.
    freeze.FREEZE_FILE.parent.mkdir(parents=True, exist_ok=True)
    freeze.FREEZE_FILE.write_text("{not json")
    assert freeze.read()["state"] == freeze.CLEAR
    freeze.FREEZE_FILE.write_text(json.dumps({"reason": "no expiry here"}))
    assert freeze.read()["state"] == freeze.CLEAR
    freeze.FREEZE_FILE.write_text(json.dumps({"reason": "x", "until": "tuesday"}))
    assert freeze.read()["state"] == freeze.CLEAR


# --- set / show / clear -----------------------------------------------------
def test_set_writes_the_four_fields_and_reads_back_active():
    rec = freeze.set_freeze("release validation", "90m", set_by="release-validate")
    assert rec["state"] == freeze.ACTIVE
    on_disk = json.loads(freeze.FREEZE_FILE.read_text())
    assert set(on_disk) == {"reason", "set_at", "until", "set_by"}
    assert on_disk["reason"] == "release validation"
    assert on_disk["set_by"] == "release-validate"
    # Both stamps are UTC ISO-8601 and `until` is after `set_at`.
    assert on_disk["until"] > on_disk["set_at"]
    assert on_disk["until"].endswith("+00:00")
    assert freeze.is_frozen() is True


def test_the_line_every_surface_prints():
    rec = freeze.set_freeze("release validation", "2h")
    line = freeze.render_line(rec)
    assert line.startswith("FROZEN: release validation until ")
    assert line.endswith(rec["until"])


def test_clear_removes_the_file_and_reports_what_was_there():
    freeze.set_freeze("release validation", "2h")
    was = freeze.clear()
    assert was["state"] == freeze.ACTIVE
    assert was["reason"] == "release validation"
    assert not freeze.FREEZE_FILE.exists()
    assert freeze.read()["state"] == freeze.CLEAR
    # Clearing nothing is not an error.
    assert freeze.clear()["state"] == freeze.CLEAR


# --- expiry -----------------------------------------------------------------
def test_expiry_reads_as_expired_and_is_not_frozen():
    freeze.set_freeze("release validation", "30m")
    later = _at(1)
    rec = freeze.read(now=later)
    assert rec["state"] == freeze.EXPIRED
    assert freeze.is_frozen(now=later) is False
    # An expired freeze prints nothing — every consumer treats it as clear.
    assert freeze.render_line(rec) == ""
    # ...but the file is still there to be reported on, which is the point:
    # a forgotten set stays visible instead of vanishing.
    assert freeze.FREEZE_FILE.exists()


def test_an_already_past_expiry_is_refused():
    with pytest.raises(ValueError):
        freeze.set_freeze("release validation", "2020-01-01T00:00:00+00:00")


def test_a_reason_is_mandatory():
    with pytest.raises(ValueError):
        freeze.set_freeze("   ", "2h")


# --- --until parsing --------------------------------------------------------
@pytest.mark.parametrize("spec,hours", [("90m", 1.5), ("2h", 2), ("1d", 24),
                                        ("+45m", 0.75)])
def test_duration_shorthand(spec, hours):
    now = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=datetime.timezone.utc)
    assert freeze.parse_until(spec, now=now) == now + datetime.timedelta(hours=hours)


def test_iso_timestamps_with_and_without_a_zone():
    naive = freeze.parse_until("2026-09-03T19:30:00")
    zulu = freeze.parse_until("2026-09-03T19:30:00Z")
    offset = freeze.parse_until("2026-09-03T19:30:00+00:00")
    # A naive stamp is read as UTC, so all three name the same instant — the
    # same string must not mean two things on two machines.
    assert naive == zulu == offset


def test_an_empty_or_nonsense_until_is_an_error():
    with pytest.raises(ValueError):
        freeze.parse_until("")
    with pytest.raises(ValueError):
        freeze.parse_until("soon")


# --- the CLI contract -------------------------------------------------------
def test_show_exit_codes_are_the_shell_gate(capsys):
    assert freeze.main(["--show"]) == 0
    assert "not frozen" in capsys.readouterr().out

    # `--set` always exits 0: a driver running under `set -e` must not abort on
    # the freeze it just took out itself.
    assert freeze.main(["--set", "release validation", "--until", "90m"]) == 0
    capsys.readouterr()

    # A read while active exits 3 — one call, no JSON parse.
    assert freeze.main(["--show"]) == freeze.RC_FROZEN
    out = capsys.readouterr().out
    assert out.startswith("FROZEN: release validation until ")
    assert "set " in out and "by " in out

    assert freeze.main(["--clear"]) == 0
    assert "cleared: release validation" in capsys.readouterr().out
    assert freeze.main([]) == 0  # no args == --show
    assert "not frozen" in capsys.readouterr().out


def test_show_json_is_what_brain_reads(capsys):
    freeze.main(["--set", "release validation", "--until", "2h",
                 "--set-by", "pre-build"])
    capsys.readouterr()
    assert freeze.main(["--show", "--json"]) == freeze.RC_FROZEN
    rec = json.loads(capsys.readouterr().out)
    assert rec["state"] == "active"
    assert rec["reason"] == "release validation"
    assert rec["set_by"] == "pre-build"
    assert rec["until"] and rec["set_at"]


def test_expired_show_says_expired_and_exits_clear(capsys):
    # Set a freeze that has already run out by writing the record directly —
    # `set_freeze` refuses a past expiry, which is the behaviour tested above.
    freeze.FREEZE_FILE.parent.mkdir(parents=True, exist_ok=True)
    freeze.FREEZE_FILE.write_text(json.dumps({
        "reason": "release validation", "set_at": "2026-09-03T10:00:00+00:00",
        "until": "2026-09-03T11:00:00+00:00", "set_by": "pre-build"}))
    now = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=datetime.timezone.utc)
    assert freeze.read(now=now)["state"] == freeze.EXPIRED
    assert freeze.main(["--show"]) == 0
    assert "expired" in capsys.readouterr().out


def test_set_and_clear_together_is_a_usage_error(capsys):
    assert freeze.main(["--set", "x", "--until", "2h", "--clear"]) == 2
    assert "opposites" in capsys.readouterr().err


def test_set_without_until_is_a_usage_error_not_a_permanent_freeze(capsys):
    assert freeze.main(["--set", "release validation"]) == 2
    assert "--until is required" in capsys.readouterr().err
    assert not freeze.FREEZE_FILE.exists()
