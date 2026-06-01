"""pulse/checks/test_run.py — surface the latest PyAutoBuild test-run verdict.

PyAutoBuild's release pipeline writes an aggregated ``report.json`` into each
run directory (reachable via the ``test_results/latest`` symlink). It carries
the single most important release signal — a top-level ``ready`` boolean —
plus per-status counts, per-project breakdown, and the ``slow_skips`` /
``needs_fix_skips`` lists (each entry already carrying an ``is_stale`` flag
computed by ``slow_skip_check.py``).

This check reads that file (no heavy imports, just JSON) and emits
``$PULSE_STATE_DIR/test_run.json`` so the readiness verdict and the status
dashboard can consume it continuously, instead of the signal only existing at
release time.

Older runs predate ``report.json``; for those we fall back to summing the
per-job ``*__script.json`` ``summary`` blocks, set ``ready`` to ``None`` (the
verdict treats that as "unknown", a yellow — never a silent green), and mark
the parked-script counts unknown.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Reuse the same root/latest resolution as script_timing.py.
PULSE_HOME = Path(__file__).resolve().parents[2]
_p3 = Path(__file__).resolve().parents[3]
PYAUTO_ROOT = _p3 if _p3.name == "PyAutoLabs" else Path.home() / "Code" / "PyAutoLabs"
TEST_RESULTS_LATEST = PYAUTO_ROOT / "PyAutoBuild" / "test_results" / "latest"
PULSE_STATE_DIR = Path.home() / ".pyauto-pulse"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _from_report(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary", {}) or {}
    parked = []
    for key in ("slow_skips", "needs_fix_skips"):
        for entry in report.get(key, []) or []:
            if isinstance(entry, dict) and entry.get("is_stale"):
                parked.append(
                    {
                        "workspace": entry.get("workspace"),
                        "pattern": entry.get("pattern"),
                        "category": entry.get("category"),
                        "age_days": entry.get("age_days"),
                    }
                )
    return {
        "ready": report.get("ready"),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "skipped": summary.get("skipped", 0),
        "timeout": summary.get("timeout", 0),
        "per_project": report.get("per_project", {}) or {},
        "run_label": report.get("run_label", ""),
        "parked_stale_count": len(parked),
        "parked_stale": parked,
        "source": "report",
    }


def _from_per_job(results_dir: Path) -> dict[str, Any]:
    """Fallback: sum the per-job ``*__script.json`` summaries. We can't know
    overall readiness from these alone, so ready is left unknown (None)."""
    totals = {"passed": 0, "failed": 0, "skipped": 0, "timeout": 0}
    per_project: dict[str, dict[str, int]] = {}
    found = False
    for jp in sorted(results_dir.glob("*__script.json")):
        data = _read_json(jp)
        if not isinstance(data, dict):
            continue
        found = True
        s = data.get("summary", {}) or {}
        proj = data.get("project", "?")
        pp = per_project.setdefault(proj, {})
        for k in totals:
            v = int(s.get(k, 0) or 0)
            totals[k] += v
            if v:
                pp[k] = pp.get(k, 0) + v
    if not found:
        return {}
    return {
        "ready": None,
        **totals,
        "per_project": per_project,
        "run_label": results_dir.resolve().name,
        "parked_stale_count": 0,
        "parked_stale": [],
        "source": "per-job",
    }


def run(results_dir: Path | None = None) -> dict[str, Any]:
    results_dir = results_dir or TEST_RESULTS_LATEST
    summary: dict[str, Any]
    report = _read_json(results_dir / "report.json")
    if isinstance(report, dict):
        summary = _from_report(report)
    else:
        summary = _from_per_job(results_dir)

    PULSE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    (PULSE_STATE_DIR / "test_run.json").write_text(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str]) -> int:
    results_dir = Path(argv[1]) if len(argv) > 1 else TEST_RESULTS_LATEST
    summary = run(results_dir)

    sys.path.insert(0, str(PULSE_HOME))
    from pulse.pulse_color import c_ok, c_warn, c_fail, c_info, c_meta, glyph_ok, glyph_warn, glyph_fail

    if not summary:
        print(f"{c_meta('·')} {c_info('test_run')} {c_meta('(no test_results yet)')}")
        return 0

    ready = summary.get("ready")
    failed = summary.get("failed", 0)
    if ready is False or failed:
        glyph = glyph_fail()
        label = c_fail(f"NOT ready ({failed} failed)")
    elif ready is True:
        glyph = glyph_ok()
        label = c_ok("ready")
    else:
        glyph = glyph_warn()
        label = c_warn("ready unknown")
    extra = c_meta(
        f" {summary.get('passed', 0)}p/{failed}f/{summary.get('skipped', 0)}s"
        f" @ {summary.get('run_label', '?')}"
    )
    print(f"{glyph} {c_info('test_run')} {label}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
