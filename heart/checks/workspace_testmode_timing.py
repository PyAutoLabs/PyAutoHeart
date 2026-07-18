"""heart/checks/workspace_testmode_timing.py — time curated workspace scripts run
in integration TEST MODE, track a rolling baseline, flag regressions.

Distinct from ``script_timing``, which reads PyAutoHands ``run_all`` durations
(the release/CI path). This leg runs a curated set of workspace ``start_here``
scripts with ``PYAUTO_TEST_MODE=2`` + ``PYAUTO_WORKSPACE_SMALL_DATASETS=1`` — the
developer-loop mode named in the original hygiene prompt — and times them.

OFF-TICK by design: running scripts costs seconds–minutes, which does not fit
the <30s watch-loop budget (``docs/internals.md`` rule 3). Run on a slower
cadence (daily cron / on demand), NOT from ``heart/tick.sh``:

    python -m heart.checks.workspace_testmode_timing

Scripts run in a SUBPROCESS (rule 4 — Heart never imports the science stack; the
runner is injected so the stdlib-only tests use fixtures). Advisory: surfaced on
the board but never a release gate.

State: ~/.pyauto-heart/timings-ws-testmode/<slug>.json — rolling window per script.
Output: ~/.pyauto-heart/workspace_testmode_timing.json — regression summary.
Classification mirrors script_timing (ratio = latest / median(prior window)).
"""

from __future__ import annotations

import json
import os
import re
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
HEART_WS_TIMINGS_DIR = HEART_STATE_DIR / "timings-ws-testmode"
HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"

# Curated dev-loop scripts (the smoke-subset spirit — do NOT grow this to make
# the signal feel stronger). (workspace_repo, workspace-relative script path).
DEFAULT_SCRIPTS = [
    ("autolens_workspace", "scripts/imaging/start_here.py"),
    ("autogalaxy_workspace", "scripts/imaging/start_here.py"),
    ("autofit_workspace", "scripts/overview/overview_1_the_basics.py"),
]

# A runner maps (workspace, rel_path) to the script's wall-clock in seconds, or
# None when it could not be timed (missing / errored / timed out). Injected in tests.
Runner = Callable[[str, str], "float | None"]


def load_thresholds() -> tuple[float, float, int]:
    yellow, red, window = 1.5, 3.0, 7
    if CONFIG_PATH.is_file():
        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        t = cfg.get("thresholds", {}).get("workspace_testmode_timing", {})
        yellow = float(t.get("yellow_factor", yellow))
        red = float(t.get("red_factor", red))
        window = int(t.get("baseline_window", window))
    return yellow, red, window


def default_runner(root: Path, python: str, timeout: float) -> Runner:
    """Time ``python <script>`` in the workspace, in TEST_MODE, in a subprocess."""
    env = {
        **os.environ,
        "PYAUTO_TEST_MODE": "2",
        "PYAUTO_WORKSPACE_SMALL_DATASETS": "1",
        "NUMBA_CACHE_DIR": os.environ.get("NUMBA_CACHE_DIR", "/tmp/numba_cache"),
        "MPLCONFIGDIR": os.environ.get("MPLCONFIGDIR", "/tmp/matplotlib"),
        "PYTHONUNBUFFERED": "1",
    }

    def run_script(workspace: str, rel: str) -> float | None:
        workspace_dir = root / workspace
        script = workspace_dir / rel
        if not script.is_file():
            return None
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                [python, str(script)], cwd=str(workspace_dir),
                capture_output=True, timeout=timeout, env=env,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if proc.returncode != 0:
            return None
        return time.perf_counter() - start

    return run_script


def _slug(workspace: str, rel: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{workspace}__{rel}").strip("_")
    return f"{safe}.json"


def update_history(slug: str, duration: float, window: int) -> list[float]:
    HEART_WS_TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    history_path = HEART_WS_TIMINGS_DIR / slug
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


def run(runner: Runner | None = None, scripts: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    yellow_factor, red_factor, window = load_thresholds()
    scripts = scripts if scripts is not None else DEFAULT_SCRIPTS
    python = os.environ.get("HYGIENE_PYTHON", "python3")
    timeout = float(os.environ.get("HEART_WS_SCRIPT_TIMEOUT", "300"))
    root = Path(os.environ.get("PYAUTO_ROOT", str(Path.home() / "Code" / "PyAutoLabs")))
    runner = runner or default_runner(root, python, timeout)

    findings: dict[str, list[dict[str, Any]]] = {"red": [], "yellow": [], "green": []}
    measured = 0
    unavailable: list[str] = []
    new_scripts = 0

    for workspace, rel in scripts:
        sid = f"{workspace}/{rel}"
        duration = runner(workspace, rel)
        if duration is None:
            unavailable.append(sid)
            continue
        measured += 1
        history = update_history(_slug(workspace, rel), duration, window)
        if len(history) <= 1:
            new_scripts += 1
            continue
        baseline = statistics.median(history[:-1])
        if baseline <= 0:
            continue
        ratio = duration / baseline
        findings[classify(ratio, yellow_factor, red_factor)].append({
            "script": sid,
            "latest_seconds": round(duration, 3),
            "baseline_seconds": round(baseline, 3),
            "ratio": round(ratio, 2),
            "samples": len(history) - 1,
        })

    summary = {
        "python": python,
        "mode": "PYAUTO_TEST_MODE=2 PYAUTO_WORKSPACE_SMALL_DATASETS=1",
        "scripts_measured": measured,
        "scripts_unavailable": unavailable,
        "new_scripts_no_baseline": new_scripts,
        "red_count": len(findings["red"]),
        "yellow_count": len(findings["yellow"]),
        "green_count": len(findings["green"]),
        "red": sorted(findings["red"], key=lambda x: -x["ratio"]),
        "yellow": sorted(findings["yellow"], key=lambda x: -x["ratio"]),
    }

    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(HEART_STATE_DIR / "workspace_testmode_timing.json", summary)
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
    elif not summary["scripts_measured"]:
        glyph = glyph_warn()
        label = c_warn("no script runnable (set HYGIENE_PYTHON / PYAUTO_ROOT)")
    else:
        glyph = glyph_ok()
        label = c_ok(f"{summary['green_count']} scripts within baseline")
    extra = c_meta(f" ({summary['new_scripts_no_baseline']} new, no baseline)")
    print(f"{glyph} {c_info('workspace_testmode_timing')} {label}{extra}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main(sys.argv))
