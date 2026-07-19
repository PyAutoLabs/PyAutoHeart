"""heart/checks/unit_test_timing.py — track library unit-test durations, flag
slow-test regressions.

OFF-TICK by design. Running the library suites costs minutes (test_autofit is
~1500 tests), which does not fit the <30s watch-loop budget (``docs/internals.md``
rule 3). Run on a slower cadence — a daily/weekly cron or on demand — and do
**not** wire it into ``heart/tick.sh``:

    python -m heart.checks.unit_test_timing
    HYGIENE_PYTHON=~/venv/PyAuto/bin/python python -m heart.checks.unit_test_timing

The suites run in a SUBPROCESS (``pytest --durations``) — Heart's own process
never imports the test/science stack (rule 4) — and the runner is injected so
the stdlib-only tests feed fixtures and never run pytest-in-pytest. Advisory:
surfaced on the board but never a release gate (test speed is not release-blocking).

State: ~/.pyauto-heart/timings-unit/<slug>.json — rolling window per tracked test.
Output: ~/.pyauto-heart/unit_test_timing.json — the latest regression summary.
Classification (mirrors script_timing): ratio = latest / median(prior window);
green <= yellow_factor (1.5), yellow <= red_factor (3.0), else red.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

HEART_STATE_DIR = Path(
    os.environ.get("HEART_STATE_DIR")
    or Path.home() / ".pyauto-heart"
)
HEART_UNIT_TIMINGS_DIR = HEART_STATE_DIR / "timings-unit"
HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"

# Each library's suite is discovered via its own pytest config (testpaths), so we
# just invoke pytest from the repo root. Import-name maps to repo dir name.
DEFAULT_REPOS = ["PyAutoNerves", "PyAutoFit", "PyAutoArray", "PyAutoGalaxy", "PyAutoLens"]

# A runner maps a repo checkout dir to {test_nodeid: call_seconds} for the
# slowest tests, or None when the suite could not be run. Injected in tests.
Runner = Callable[[Path], "dict[str, float] | None"]

# pytest's "slowest durations" report lines: "1.23s call path::test_x"
_DURATION_RE = re.compile(r"^\s*([0-9.]+)s\s+call\s+(\S+)\s*$")


def load_thresholds() -> tuple[float, float, int, int]:
    """Return (yellow_factor, red_factor, baseline_window, top_n) from config."""
    yellow, red, window, top_n = 1.5, 3.0, 7, 15
    if CONFIG_PATH.is_file():
        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        t = cfg.get("thresholds", {}).get("unit_test_timing", {})
        yellow = float(t.get("yellow_factor", yellow))
        red = float(t.get("red_factor", red))
        window = int(t.get("baseline_window", window))
        top_n = int(t.get("top_n", top_n))
    return yellow, red, window, top_n


def parse_durations(stdout: str) -> dict[str, float]:
    """Extract {nodeid: call_seconds} from a pytest --durations report."""
    out: dict[str, float] = {}
    for line in stdout.splitlines():
        m = _DURATION_RE.match(line)
        if m:
            out[m.group(2)] = float(m.group(1))
    return out


def default_runner(python: str, timeout: float, top_n: int) -> Runner:
    """Run ``pytest --durations`` per repo in a subprocess; parse the slowest."""
    env = {
        **os.environ,
        "NUMBA_CACHE_DIR": os.environ.get("NUMBA_CACHE_DIR", "/tmp/numba_cache"),
        "MPLCONFIGDIR": os.environ.get("MPLCONFIGDIR", "/tmp/matplotlib"),
    }

    def run_repo(repo_dir: Path) -> dict[str, float] | None:
        if not repo_dir.is_dir():
            return None
        cmd = [python, "-m", "pytest", "--durations", str(top_n),
               "-q", "-p", "no:cacheprovider"]
        try:
            proc = subprocess.run(
                cmd, cwd=str(repo_dir), capture_output=True, text=True,
                timeout=timeout, env=env,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        # A non-zero exit (test failures) still prints the durations report; parse
        # regardless. Only a total absence of parseable lines means "no data".
        parsed = parse_durations(proc.stdout)
        return parsed or None

    return run_repo


def _slug(repo: str, nodeid: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{repo}__{nodeid}").strip("_")
    return f"{safe}.json"


def update_history(slug: str, duration: float, window: int) -> list[float]:
    HEART_UNIT_TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    history_path = HEART_UNIT_TIMINGS_DIR / slug
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


def run(runner: Runner | None = None, repos: list[str] | None = None) -> dict[str, Any]:
    """Run each repo's suite, update rolling per-test baselines, classify."""
    yellow_factor, red_factor, window, top_n = load_thresholds()
    repos = repos if repos is not None else DEFAULT_REPOS
    python = os.environ.get("HYGIENE_PYTHON", "python3")
    timeout = float(os.environ.get("HEART_UNIT_TEST_TIMEOUT", "1800"))
    root = Path(os.environ.get("PYAUTO_ROOT", str(Path.home() / "Code" / "PyAutoLabs")))
    runner = runner or default_runner(python, timeout, top_n)

    findings: dict[str, list[dict[str, Any]]] = {"red": [], "yellow": [], "green": []}
    repos_measured = 0
    unavailable: list[str] = []
    new_tests = 0

    for repo in repos:
        durations = runner(root / repo)
        if durations is None:
            unavailable.append(repo)
            continue
        repos_measured += 1
        for nodeid, duration in durations.items():
            history = update_history(_slug(repo, nodeid), duration, window)
            if len(history) <= 1:
                new_tests += 1
                continue
            baseline = statistics.median(history[:-1])
            if baseline <= 0:
                continue
            ratio = duration / baseline
            findings[classify(ratio, yellow_factor, red_factor)].append({
                "repo": repo,
                "test": nodeid,
                "latest_seconds": round(duration, 3),
                "baseline_seconds": round(baseline, 3),
                "ratio": round(ratio, 2),
                "samples": len(history) - 1,
            })

    summary = {
        "python": python,
        "repos_measured": repos_measured,
        "repos_unavailable": unavailable,
        "new_tests_no_baseline": new_tests,
        "red_count": len(findings["red"]),
        "yellow_count": len(findings["yellow"]),
        "green_count": len(findings["green"]),
        "red": sorted(findings["red"], key=lambda x: -x["ratio"]),
        "yellow": sorted(findings["yellow"], key=lambda x: -x["ratio"]),
    }

    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(HEART_STATE_DIR / "unit_test_timing.json", summary)
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
    elif not summary["repos_measured"]:
        glyph = glyph_warn()
        label = c_warn("no suite runnable (set HYGIENE_PYTHON / PYAUTO_ROOT)")
    else:
        glyph = glyph_ok()
        label = c_ok(f"{summary['green_count']} tracked tests within baseline")
    extra = c_meta(f" ({summary['new_tests_no_baseline']} new, no baseline)")
    print(f"{glyph} {c_info('unit_test_timing')} {label}{extra}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main(sys.argv))
