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
