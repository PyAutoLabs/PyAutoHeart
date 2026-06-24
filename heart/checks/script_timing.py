"""heart/checks/script_timing.py — read autobuild run_all results, track
rolling per-script duration baselines, classify regressions.

Inputs:
- PyAutoBuild/test_results/latest/ (symlink to most recent run dir)
- Per-script JSON: <workspace>__<dir>__script.json containing
  `results` list with `file`, `duration_seconds`, `status`.

State (per Heart instance):
- ~/.pyauto-heart/timings/<workspace>__<dir>__<file>.json
  containing a rolling window of recent durations.

Output:
- ~/.pyauto-heart/script_timing.json with the latest regression summary.

Classification:
- green: ratio <= yellow_factor (default 1.5)
- yellow: yellow_factor < ratio <= red_factor (default 3.0)
- red:    ratio > red_factor

Where ratio = latest_duration / median(rolling_window).
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import yaml

HEART_STATE_DIR = Path(
    os.environ.get("HEART_STATE_DIR")
    or os.environ.get("PULSE_STATE_DIR")
    or Path.home() / ".pyauto-heart"
)
HEART_TIMINGS_DIR = HEART_STATE_DIR / "timings"
HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"
PYAUTO_ROOT = Path(__file__).resolve().parents[3] if Path(__file__).resolve().parents[3].name == "PyAutoLabs" else Path.home() / "Code" / "PyAutoLabs"
TEST_RESULTS_LATEST = PYAUTO_ROOT / "PyAutoBuild" / "test_results" / "latest"


def load_thresholds() -> tuple[float, float, int]:
    """Return (yellow_factor, red_factor, baseline_window) from config."""
    if not CONFIG_PATH.is_file():
        return 1.5, 3.0, 7
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    t = cfg.get("thresholds", {}).get("script_timing", {})
    return (
        float(t.get("yellow_factor", 1.5)),
        float(t.get("red_factor", 3.0)),
        int(t.get("baseline_window", 7)),
    )


def slug_for(workspace: str, directory: str, file_path: str) -> str:
    """Stable filename slug for a script's timing history.

    Uses the FULL relative file path so scripts in nested subdirs
    (e.g. ``imaging/modeling.py`` vs ``imaging/features/.../modeling.py``)
    do not collide on a shared leaf name.
    """
    # The autobuild run_all writes ``file`` as an absolute path. Strip
    # everything up to and including "scripts/" so the slug is workspace-
    # relative.
    f = Path(file_path)
    parts = f.parts
    if "scripts" in parts:
        idx = parts.index("scripts")
        relative = "__".join(parts[idx:])
    else:
        relative = "__".join(parts)
    relative = relative.replace(".py", "")
    return f"{workspace}__{relative}.json"


def update_history(slug: str, duration: float, window: int) -> list[float]:
    """Append duration to slug's rolling history, return the new list."""
    HEART_TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    history_path = HEART_TIMINGS_DIR / slug
    if history_path.is_file():
        try:
            history = json.loads(history_path.read_text())
        except json.JSONDecodeError:
            history = []
    else:
        history = []

    history.append(duration)
    # Keep at most `window` most recent.
    history = history[-window:]
    history_path.write_text(json.dumps(history))
    return history


def classify(ratio: float, yellow: float, red: float) -> str:
    if ratio > red:
        return "red"
    if ratio > yellow:
        return "yellow"
    return "green"


def scan_latest_results(results_dir: Path) -> list[dict[str, Any]]:
    """Walk results_dir for per-script JSONs and yield individual script
    entries with workspace/directory/file/duration/status."""
    entries: list[dict[str, Any]] = []
    if not results_dir.exists():
        return entries

    for json_path in sorted(results_dir.glob("*__script.json")):
        # Filename shape: <project>__scripts__<directory>__script.json
        # We just read the file to get the project + directory + per-script results.
        try:
            data = json.loads(json_path.read_text())
        except json.JSONDecodeError:
            continue
        project = data.get("project", "")
        directory = data.get("directory", "")
        for r in data.get("results", []):
            if r.get("status") != "passed":
                continue
            duration = r.get("duration_seconds")
            file_path = r.get("file", "")
            if duration is None or not file_path:
                continue
            entries.append({
                "project": project,
                "directory": directory,
                "file": file_path,
                "duration": float(duration),
            })
    return entries


def run(results_dir: Path | None = None) -> dict[str, Any]:
    """Update rolling timings from results_dir; return classification summary."""
    results_dir = results_dir or TEST_RESULTS_LATEST
    yellow_factor, red_factor, window = load_thresholds()

    findings: dict[str, list[dict[str, Any]]] = {"red": [], "yellow": [], "green": []}
    total = 0
    new_scripts = 0

    for entry in scan_latest_results(results_dir):
        slug = slug_for(entry["project"], entry["directory"], entry["file"])
        history = update_history(slug, entry["duration"], window)
        total += 1
        if len(history) <= 1:
            # First observation, no baseline yet.
            new_scripts += 1
            continue
        # Compare latest to median of the prior history (exclude current).
        prior = history[:-1]
        baseline = statistics.median(prior)
        if baseline <= 0:
            continue
        ratio = entry["duration"] / baseline
        category = classify(ratio, yellow_factor, red_factor)
        record = {
            "project": entry["project"],
            "file": entry["file"],
            "latest_seconds": entry["duration"],
            "baseline_seconds": baseline,
            "ratio": round(ratio, 2),
            "samples": len(prior),
        }
        findings[category].append(record)

    summary = {
        "results_dir": str(results_dir),
        "total_scripts": total,
        "new_scripts_no_baseline": new_scripts,
        "red_count": len(findings["red"]),
        "yellow_count": len(findings["yellow"]),
        "green_count": len(findings["green"]),
        "red": sorted(findings["red"], key=lambda x: -x["ratio"]),
        "yellow": sorted(findings["yellow"], key=lambda x: -x["ratio"]),
    }

    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(HEART_STATE_DIR / "script_timing.json", summary)
    return summary


def main(argv: list[str]) -> int:
    results_dir = Path(argv[1]) if len(argv) > 1 else TEST_RESULTS_LATEST
    summary = run(results_dir)

    # Coloured one-line summary to stdout (used by tick.sh log).
    from heart_color import c_ok, c_warn, c_fail, c_info, c_meta, glyph_ok, glyph_warn, glyph_fail

    if summary["red_count"]:
        glyph = glyph_fail()
        label = c_fail(f"{summary['red_count']} red") + " " + c_warn(f"{summary['yellow_count']} yellow")
    elif summary["yellow_count"]:
        glyph = glyph_warn()
        label = c_warn(f"{summary['yellow_count']} yellow")
    else:
        glyph = glyph_ok()
        label = c_ok(f"{summary['green_count']} scripts within baseline")
    extra = c_meta(f" ({summary['new_scripts_no_baseline']} new, no baseline)")
    print(f"{glyph} {c_info('script_timing')} {label}{extra}")
    return 0


if __name__ == "__main__":
    # Allow running standalone. We import the color helpers at runtime
    # to avoid a hard dep when called as a library.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main(sys.argv))
