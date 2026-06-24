"""heart/heart_color.py — ANSI color helpers (Python side).

Mirrors the bash `heart/_color.sh` helpers. Honours NO_COLOR,
HEART_NO_COLOR, HEART_FORCE_COLOR env vars (plus legacy PULSE_* aliases), and detects whether stdout
is a tty. Same colour convention as the plan:
  green  → passing / clean / nominal
  yellow → warning / stale / mild drift
  red    → failing / actionable now
  cyan   → informational
  dim    → secondary
"""

from __future__ import annotations

import os
import sys


def colors_enabled() -> bool:
    if os.environ.get("HEART_FORCE_COLOR") or os.environ.get("PULSE_FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR") or os.environ.get("HEART_NO_COLOR") or os.environ.get("PULSE_NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _wrap(code: str, text: str) -> str:
    if colors_enabled():
        return f"\033[{code}m{text}\033[0m"
    return str(text)


def c_dim(text: str) -> str:     return _wrap("2", text)
def c_bold(text: str) -> str:    return _wrap("1", text)
def c_green(text: str) -> str:   return _wrap("32", text)
def c_yellow(text: str) -> str:  return _wrap("33", text)
def c_red(text: str) -> str:     return _wrap("31", text)
def c_blue(text: str) -> str:    return _wrap("34", text)
def c_cyan(text: str) -> str:    return _wrap("36", text)
def c_magenta(text: str) -> str: return _wrap("35", text)


# Semantic shortcuts.
def c_ok(text: str) -> str:   return c_green(text)
def c_warn(text: str) -> str: return c_yellow(text)
def c_fail(text: str) -> str: return c_red(text)
def c_info(text: str) -> str: return c_cyan(text)
def c_meta(text: str) -> str: return c_dim(text)


# Status glyphs.
def glyph_ok() -> str:   return c_ok("✓")
def glyph_warn() -> str: return c_warn("!")
def glyph_fail() -> str: return c_fail("✗")
def glyph_info() -> str: return c_info("•")
