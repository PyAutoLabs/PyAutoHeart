"""tests/test_color.py — heart.heart_color helpers honour NO_COLOR,
HEART_NO_COLOR, HEART_FORCE_COLOR, and tty detection.
"""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

from heart import heart_color as pc


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Each test starts with no HEART_* / PULSE_* / NO_COLOR env vars."""
    for v in ("NO_COLOR", "HEART_NO_COLOR", "HEART_FORCE_COLOR", "PULSE_NO_COLOR", "PULSE_FORCE_COLOR"):
        monkeypatch.delenv(v, raising=False)
    yield


def _strip(text: str) -> str:
    """Strip ANSI escape sequences."""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


def test_force_color_overrides_tty_check(monkeypatch):
    monkeypatch.setenv("HEART_FORCE_COLOR", "1")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert pc.c_green("ok").startswith("\033[")


def test_no_color_strips_codes(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert pc.c_green("ok") == "ok"
    assert pc.c_red("fail") == "fail"
    assert pc.glyph_ok() == "✓"


def test_heart_no_color_alias(monkeypatch):
    monkeypatch.setenv("HEART_NO_COLOR", "1")
    assert pc.c_red("x") == "x"


def test_pulse_no_color_alias(monkeypatch):
    monkeypatch.setenv("PULSE_NO_COLOR", "1")
    assert pc.c_red("x") == "x"


def test_non_tty_disables_color(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert pc.c_green("ok") == "ok"


def test_tty_enables_color(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert "\033[32m" in pc.c_green("ok")


def test_semantic_shortcuts_map_to_correct_codes(monkeypatch):
    monkeypatch.setenv("HEART_FORCE_COLOR", "1")
    assert pc.c_ok("x") == pc.c_green("x")
    assert pc.c_warn("x") == pc.c_yellow("x")
    assert pc.c_fail("x") == pc.c_red("x")
    assert pc.c_info("x") == pc.c_cyan("x")
    assert pc.c_meta("x") == pc.c_dim("x")
