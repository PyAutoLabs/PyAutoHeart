"""pulse/readiness.py — composite release-readiness verdict.

Rolls the continuous-health signals already in ``state.json`` into a single
green / yellow / red verdict answering "is it safe to release?". This is
**advisory** — Pulse emits the verdict; PyAutoBuild keeps its own authoritative
release gates and does not depend on this.

The verdict uses STRICT release gates:

- **RED** (a real blocker) if any of the 5 libraries has failing CI, is off
  ``main``, has uncommitted source changes, or is behind origin; or the latest
  Build test run is ``ready == false``; or any workspace is pinned AHEAD of its
  installed library.
- **YELLOW** (caution) for soft signals: script-timing regressions, stale open
  PRs, stale parked scripts, a workspace pinned BEHIND, and — crucially — any
  *unknown* (missing test-run report, a library absent from the snapshot). An
  unknown is never silently treated as green and never escalated to red.
- **GREEN** otherwise.

Red dominates yellow structurally: reasons are collected into separate lists
and ``verdict = red if red_reasons else yellow if yellow_reasons else green``.
A ``score`` (0–100, weighted penalties) is advisory/sortable only — the colour,
not the number, is the gate. ``compute`` is a pure function of the snapshot for
easy testing and never raises on partial/malformed data.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from pulse import state
from pulse.pulse_color import (
    c_bold, c_fail, c_info, c_meta, c_ok, c_warn,
    glyph_fail, glyph_ok, glyph_warn,
)

PULSE_HOME = Path(__file__).resolve().parents[1]
CONFIG_PATH = PULSE_HOME / "config" / "repos.yaml"
RELEASE_READY_FILE = state.PULSE_STATE_DIR / "release_ready.json"

DEFAULT_LIBRARIES = ("PyAutoConf", "PyAutoFit", "PyAutoArray", "PyAutoGalaxy", "PyAutoLens")

# gate key -> (penalty per occurrence, cap). Score = 100 - sum(min(n*w, cap)).
# Advisory only; colour is decided by reason presence, not by score.
_WEIGHTS: dict[str, tuple[int, int]] = {
    "lib_ci": (30, 60),
    "lib_branch": (15, 30),
    "lib_dirty": (15, 30),
    "lib_behind": (20, 40),
    "test_not_ready": (40, 40),
    "skew_ahead": (25, 50),
    "lib_unknown": (10, 30),
    "test_unknown": (10, 10),
    "timing_red": (15, 15),
    "timing_yellow": (8, 8),
    "open_pr": (5, 15),
    "parked": (5, 15),
    "skew_behind": (8, 24),
}


def load_library_names(config_path: Path | str = CONFIG_PATH) -> list[str]:
    """Return repos.libraries[].name, or DEFAULT_LIBRARIES if unavailable."""
    try:
        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        libs = [r["name"] for r in cfg.get("repos", {}).get("libraries", [])]
        return libs or list(DEFAULT_LIBRARIES)
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return list(DEFAULT_LIBRARIES)


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def compute(snapshot: dict | None, libraries: Sequence[str] | None = None) -> dict[str, Any]:
    """Pure verdict function. Never raises on missing/partial data."""
    snapshot = snapshot or {}
    libs = list(libraries) if libraries is not None else load_library_names()
    repos = snapshot.get("repos", {}) or {}

    red: list[str] = []
    yellow: list[str] = []
    counts: dict[str, int] = {}

    def hit(key: str, n: int = 1) -> None:
        counts[key] = counts.get(key, 0) + n

    # --- library gates (RED) ---
    for lib in libs:
        body = repos.get(lib)
        if not isinstance(body, dict) or not body:
            yellow.append(f"{lib}: status unknown")
            hit("lib_unknown")
            continue
        ci = body.get("ci_status", {}) or {}
        conclusion = ci.get("conclusion")
        if conclusion not in (None, "", "success"):
            red.append(f"{lib}: CI {conclusion}")
            hit("lib_ci")
        rs = body.get("repo_state", {}) or {}
        branch = rs.get("branch")
        if branch and branch != "main":
            red.append(f"{lib}: on branch {branch} (not main)")
            hit("lib_branch")
        dirty_real = _as_int(rs.get("dirty_real", rs.get("dirty_files", 0)))
        if dirty_real > 0:
            red.append(f"{lib}: {dirty_real} uncommitted source change(s)")
            hit("lib_dirty")
        behind = _as_int(rs.get("behind", 0))
        if behind > 0:
            red.append(f"{lib}: {behind} commit(s) behind origin")
            hit("lib_behind")

    # --- test-run gate (RED if false, YELLOW if unknown) ---
    test_run = snapshot.get("test_run")
    if isinstance(test_run, dict) and "ready" in test_run:
        ready = test_run.get("ready")
        if ready is False:
            red.append(f"test run not ready ({test_run.get('run_label', '?')})")
            hit("test_not_ready")
        elif ready is not True:
            yellow.append("test run status unknown")
            hit("test_unknown")
        # parked staleness (YELLOW)
        parked = _as_int(test_run.get("parked_stale_count", 0))
        if parked > 0:
            yellow.append(f"{parked} stale parked script(s)")
            hit("parked")
    else:
        yellow.append("test run status unknown (no report.json)")
        hit("test_unknown")

    # --- version skew (RED ahead / YELLOW behind) ---
    skew = snapshot.get("version_skew")
    if isinstance(skew, dict):
        for w in skew.get("workspaces") or []:
            if not isinstance(w, dict):
                continue
            status = str(w.get("status", "")).upper()
            if status == "AHEAD":
                red.append(f"{w.get('workspace')}: pinned {w.get('pinned')} AHEAD of installed {w.get('installed')}")
                hit("skew_ahead")
            elif status == "BEHIND":
                yellow.append(f"{w.get('workspace')}: pinned BEHIND installed {w.get('installed')}")
                hit("skew_behind")

    # --- script timing (YELLOW) ---
    timing = snapshot.get("script_timing", {}) or {}
    if _as_int(timing.get("red_count", 0)) > 0:
        yellow.append(f"{_as_int(timing.get('red_count'))} script timing regression(s)")
        hit("timing_red")
    elif _as_int(timing.get("yellow_count", 0)) > 0:
        yellow.append(f"{_as_int(timing.get('yellow_count'))} slow script(s)")
        hit("timing_yellow")

    # --- open PRs across all repos (YELLOW) ---
    for name, body in sorted(repos.items()):
        if not isinstance(body, dict):
            continue
        pr = body.get("open_prs", {}) or {}
        if _as_int(pr.get("open_count", 0)) > 0 and _as_int(pr.get("max_age_days", 0)) >= 7:
            yellow.append(f"{name}: open PR {_as_int(pr.get('max_age_days'))}d old")
            hit("open_pr")

    # --- score ---
    score = 100
    for key, n in counts.items():
        w, cap = _WEIGHTS.get(key, (0, 0))
        score -= min(n * w, cap)
    score = max(0, min(100, score))

    verdict = "red" if red else ("yellow" if yellow else "green")
    return {
        "verdict": verdict,
        "score": score,
        "reasons": red + yellow,
        "red_reasons": red,
        "yellow_reasons": yellow,
        "ts": snapshot.get("ts") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def run() -> dict[str, Any]:
    """Load state, compute, atomic-write release_ready.json, return verdict."""
    verdict = compute(state.load() or {})
    state.atomic_write_json(RELEASE_READY_FILE, verdict)
    return verdict


def load_verdict() -> dict[str, Any]:
    """Return the persisted verdict, falling back to a live compute."""
    if RELEASE_READY_FILE.is_file():
        try:
            return json.loads(RELEASE_READY_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return compute(state.load() or {})


def render_block(verdict: dict[str, Any], quiet: bool = False) -> list[str]:
    v = verdict.get("verdict", "green")
    score = verdict.get("score", 0)
    if v == "red":
        glyph, word = glyph_fail(), c_fail("RED")
    elif v == "yellow":
        glyph, word = glyph_warn(), c_warn("YELLOW")
    else:
        glyph, word = glyph_ok(), c_ok("GREEN")
    lines = [f"{c_info('RELEASE READINESS')}  {glyph} {word}  {c_meta(f'score {score}')}"]

    reds = verdict.get("red_reasons", [])
    yellows = verdict.get("yellow_reasons", [])
    limit = 1 if quiet else 6
    shown = 0
    for r in reds:
        lines.append("  " + c_fail(f"✗ {r}"))
        shown += 1
        if shown >= limit:
            break
    if shown < limit:
        for y in yellows[: limit - shown]:
            lines.append("  " + c_warn(f"! {y}"))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pyauto-pulse readiness")
    ap.add_argument("--json", action="store_true", help="print the raw verdict JSON")
    ap.add_argument("--quiet", action="store_true", help="verdict line + top reason only")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    ns = ap.parse_args(argv)
    if ns.no_color:
        os.environ["NO_COLOR"] = "1"

    verdict = load_verdict()
    if ns.json:
        json.dump(verdict, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    for line in render_block(verdict, quiet=ns.quiet):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
