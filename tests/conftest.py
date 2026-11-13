"""tests/conftest.py — sandbox HEART_STATE_DIR for the whole suite.

heart/checks/* resolve HEART_STATE_DIR at import time, so this assignment must
happen before any test module imports them; conftest import is pytest's
earliest hook, which is why this is a module-level statement and not a fixture.
Belt-and-braces on top of the checks' run()/main() split (state writes live
only in main()): no test, present or future, can reach the developer's live
~/.pyauto-heart — that state is the input to the release gate.
"""

from __future__ import annotations

import os
import tempfile

os.environ["HEART_STATE_DIR"] = tempfile.mkdtemp(prefix="pyauto-heart-test-state-")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_stage3_quarantine(monkeypatch, tmp_path_factory):
    """Readiness reads the repo-tracked Stage 3 quarantine by default. A real
    entry filed on main must not turn every verdict test YELLOW, so the suite
    reads an absent file unless a test passes entries (PyAutoHeart#344)."""
    from heart import quarantine
    monkeypatch.setattr(quarantine, "QUARANTINE_PATH",
                        tmp_path_factory.getbasetemp() / "no_stage3_quarantine.yaml")
