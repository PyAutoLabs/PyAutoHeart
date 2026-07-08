"""heart/checks/profiling_drift.py — scan autolens_profiling result JSONs
for pinned-value drift flags.

autolens_profiling's runtime cells compare each run's log-likelihood /
log-evidence against pinned baseline values and **record** the outcome in
every result JSON instead of crashing (autolens_profiling#54):

- ``pinned_expected`` — the baseline value, or ``null`` when the instrument
  has no pin;
- ``pinned_drift`` — list of ``{label, expected, got, rel_diff, rtol}``
  records; an empty list means every compared value matched.

A non-empty ``pinned_drift`` is a health finding either way — a library
regression or a stale pin — and the profiling baselines are non-comparable
until it is resolved (boundary rule:
``autolens_profiling/results/notes/design_lock_in.md``). Heart observes and
reports; adjudication belongs to autolens_workspace_test / the library repos.

Inputs:
- autolens_profiling/results/**/*.json (read-only; repo absent → not observed)

Output:
- ~/.pyauto-heart/profiling_drift.json with the latest scan summary.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HEART_STATE_DIR = Path(
    os.environ.get("HEART_STATE_DIR")
    or Path.home() / ".pyauto-heart"
)
HEART_HOME = Path(__file__).resolve().parents[2]
PYAUTO_ROOT = Path(__file__).resolve().parents[3] if Path(__file__).resolve().parents[3].name == "PyAutoLabs" else Path.home() / "Code" / "PyAutoLabs"
RESULTS_ROOT = PYAUTO_ROOT / "autolens_profiling" / "results"


def scan_results(results_root: Path) -> dict[str, Any]:
    """Scan every result JSON under results_root for pinned-drift flags."""
    files_scanned = 0
    pinned_checked = 0
    findings: list[dict[str, Any]] = []

    for json_path in sorted(results_root.rglob("*.json")):
        try:
            data = json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict) or "pinned_drift" not in data:
            continue
        files_scanned += 1
        if data.get("pinned_expected") is not None:
            pinned_checked += 1
        drift = data.get("pinned_drift") or []
        if not drift:
            continue
        findings.append({
            "path": str(json_path.relative_to(results_root)),
            "instrument": data.get("instrument"),
            "autolens_version": data.get("autolens_version"),
            "drift": [
                {
                    "label": d.get("label"),
                    "expected": d.get("expected"),
                    "got": d.get("got"),
                    "rel_diff": d.get("rel_diff"),
                }
                for d in drift
                if isinstance(d, dict)
            ],
        })

    return {
        "results_root": str(results_root),
        "observed": True,
        "files_scanned": files_scanned,
        "pinned_checked": pinned_checked,
        "drift_count": len(findings),
        "findings": findings,
    }


def run(results_root: Path | None = None) -> dict[str, Any]:
    """Scan and persist the summary; repo absent → observed: False."""
    results_root = results_root or RESULTS_ROOT

    if not results_root.is_dir():
        summary: dict[str, Any] = {
            "results_root": str(results_root),
            "observed": False,
            "files_scanned": 0,
            "pinned_checked": 0,
            "drift_count": 0,
            "findings": [],
        }
    else:
        summary = scan_results(results_root)

    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(HEART_STATE_DIR / "profiling_drift.json", summary)
    return summary


def main(argv: list[str]) -> int:
    results_root = Path(argv[1]) if len(argv) > 1 else RESULTS_ROOT
    summary = run(results_root)

    from heart_color import c_ok, c_warn, c_info, c_meta, glyph_ok, glyph_warn

    n_drift = summary["drift_count"]
    n_scanned = summary["files_scanned"]
    n_pinned = summary["pinned_checked"]
    if not summary["observed"]:
        print(
            f"{glyph_warn()} {c_info('profiling_drift')} "
            f"{c_meta('skipped (autolens_profiling/results not found)')}"
        )
    elif n_drift:
        print(
            f"{glyph_warn()} {c_info('profiling_drift')} "
            f"{c_warn(f'{n_drift} drifted result(s)')} "
            f"{c_meta(f'({n_scanned} scanned)')}"
        )
    else:
        print(
            f"{glyph_ok()} {c_info('profiling_drift')} "
            f"{c_ok(f'{n_scanned} results clean')} "
            f"{c_meta(f'({n_pinned} with pins)')}"
        )
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main(sys.argv))
