"""heart/checks/script_timing.py — read autohands run_all results, track
rolling per-script duration baselines, classify regressions.

Inputs:
- PyAutoHands/run_logs/latest/ (symlink to most recent run dir)
- Per-script JSON: <workspace>__<dir>__script.json containing
  `results` list with `file`, `duration_seconds`, `status`.

State (per Heart instance):
- ~/.pyauto-heart/timings/<workspace>__<dir>__<file>.json
  containing a rolling window of recent observations, each an entry
  ``{"duration_s": float, "run_id": str, "ts": str}``.

Output:
- ~/.pyauto-heart/script_timing.json with the latest regression summary.

Classification:
- green: ratio <= yellow_factor (default 1.5)
- yellow: yellow_factor < ratio <= red_factor (default 3.0)
- red:    ratio > red_factor

Where ratio = latest_duration / median(rolling_window).

Why the entries carry provenance
--------------------------------
The tick runs every few minutes but ``run_logs/latest`` only changes when a
new ``run_all`` finishes. The original implementation appended the durations
it read on *every* tick, so a single run was copied into the window until all
seven slots held the same number: ``median(prior)`` was then that one
observation wearing the clothes of a seven-run median — stable by
construction, and one unlucky run read as a regression.

The cure is run identity, not seeding rules. Each entry records the ``run_id``
it came from (the real name of the timestamped run dir behind the ``latest``
symlink); re-ticking on a run already at the head of the window *replaces*
that entry instead of appending, so ticks are idempotent and a window of
seven means seven distinct runs. Histories written before provenance existed
are read as entries with an empty ``run_id``; one whose values are all
identical is provably that same single-observation artefact, so it collapses
to one entry the first time it is touched.

Classification then waits for a real baseline: fewer than
``MIN_BASELINE_RUNS`` distinct runs in the prior window counts the script as
*building* rather than green/yellow/red, so a thin baseline never masquerades
as a verdict.

Why migration is attempted
--------------------------
The slug is path-derived (see :func:`slug_for`), so moving a script strands
its history under a filename nothing writes to again — and the check then has
nothing to compare against and silently never fires. Every scan therefore
notices history files it did not touch: where such an orphan unambiguously
corresponds to a script with no history (same workspace, same leaf script
name), its history is renamed onto the new slug so the baseline survives the
move. Ambiguous or unmatched orphans are never deleted or guessed at — they
are reported in the summary, loudly, which is what the silent case lacked.
"""

from __future__ import annotations

import datetime
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import yaml

HEART_STATE_DIR = Path(
    os.environ.get("HEART_STATE_DIR")
    or Path.home() / ".pyauto-heart"
)
HEART_TIMINGS_DIR = HEART_STATE_DIR / "timings"
HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"
PYAUTO_ROOT = Path(__file__).resolve().parents[3] if Path(__file__).resolve().parents[3].name == "PyAutoLabs" else Path.home() / "Code" / "PyAutoLabs"
TEST_RESULTS_LATEST = PYAUTO_ROOT / "PyAutoHands" / "run_logs" / "latest"

# Distinct runs the prior window must hold before a ratio is a verdict rather
# than a coin flip. Below this the script is counted as "building" a baseline.
MIN_BASELINE_RUNS = 3

# Cap on the migrated/orphaned lists carried in the summary JSON.
MAX_LISTED = 20


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

    Moving a script therefore changes its slug; :func:`migrate_orphans` (run
    from :func:`run`) reattaches the stranded history where the match is
    unambiguous.
    """
    # The autohands run_all writes ``file`` as an absolute path. Strip
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


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _state_module():
    """Import ``heart.state`` lazily (this module is also run standalone)."""
    if str(HEART_HOME) not in sys.path:
        sys.path.insert(0, str(HEART_HOME))
    from heart import state

    return state


def normalize_history(raw: Any) -> list[dict[str, Any]]:
    """Coerce a stored history to the entry shape, tolerating legacy floats.

    Legacy histories are bare ``[float, ...]`` lists with no provenance; they
    become entries with an empty ``run_id``. A legacy history whose values are
    ALL identical is the single-observation artefact this check used to
    manufacture (see the module docstring) — it collapses to ONE entry, since
    that is the only real observation it ever held.
    """
    if not isinstance(raw, list):
        return []

    entries: list[dict[str, Any]] = []
    all_legacy = True
    for item in raw:
        if isinstance(item, dict):
            duration = item.get("duration_s", item.get("duration"))
            if duration is None:
                continue
            try:
                duration = float(duration)
            except (TypeError, ValueError):
                continue
            all_legacy = False
            entries.append({
                "duration_s": duration,
                "run_id": str(item.get("run_id") or ""),
                "ts": str(item.get("ts") or ""),
            })
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            entries.append({"duration_s": float(item), "run_id": "", "ts": ""})
        # Anything else is unreadable noise; drop it.

    if all_legacy and len(entries) > 1:
        durations = {e["duration_s"] for e in entries}
        if len(durations) == 1:
            entries = entries[:1]
    return entries


def read_history(slug: str) -> list[dict[str, Any]]:
    """Return the normalized history entries stored for ``slug`` (may be [])."""
    history_path = HEART_TIMINGS_DIR / slug
    if not history_path.is_file():
        return []
    try:
        raw = json.loads(history_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return normalize_history(raw)


def _write_history(slug: str, entries: list[dict[str, Any]]) -> None:
    """Atomically persist ``entries`` for ``slug`` (concurrent ticks are real)."""
    HEART_TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    _state_module().atomic_write_json(HEART_TIMINGS_DIR / slug, entries)


def distinct_run_count(entries: list[dict[str, Any]]) -> int:
    """Number of distinct runs ``entries`` represents.

    Entries with an empty ``run_id`` predate provenance; each counts as its
    own run, because a legacy history that survived the identical-value
    collapse is genuine accumulation across ticks we cannot name.
    """
    named: set[str] = set()
    unnamed = 0
    for entry in entries:
        run_id = entry.get("run_id") or ""
        if run_id:
            named.add(run_id)
        else:
            unnamed += 1
    return len(named) + unnamed


def update_history(
    slug: str,
    duration: float,
    window: int,
    run_id: str = "",
    ts: str | None = None,
) -> list[dict[str, Any]]:
    """Record ``duration`` for ``slug``, returning the new history entries.

    An observation from the run already at the head of the window REPLACES
    that entry rather than appending: the tick fires far more often than
    ``run_all`` does, and re-reading one run must not fill the window with
    copies of it. Distinct runs append as usual, trimmed to ``window``.
    """
    history = read_history(slug)
    entry = {
        "duration_s": float(duration),
        "run_id": str(run_id or ""),
        "ts": ts or _now_iso(),
    }

    if history and entry["run_id"] and history[-1].get("run_id") == entry["run_id"]:
        history[-1] = entry
    else:
        history.append(entry)

    # Keep at most `window` most recent.
    history = history[-window:]
    _write_history(slug, history)
    return history


def run_id_for(results_dir: Path) -> str:
    """Identity of the run behind ``results_dir``.

    ``run_logs/latest`` is a symlink onto a timestamped run dir; the real
    directory name is the stable identity of the run that produced these
    durations, so resolve through the link. Falls back to the raw name when
    the path cannot be resolved.
    """
    try:
        resolved = results_dir.resolve()
        name = resolved.name
    except OSError:
        name = ""
    return name or results_dir.name


def _slug_identity(slug: str) -> tuple[str, str]:
    """Return (workspace, final script token) for a history filename."""
    stem = slug[:-len(".json")] if slug.endswith(".json") else slug
    parts = stem.split("__")
    workspace = parts[0] if parts else ""
    final = parts[-1] if parts else ""
    return workspace, final


def migration_candidates(slug: str, orphans: list[str]) -> list[str]:
    """Orphan history files that could belong to ``slug`` after a move.

    A move keeps the workspace and the script's own name and changes only the
    directories between them, so those two tokens are the match. Anything less
    specific would silently glue one script's baseline onto another.
    """
    workspace, final = _slug_identity(slug)
    if not workspace or not final:
        return []
    return [
        o for o in orphans
        if _slug_identity(o) == (workspace, final)
    ]


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


def _existing_history_files() -> list[str]:
    if not HEART_TIMINGS_DIR.is_dir():
        return []
    return sorted(p.name for p in HEART_TIMINGS_DIR.glob("*.json") if p.is_file())


def run(results_dir: Path | None = None) -> dict[str, Any]:
    """Update rolling timings from results_dir; return classification summary."""
    results_dir = results_dir or TEST_RESULTS_LATEST
    yellow_factor, red_factor, window = load_thresholds()
    run_id = run_id_for(results_dir)
    ts = _now_iso()

    findings: dict[str, list[dict[str, Any]]] = {"red": [], "yellow": [], "green": []}
    total = 0
    new_scripts = 0
    building = 0
    migrations: list[dict[str, str]] = []

    scanned = scan_latest_results(results_dir)
    slugs = [slug_for(e["project"], e["directory"], e["file"]) for e in scanned]
    touched = set(slugs)
    # Anything this scan does not write to is a candidate stranded baseline.
    orphans = [name for name in _existing_history_files() if name not in touched]

    for entry, slug in zip(scanned, slugs):
        if not read_history(slug):
            # No baseline here: a move may have stranded it under another name.
            candidates = migration_candidates(slug, orphans)
            if len(candidates) == 1:
                source = candidates[0]
                (HEART_TIMINGS_DIR / source).replace(HEART_TIMINGS_DIR / slug)
                orphans.remove(source)
                migrations.append({"from": source, "to": slug})
            # Zero or several candidates: never guess — they stay orphaned and
            # get reported below.

        history = update_history(slug, entry["duration"], window, run_id=run_id, ts=ts)
        total += 1

        prior = history[:-1]
        if not prior:
            # First observation, no baseline yet.
            new_scripts += 1
            continue
        if distinct_run_count(prior) < MIN_BASELINE_RUNS:
            # A baseline too thin to be a verdict.
            building += 1
            continue
        # Compare latest to median of the prior history (exclude current).
        baseline = statistics.median(p["duration_s"] for p in prior)
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
        "run_id": run_id,
        "total_scripts": total,
        "new_scripts_no_baseline": new_scripts,
        "building_count": building,
        "migrated_count": len(migrations),
        "orphaned_count": len(orphans),
        "migrated": migrations[:MAX_LISTED],
        "orphaned": orphans[:MAX_LISTED],
        "red_count": len(findings["red"]),
        "yellow_count": len(findings["yellow"]),
        "green_count": len(findings["green"]),
        "red": sorted(findings["red"], key=lambda x: -x["ratio"]),
        "yellow": sorted(findings["yellow"], key=lambda x: -x["ratio"]),
    }

    _state_module().atomic_write_json(HEART_STATE_DIR / "script_timing.json", summary)
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
    extra = c_meta(
        f" ({summary['new_scripts_no_baseline']} new,"
        f" {summary['building_count']} building)"
    )
    if summary["migrated_count"]:
        extra += c_meta(f" · {summary['migrated_count']} migrated")
    if summary["orphaned_count"]:
        # Loud: a stranded baseline is a check that silently never fires.
        extra += " " + c_warn(f"· {summary['orphaned_count']} orphaned baseline(s)")
    print(f"{glyph} {c_info('script_timing')} {label}{extra}")
    return 0


if __name__ == "__main__":
    # Allow running standalone. We import the color helpers at runtime
    # to avoid a hard dep when called as a library.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main(sys.argv))
