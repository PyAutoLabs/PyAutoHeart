"""heart/timings.py — the permanent CI timing record, committed into this repo.

``ci_timing`` measures the gates and ``smoke_timings`` measures the scripts
inside them, and both of them carried their own history in exactly one place:
the ``board.json`` the last run published to Pages. That is free and it is
idempotent, but it is also *the same artifact the render produces* — a Pages
publish gap, a rewritten board, a schema change, and the history is simply
gone. There is no second copy, and nothing can be recomputed after the fact:
the Actions REST window is a couple of weeks and an artifact expires in days.

This module is the second copy, and it is the one that lasts: an append-only
record committed into PyAutoHeart's own repo beside the README block, one
commit a day, written by the daily cloud job and by nothing else.

Layout::

    timings/README.md            # the schema + the rules (doctrine)
    timings/gates.jsonl          # one line per UTC date
    timings/scripts/<repo>.jsonl # one line per (python leg, run id)

Deliberate choices, each one a recorded lesson:

* **Append-only, never rewritten.** Lines are only ever added. A wrong line is
  superseded by a later one, never edited away — the record is evidence, and
  evidence that can be quietly rewritten is not evidence. Consequently every
  writer here appends; none of them opens a file for truncation.
* **Dedupe on identity, never on the day.** The gates file is keyed by
  ``date``; the scripts files by ``(python, run_id)``. The distinction matters:
  a quiet week produces the SAME smoke run for seven days running, and keying
  the scripts record on the day would write seven copies of one measurement —
  the ``script_timing`` "one value repeated seven times" defect, recorded once
  and not to be re-derived. A re-run of the daily job on the same date is
  likewise a no-op rather than a duplicate point in every sparkline.
* **Only measurements are recorded.** ``rows`` in the smoke rollup carries the
  *timed* entries; an entry the runner skipped has ``seconds: null`` and was
  never a measurement, so it is not in the record. The per-leg census
  (``repos[].entries``) keeps the coverage visible beside the count.
* **A missing file is an empty record, never an error.** This is read and
  written by an unattended job: bad input degrades to an honest empty/partial
  record and exit 0. An unparseable line is skipped and *counted*, so the
  census can say the record has a hole rather than pretending it does not.
* **Single writer.** Only the daily cloud job appends (the workflow's own
  comment says so). A dev-box tick never does — two writers on an append-only
  file in a git repo is a merge conflict waiting for a human.

``gates.jsonl`` line::

    {"date": "2026-09-05", "ts": "2026-09-05T05:03:00+00:00",
     "gates": {"RepoA/Gate One": {"p50_s": 553.0, "pr_median_s": 601.0,
                                  "max_s": 912.0, "queue_median_s": 12.0,
                                  "runs": 14}}}

``scripts/<repo>.jsonl`` line::

    {"date": "2026-09-05", "at": "2026-09-04T10:00:00Z", "python": "3.12",
     "run_id": 7, "run_url": "https://ci.invalid/runs/7",
     "head_branch": "feat/x", "head_sha": "abc123", "env_profile": "smoke",
     "entries": {"imaging/x.py": [12.5, "passed", 600.0]}}

The entry triple is ``[seconds, status, cap_s]`` — positional on purpose: this
file grows by one line per leg per run forever, and the three keys repeated on
every entry would triple it for nothing a reader cannot infer from the schema.

``timings/epochs.jsonl`` is RESERVED (``EPOCHS_FILE``) for a future labelled
epoch boundary — ``{"date", "label", "note"}``, e.g. "runners moved to 8-core"
— so a reader can tell a step change from a regression. Nothing here writes it
and the file deliberately does not exist yet.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

HEART_HOME = Path(__file__).resolve().parents[1]
TIMINGS_DIR = HEART_HOME / "timings"
GATES_FILE = TIMINGS_DIR / "gates.jsonl"
SCRIPTS_DIR = TIMINGS_DIR / "scripts"
# Named so the reservation is in the code, not only in the README. NOTHING
# writes this: an epoch boundary is a human's judgement about the world (a
# runner change, a cap change), not something a daily job can observe.
EPOCHS_FILE = TIMINGS_DIR / "epochs.jsonl"

# The census key set, so a reader (and `show`) sees the same keys whether or
# not the record exists yet.
EMPTY_CENSUS = {
    "gates_days": 0,
    "gates_first": "",
    "gates_last": "",
    "scripts_observations": 0,
    "repos": 0,
    "unparseable": 0,
}


def scripts_file(repo: str, directory: Path | str = TIMINGS_DIR) -> Path:
    """The per-repo scripts record path under ``directory``."""
    return Path(directory) / "scripts" / f"{repo}.jsonl"


# --- jsonl I/O ---------------------------------------------------------------
def _as_float(value: Any) -> float | None:
    """A float, or None. ``bool`` is not a number here, and neither is junk."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return default


def read_jsonl(path: Path | str) -> tuple[list[dict[str, Any]], int]:
    """``(records, unparseable_count)``. A missing file is ``([], 0)``.

    A line that is not a JSON object is skipped and counted rather than raised
    over: this is read inside an unattended job, and one corrupt line must cost
    that line, not the record. The count travels into the census so the hole is
    visible instead of silent.
    """
    p = Path(path)
    if not p.is_file():
        return [], 0
    try:
        text = p.read_text()
    except OSError:
        return [], 0
    records: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(data, dict):
            records.append(data)
        else:
            skipped += 1
    return records, skipped


def _dump(record: dict[str, Any]) -> str:
    """One record → one compact, stable line.

    ``sort_keys`` so two writers of the same content produce the same bytes and
    a diff shows only what changed; ``ensure_ascii=False`` so a UTF-8 path stays
    readable in the file rather than becoming escapes.
    """
    return json.dumps(record, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _append(path: Path | str, records: list[dict[str, Any]]) -> None:
    """Append records to ``path`` (creating its directory). Never rewrites."""
    if not records:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(_dump(record) + "\n")


# --- gates -------------------------------------------------------------------
def gates_line_from_rollup(
    rollup: Any, today: str, ts: str
) -> dict[str, Any] | None:
    """One ``gates.jsonl`` line from a ``ci_timing.json`` rollup, or None.

    Only gates whose ``median_s`` is a number are recorded: a gate with no
    completed runs in the window measured nothing, and a row of nulls would
    make the record look like it has coverage it does not have. ``None`` when
    that leaves nothing — an empty line is worse than no line.
    """
    gates: dict[str, Any] = {}
    if isinstance(rollup, dict):
        for gate in rollup.get("gates") or []:
            if not isinstance(gate, dict):
                continue
            median = _as_float(gate.get("median_s"))
            if median is None:
                continue
            repo = str(gate.get("repo") or "")
            workflow = str(gate.get("workflow") or "")
            if not repo or not workflow:
                continue
            gates[f"{repo}/{workflow}"] = {
                "p50_s": median,
                "pr_median_s": _as_float(gate.get("pr_median_s")),
                "max_s": _as_float(gate.get("max_s")),
                "queue_median_s": _as_float(gate.get("queue_median_s")),
                "runs": _as_int(gate.get("runs_counted")),
            }
    if not gates:
        return None
    return {"date": today, "ts": ts, "gates": gates}


def append_gates(path: Path | str, line: dict[str, Any] | None) -> bool:
    """Append one dated gates line. ``False`` when that date is already there.

    Keyed by ``date``: the daily job re-run on the same day is a no-op rather
    than a second point for the same day in every gate's sparkline.
    """
    if not isinstance(line, dict) or not line.get("date"):
        return False
    existing, _ = read_jsonl(path)
    date = str(line["date"])
    if any(str(rec.get("date") or "") == date for rec in existing):
        return False
    _append(path, [line])
    return True


def gates_history(
    path: Path | str = GATES_FILE, cap: int = 30
) -> list[dict[str, Any]]:
    """The last ``cap`` gates lines, oldest first, in the *history* shape.

    ``{"date", "gates": {key: {"p50_s", "runs"}}}`` — exactly what
    ``ci_timing.history_baseline`` and ``dashboard._gate_spark`` read. The extra
    per-gate figures the record keeps (PR median, max, queue) are deliberately
    dropped from this view: the baseline and the sparkline are about the p50,
    and widening the shape here would change ``board.json``'s
    ``performance.history`` for every consumer downstream.
    """
    records, _ = read_jsonl(path)
    out: list[dict[str, Any]] = []
    for record in records:
        date = str(record.get("date") or "")
        if not date:
            continue
        gates = record.get("gates")
        if not isinstance(gates, dict):
            continue
        view = {
            str(key): {"p50_s": _as_float(row.get("p50_s")),
                       "runs": _as_int(row.get("runs"))}
            for key, row in gates.items()
            if isinstance(row, dict) and _as_float(row.get("p50_s")) is not None
        }
        out.append({"date": date, "gates": view})
    return out[-int(cap):] if cap and int(cap) > 0 else out


# --- scripts -----------------------------------------------------------------
def scripts_lines_from_rollup(
    rollup: Any, today: str
) -> dict[str, list[dict[str, Any]]]:
    """``{repo: [line, ...]}`` from a ``smoke_timings.json`` rollup.

    One line per ``(repo, python, run_id)``: the identity of a measurement.
    ``repos`` carries the provenance (run, branch, sha, env profile) and
    ``rows`` carries the timed entries, so the two are joined on that key.

    A leg with no ``run_id`` is skipped outright — without it the line has no
    identity, so the dedupe could not tell one run from another and a quiet
    week would record the same numbers over and over.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(rollup, dict):
        return out

    entries_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rollup.get("rows") or []:
        if not isinstance(row, dict):
            continue
        run_id = row.get("run_id")
        if run_id is None or str(run_id) == "":
            continue
        entry = str(row.get("entry") or "")
        seconds = _as_float(row.get("seconds"))
        if not entry or seconds is None:
            continue
        key = (str(row.get("repo") or ""), str(row.get("python") or ""),
               str(run_id))
        entries_by_key.setdefault(key, {})[entry] = [
            seconds, str(row.get("status") or ""), _as_float(row.get("cap_s")),
        ]

    seen: set[tuple[str, str, str]] = set()
    for leg in rollup.get("repos") or []:
        if not isinstance(leg, dict):
            continue
        repo = str(leg.get("repo") or "")
        run_id = leg.get("run_id")
        if not repo or run_id is None or str(run_id) == "":
            continue
        python = str(leg.get("python") or "")
        key = (repo, python, str(run_id))
        if key in seen:
            continue
        seen.add(key)
        entries = entries_by_key.get(key, {})
        out.setdefault(repo, []).append({
            "date": today,
            "at": str(leg.get("at") or ""),
            "python": python,
            "run_id": run_id,
            "run_url": str(leg.get("run_url") or ""),
            "head_branch": str(leg.get("head_branch") or ""),
            # Absent from an older rollup: stored empty rather than omitted, so
            # every line in the file has the same keys.
            "head_sha": str(leg.get("head_sha") or ""),
            "env_profile": str(leg.get("env_profile") or ""),
            "entries": {name: entries[name] for name in sorted(entries)},
        })
    for repo in out:
        out[repo].sort(key=lambda line: (str(line.get("python") or ""),
                                         str(line.get("run_id") or "")))
    return out


def append_scripts(path: Path | str, lines: list[dict[str, Any]]) -> int:
    """Append the scripts lines not already recorded. Returns how many landed.

    Keyed by ``(python, run_id)``, NOT by the day: the smoke artifacts only
    change when a PR runs, so a quiet week hands the daily job the same run
    seven times. Keying on the day would write seven identical observations and
    make a flat week look like a week of measurements.
    """
    existing, _ = read_jsonl(path)
    seen = {
        (str(rec.get("python") or ""), str(rec.get("run_id") or ""))
        for rec in existing
    }
    fresh: list[dict[str, Any]] = []
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        key = (str(line.get("python") or ""), str(line.get("run_id") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        fresh.append(line)
    _append(path, fresh)
    return len(fresh)


def previous_script_rows(
    scripts_dir: Path | str = SCRIPTS_DIR,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """``{(repo, python, entry): prev_row}`` — the last recorded observation.

    The shape is exactly what ``smoke_timings.classify_drift`` expects of a
    previous row (``seconds``/``run_id``/``run_url``), so the record can stand
    in for ``performance.scripts.rows`` off the published board.

    The *latest* line per python leg wins, and "latest" is file order: the file
    is append-only, so the last line for a leg is the most recently recorded
    one. Timed entries only — the record holds no untimed rows to begin with,
    and a row with no seconds could not be compared anyway.
    """
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    root = Path(scripts_dir)
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.jsonl")):
        repo = path.name[: -len(".jsonl")]
        records, _ = read_jsonl(path)
        latest: dict[str, dict[str, Any]] = {}
        for record in records:
            latest[str(record.get("python") or "")] = record
        for python, record in latest.items():
            entries = record.get("entries")
            if not isinstance(entries, dict):
                continue
            for entry, triple in entries.items():
                if not isinstance(triple, (list, tuple)) or not triple:
                    continue
                seconds = _as_float(triple[0])
                if seconds is None:
                    continue
                out[(repo, python, str(entry))] = {
                    "seconds": seconds,
                    "run_id": record.get("run_id"),
                    "run_url": str(record.get("run_url") or ""),
                    "status": str(triple[1]) if len(triple) > 1 else "",
                    "cap_s": _as_float(triple[2]) if len(triple) > 2 else None,
                }
    return out


# --- census ------------------------------------------------------------------
def census(directory: Path | str = TIMINGS_DIR) -> dict[str, Any]:
    """What the record holds — the one-screen answer, and the board's detail.

    ``unparseable`` is summed across every file so a corrupt line is reported
    rather than silently dropped: the record is evidence, and a hole in it is
    itself a finding.
    """
    root = Path(directory)
    out = dict(EMPTY_CENSUS)
    gates, skipped = read_jsonl(root / "gates.jsonl")
    dates = sorted(str(rec.get("date") or "") for rec in gates if rec.get("date"))
    out["gates_days"] = len(dates)
    out["gates_first"] = dates[0] if dates else ""
    out["gates_last"] = dates[-1] if dates else ""
    out["unparseable"] = skipped

    scripts_root = root / "scripts"
    repos = 0
    observations = 0
    if scripts_root.is_dir():
        for path in sorted(scripts_root.glob("*.jsonl")):
            records, repo_skipped = read_jsonl(path)
            out["unparseable"] += repo_skipped
            if records:
                repos += 1
            observations += len(records)
    out["repos"] = repos
    out["scripts_observations"] = observations
    return out


# --- I/O shell ---------------------------------------------------------------
def _read_rollup(path: str | Path | None) -> Any:
    """One rollup JSON, or ``{}``.

    A missing or unreadable rollup means "nothing to append from that slice" —
    the other slice still records. Never an error: the check that writes it
    already recorded its own failure honestly.
    """
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _default_census_out() -> Path:
    from heart import state

    return state.HEART_STATE_DIR / "timings_record.json"


def _do_append(ns: argparse.Namespace) -> int:
    from heart import state

    directory = Path(ns.dir) if ns.dir else TIMINGS_DIR
    today = ns.today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    ts = ns.ts or datetime.datetime.now(datetime.timezone.utc).isoformat()

    gates_line = gates_line_from_rollup(_read_rollup(ns.ci_timing), today, ts)
    gates_added = append_gates(directory / "gates.jsonl", gates_line)
    skipped = 1 if (gates_line is not None and not gates_added) else 0

    per_repo = scripts_lines_from_rollup(_read_rollup(ns.smoke_timings), today)
    appended: dict[str, int] = {}
    scripts_added = 0
    for repo in sorted(per_repo):
        lines = per_repo[repo]
        added = append_scripts(scripts_file(repo, directory), lines)
        skipped += len(lines) - added
        if added:
            appended[repo] = added
            scripts_added += added

    payload = dict(census(directory))
    payload["appended_today"] = {"gates": gates_added, "scripts": appended}
    out_path = Path(ns.census_out) if ns.census_out else _default_census_out()
    try:
        state.atomic_write_json(out_path, payload)
    except OSError:
        # The census is a convenience for the render, never the record itself.
        pass

    print(
        f"timings: gates +{1 if gates_added else 0} line, "
        f"scripts +{scripts_added} lines across {len(appended)} repos, "
        f"{skipped} skipped (already recorded)"
    )
    return 0


def _do_show(ns: argparse.Namespace) -> int:
    directory = Path(ns.dir) if ns.dir else TIMINGS_DIR
    for key, value in census(directory).items():
        print(f"{key}: {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="heart.timings")
    sub = ap.add_subparsers(dest="command", required=True)

    ap_append = sub.add_parser(
        "append", help="append today's observations to the committed record"
    )
    ap_append.add_argument("--ci-timing", default="",
                           help="the ci_timing.json rollup; missing => nothing "
                                "to append from that slice, never an error")
    ap_append.add_argument("--smoke-timings", default="",
                           help="the smoke_timings.json rollup; missing => "
                                "nothing to append from that slice")
    ap_append.add_argument("--today", default="",
                           help="ISO date for the gates line (default: today, UTC)")
    ap_append.add_argument("--ts", default="",
                           help="ISO timestamp stamped on the gates line")
    ap_append.add_argument("--dir", default="",
                           help=f"the record directory (default: {TIMINGS_DIR})")
    ap_append.add_argument("--census-out", default="",
                           help="where the census JSON lands (default: "
                                "$HEART_STATE_DIR/timings_record.json)")

    ap_show = sub.add_parser("show", help="print the record's census")
    ap_show.add_argument("--dir", default="",
                         help=f"the record directory (default: {TIMINGS_DIR})")

    ns = ap.parse_args(argv)
    sys.path.insert(0, str(HEART_HOME))
    if ns.command == "append":
        return _do_append(ns)
    return _do_show(ns)


if __name__ == "__main__":
    sys.exit(main())
