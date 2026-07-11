"""heart/checks/import_time.py — measure PyAuto library import cost in a
subprocess, track rolling per-package baselines, classify regressions.

OFF-TICK by design. Importing the science stack costs several seconds
(``import autolens`` ~3.6s warm, more cold), which does not fit the <30s
watch-loop tick budget (``docs/internals.md`` rule 3). Run this on a slower
cadence — a daily cron or on demand — and do **not** wire it into
``heart/tick.sh``:

    python -m heart.checks.import_time
    HYGIENE_PYTHON=~/venv/PyAuto/bin/python python -m heart.checks.import_time

The measurement runs in a SUBPROCESS (rule 4): Heart's own process never imports
the science/JAX stack, and the tests inject a fake measurer so the stdlib-only
suite never touches it either. Advisory only — surfaced on the board but never a
release gate (import time is not release-blocking).

Inputs:
- spawns ``<python> -c "import <pkg>"`` per configured package (``HYGIENE_PYTHON``,
  default ``python3`` — point it at the PyAuto venv to time the science libs).

State (per Heart instance):
- ~/.pyauto-heart/timings-import/<pkg>.json — a rolling window of recent durations.

Output:
- ~/.pyauto-heart/import_time.json — the latest regression summary.

Classification (mirrors script_timing):
- green: ratio <= yellow_factor (default 1.5)
- yellow: yellow_factor < ratio <= red_factor (default 3.0)
- red:    ratio > red_factor
Where ratio = latest_duration / median(rolling_window).
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import yaml

HEART_STATE_DIR = Path(
    os.environ.get("HEART_STATE_DIR")
    or Path.home() / ".pyauto-heart"
)
HEART_IMPORT_TIMINGS_DIR = HEART_STATE_DIR / "timings-import"
HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"

# autolens pulls the whole stack; the others give a per-package breakdown.
DEFAULT_PACKAGES = ["autoconf", "autofit", "autoarray", "autogalaxy", "autolens"]

# A measurer maps a package import-name to its import duration in seconds, or
# None when it cannot be imported (missing / errored / timed out). Injected in
# tests so the stdlib-only suite never imports the science stack.
Measurer = Callable[[str], "float | None"]


def load_thresholds() -> tuple[float, float, int]:
    """Return (yellow_factor, red_factor, baseline_window) from config."""
    if not CONFIG_PATH.is_file():
        return 1.5, 3.0, 7
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    t = cfg.get("thresholds", {}).get("import_time", {})
    return (
        float(t.get("yellow_factor", 1.5)),
        float(t.get("red_factor", 3.0)),
        int(t.get("baseline_window", 7)),
    )


def default_measurer(python: str, timeout: float) -> Measurer:
    """A measurer that times ``<python> -c "import <pkg>"`` in a subprocess.

    Heart's own process never imports the package — it shells out — so the
    daemon stays free of the science/JAX stack (internals rule 4).
    """
    env = {
        **os.environ,
        "NUMBA_CACHE_DIR": os.environ.get("NUMBA_CACHE_DIR", "/tmp/numba_cache"),
        "MPLCONFIGDIR": os.environ.get("MPLCONFIGDIR", "/tmp/matplotlib"),
    }

    def measure(pkg: str) -> float | None:
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                [python, "-c", f"import {pkg}"],
                capture_output=True, timeout=timeout, env=env,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if proc.returncode != 0:
            return None
        return time.perf_counter() - start

    return measure


def update_history(pkg: str, duration: float, window: int) -> list[float]:
    """Append duration to pkg's rolling history, return the new list."""
    HEART_IMPORT_TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    history_path = HEART_IMPORT_TIMINGS_DIR / f"{pkg}.json"
    if history_path.is_file():
        try:
            history = json.loads(history_path.read_text())
        except json.JSONDecodeError:
            history = []
    else:
        history = []
    history.append(duration)
    history = history[-window:]
    history_path.write_text(json.dumps(history))
    return history


def classify(ratio: float, yellow: float, red: float) -> str:
    if ratio > red:
        return "red"
    if ratio > yellow:
        return "yellow"
    return "green"


def run(
    measurer: Measurer | None = None,
    packages: list[str] | None = None,
) -> dict[str, Any]:
    """Measure each package's import cost, update rolling baselines, classify."""
    yellow_factor, red_factor, window = load_thresholds()
    packages = packages if packages is not None else DEFAULT_PACKAGES
    python = os.environ.get("HYGIENE_PYTHON", "python3")
    timeout = float(os.environ.get("HEART_IMPORT_TIMEOUT", "90"))
    measurer = measurer or default_measurer(python, timeout)

    findings: dict[str, list[dict[str, Any]]] = {"red": [], "yellow": [], "green": []}
    unavailable: list[str] = []
    measured = 0
    new_packages = 0

    for pkg in packages:
        duration = measurer(pkg)
        if duration is None:
            unavailable.append(pkg)
            continue
        measured += 1
        history = update_history(pkg, duration, window)
        if len(history) <= 1:
            new_packages += 1
            continue
        prior = history[:-1]
        baseline = statistics.median(prior)
        if baseline <= 0:
            continue
        ratio = duration / baseline
        category = classify(ratio, yellow_factor, red_factor)
        findings[category].append({
            "package": pkg,
            "latest_seconds": round(duration, 3),
            "baseline_seconds": round(baseline, 3),
            "ratio": round(ratio, 2),
            "samples": len(prior),
        })

    summary = {
        "python": python,
        "packages_measured": measured,
        "packages_unavailable": unavailable,
        "new_packages_no_baseline": new_packages,
        "red_count": len(findings["red"]),
        "yellow_count": len(findings["yellow"]),
        "green_count": len(findings["green"]),
        "red": sorted(findings["red"], key=lambda x: -x["ratio"]),
        "yellow": sorted(findings["yellow"], key=lambda x: -x["ratio"]),
    }

    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(HEART_STATE_DIR / "import_time.json", summary)
    return summary


def main(argv: list[str]) -> int:
    summary = run()

    from heart_color import c_ok, c_warn, c_fail, c_info, c_meta, glyph_ok, glyph_warn, glyph_fail

    if summary["red_count"]:
        glyph = glyph_fail()
        label = c_fail(f"{summary['red_count']} red") + " " + c_warn(f"{summary['yellow_count']} yellow")
    elif summary["yellow_count"]:
        glyph = glyph_warn()
        label = c_warn(f"{summary['yellow_count']} slower than baseline")
    elif not summary["packages_measured"]:
        glyph = glyph_warn()
        label = c_warn("no libraries importable (set HYGIENE_PYTHON to the PyAuto venv)")
    else:
        glyph = glyph_ok()
        label = c_ok(f"{summary['green_count']} imports within baseline")
    extra = c_meta(f" ({summary['new_packages_no_baseline']} new, no baseline)")
    print(f"{glyph} {c_info('import_time')} {label}{extra}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main(sys.argv))
