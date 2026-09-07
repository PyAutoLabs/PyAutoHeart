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
    timings/epochs.jsonl         # one line per epoch boundary (a human writes it)
    timings/gates.jsonl          # one line per UTC date
    timings/scripts/<repo>.jsonl # one line per (python leg, run id)
    timings/unit/<repo>.jsonl    # one line per (python leg, run id)

Deliberate choices, each one a recorded lesson:

* **Append-only, never rewritten.** Lines are only ever added. A wrong line is
  superseded by a later one, never edited away — the record is evidence, and
  evidence that can be quietly rewritten is not evidence. Consequently every
  writer here appends; none of them opens a file for truncation.
* **Dedupe on identity, never on the day.** The gates file is keyed by
  ``date``; the scripts and unit files by ``(python, run_id)``. The distinction matters:
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

``unit/<repo>.jsonl`` line (the libraries' own CI, via
``heart/checks/unit_timings.py``)::

    {"date": "2026-09-05", "at": "2026-09-04T10:00:00Z", "python": "3.12",
     "run_id": 7, "run_url": "https://ci.invalid/runs/7",
     "head_branch": "feat/x", "head_sha": "abc123", "package": "pkg_a",
     "import_s": 3.6,
     "cache": {"jax": "hit", "numba": "miss"},
     "suite": {"tests": 1500, "failures": 0, "errors": 0, "skipped": 3,
               "wall_s": 412.0},
     "slowest": {"tests/foo/test_bar.py::test_x": 12.5}}

Only the N SLOWEST tests are recorded, with the suite totals beside them: a
1500-test suite recorded whole would multiply this file forever for rows nothing
reads, while the totals keep the coverage visible. ``import_s`` is the
fresh-process cold import measured on the CI runner, and it is ``null`` — never
0.0 — when that import failed or timed out. ``cache`` is what the leg ran
UNDER: the libraries' gate restores a JAX compile cache and a numba function
cache, and a suite that recompiled everything is not a slower suite, it is a
differently-conditioned one. Both read ``unknown`` for a rollup older than the
sidecar. There is no ``datasets`` key here — that cache is the workspaces'.

``epochs.jsonl`` line::

    {"date": "2026-09-05", "label": "legacy",
     "note": "pre-rebuild reference round; comparisons must not cross this"}

An epoch boundary is where **the world changed** — a rebuild, a runner change,
a cap change — and it is the one line in this record a human writes, through
``python -m heart.timings epoch`` in a PR. The daily job never touches the
file: a boundary is a judgement, not an observation. Every reader here takes a
``since`` and drops the record lines dated before it, so the current epoch is
the only window a baseline is ever drawn from — a number measured before the
world changed is not a baseline for a run after it, it is a different
experiment. No epochs at all means one unbroken epoch, which is exactly the
behaviour this record had before the file existed.
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
UNIT_DIR = TIMINGS_DIR / "unit"
# The boundaries. A HUMAN writes this, through the `epoch` verb in a PR; the
# daily job never does — an epoch boundary is a judgement about the world (a
# rebuild, a runner change, a cap change), not something a daily job can
# observe.
EPOCHS_FILE = TIMINGS_DIR / "epochs.jsonl"

# The census key set, so a reader (and `show`) sees the same keys whether or
# not the record exists yet.
EMPTY_CENSUS = {
    "gates_days": 0,
    "gates_first": "",
    "gates_last": "",
    "scripts_observations": 0,
    "repos": 0,
    "unit_observations": 0,
    "unit_repos": 0,
    "unparseable": 0,
    # The boundaries recorded, and the one every reader is currently comparing
    # inside of (`None` when there is none — one unbroken epoch).
    "epochs": 0,
    "epoch": None,
}


def scripts_file(repo: str, directory: Path | str = TIMINGS_DIR) -> Path:
    """The per-repo scripts record path under ``directory``."""
    return Path(directory) / "scripts" / f"{repo}.jsonl"


def unit_file(repo: str, directory: Path | str = TIMINGS_DIR) -> Path:
    """The per-repo unit-timings record path under ``directory``."""
    return Path(directory) / "unit" / f"{repo}.jsonl"


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


# --- epochs ------------------------------------------------------------------
def _within_epoch(record: dict[str, Any], since: str) -> bool:
    """Is this record line inside the epoch that begins on ``since``?

    An empty ``since`` is "no boundary" and keeps everything, which is exactly
    how this record read before epochs existed. A line with NO date counts as
    older than any boundary: the record cannot place it after the world
    changed, and a baseline that might predate the change is not a baseline.
    """
    if not since:
        return True
    date = str(record.get("date") or "")
    return bool(date) and date >= since


def read_epochs(path: Path | str = EPOCHS_FILE) -> list[dict[str, Any]]:
    """The epoch boundaries, oldest first, as ``{"date", "label", "note"}``.

    A line with no date or no label is not a boundary — it could not be
    compared against and could not be named on the board — so it is dropped
    rather than carried as a half-boundary. The sort is stable on ``date``, so
    two boundaries recorded for the same day stay in the order they landed.
    """
    records, _ = read_jsonl(path)
    out: list[dict[str, Any]] = []
    for record in records:
        date = str(record.get("date") or "")
        label = str(record.get("label") or "")
        if not date or not label:
            continue
        out.append({"date": date, "label": label,
                    "note": str(record.get("note") or "")})
    out.sort(key=lambda rec: rec["date"])
    return out


def current_epoch(
    epochs: list[dict[str, Any]], today: str | None = None
) -> dict[str, Any] | None:
    """The boundary in force today — the LAST one dated on or before it.

    ``None`` when there is none, which every reader takes as "everything is one
    epoch". A boundary dated in the future is not in force yet: it can be
    written ahead of the change it labels (the PR that lands a rebuild appends
    it) without silently blinding every baseline the day it is committed.
    """
    day = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    current: dict[str, Any] | None = None
    for epoch in epochs or []:
        if str(epoch.get("date") or "") <= day:
            current = epoch
    return current


def append_epoch(
    path: Path | str, date: str, label: str, note: str = ""
) -> bool:
    """Append one boundary. ``False`` when it is already there, or malformed.

    Keyed on ``(date, label)``: re-running the verb is a no-op rather than a
    second boundary for the same judgement. Append-only like every other writer
    here — a boundary recorded wrongly is superseded by a later one, never
    edited away, because the record is evidence.
    """
    date = str(date or "")
    label = str(label or "").strip()
    if not label or not _is_iso_date(date):
        return False
    for existing in read_epochs(path):
        if existing["date"] == date and existing["label"] == label:
            return False
    _append(path, [{"date": date, "label": label, "note": str(note or "")}])
    return True


def _is_iso_date(value: str) -> bool:
    """``YYYY-MM-DD`` and nothing else — the record sorts on these as strings.

    The round-trip is the point: ``strptime`` happily parses ``2026-9-5``, and
    an unpadded date sorts *after* ``2026-12-01``, which would silently place a
    boundary in the wrong epoch.
    """
    text = str(value)
    try:
        parsed = datetime.datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return False
    return parsed.isoformat() == text


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
    path: Path | str = GATES_FILE, cap: int = 30, since: str = ""
) -> list[dict[str, Any]]:
    """The last ``cap`` gates lines, oldest first, in the *history* shape.

    ``{"date", "gates": {key: {"p50_s", "runs"}}}`` — exactly what
    ``ci_timing.history_baseline`` and ``dashboard._gate_spark`` read. The extra
    per-gate figures the record keeps (PR median, max, queue) are deliberately
    dropped from this view: the baseline and the sparkline are about the p50,
    and widening the shape here would change ``board.json``'s
    ``performance.history`` for every consumer downstream.

    ``since`` is the current epoch's date: lines dated before the boundary are
    invisible, because a gate measured before the world changed is not a
    baseline for one measured after it. Empty ``since`` filters nothing.
    """
    records, _ = read_jsonl(path)
    out: list[dict[str, Any]] = []
    for record in records:
        date = str(record.get("date") or "")
        if not date or not _within_epoch(record, since):
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
UNKNOWN_CACHE_STATE = "unknown"
_CACHE_STATES = ("hit", "miss")


# Which caches each record says a measurement was taken under. The scripts
# gate restores three; the libraries' gate restores two and has no dataset
# cache at all, so recording a `datasets` key on a unit line would be a field
# that could only ever say "unknown".
SCRIPTS_CACHE_KEYS = ("jax", "datasets", "numba")
UNIT_CACHE_KEYS = ("jax", "numba")


def _cache_states(cache: Any, keys: tuple[str, ...] = SCRIPTS_CACHE_KEYS
                  ) -> dict[str, str]:
    """The named cache states out of a rollup leg's cache block.

    The epoch is deliberately NOT among them: it is a property of the *keys*
    the workflow used, not of the measurement, and the record's job is to say
    what the measurement was taken under. Anything that is not one of the two
    known states records ``unknown`` — a rollup from before the sidecar
    existed, a section an older emitter never wrote, a leg the download failed
    on — never a fabricated miss.
    """
    src = cache if isinstance(cache, dict) else {}
    out = {}
    for key in keys:
        value = str(src.get(key) or "")
        out[key] = value if value in _CACHE_STATES else UNKNOWN_CACHE_STATE
    return out


def unit_cache_state(cache: Any) -> str:
    """The ONE state a unit leg's drift comparison turns on.

    A library suite is conditioned by two caches at once, and a comparison is
    only safe when both sides ran the same way. So: ``hit`` when the leg had
    both, ``miss`` when it had neither, and ``unknown`` for every mixture and
    for every leg where either state is itself unknown. That last case is the
    point — half-hot against half-cold is not a comparison anybody can read,
    and ``unknown`` compares exactly as it did before the sidecar existed
    rather than suppressing the row.

    Lives here, beside the record that writes ``cache_state`` into every
    previous unit row, rather than in ``heart.checks.unit_timings`` which
    imports it: this module imports nothing from ``heart``, so the dependency
    only ever points one way and there is no cycle to unpick.
    """
    states = _cache_states(cache, UNIT_CACHE_KEYS)
    jax, numba = states["jax"], states["numba"]
    if jax == numba and jax in _CACHE_STATES:
        return jax
    return UNKNOWN_CACHE_STATE


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
            # Whether the leg ran hot or cold. Recorded beside the seconds
            # because a measurement taken with a restored compile cache and one
            # taken without it are not the same measurement — and a baseline
            # that cannot say which it was is a baseline nothing can use.
            # "unknown" on every side for a rollup that predates the sidecar.
            "cache": _cache_states(leg.get("cache")),
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
    scripts_dir: Path | str = SCRIPTS_DIR, since: str = "",
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """``{(repo, python, entry): prev_row}`` — the last recorded observation.

    The shape is exactly what ``smoke_timings.classify_drift`` expects of a
    previous row (``seconds``/``run_id``/``run_url``/``cache_jax``), so the
    record can stand in for ``performance.scripts.rows`` off the published
    board.

    The *latest* line per python leg wins, and "latest" is file order: the file
    is append-only, so the last line for a leg is the most recently recorded
    one. Timed entries only — the record holds no untimed rows to begin with,
    and a row with no seconds could not be compared anyway.

    ``since`` is the current epoch's date, applied BEFORE "latest per leg" is
    chosen: a pre-boundary line is not a stale baseline to be superseded, it is
    a measurement of a different world, so it must not be the latest either.
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
            if not _within_epoch(record, since):
                continue
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
                    # The cache state this baseline was measured under, so
                    # `classify_drift` can refuse a comparison across two known
                    # but different states. A line recorded before the field
                    # existed reads "unknown" and compares as it always did.
                    "cache_jax": _cache_states(record.get("cache"))["jax"],
                }
    return out


# --- unit tests + imports (the libraries' own CI) ----------------------------
def unit_lines_from_rollup(
    rollup: Any, today: str
) -> dict[str, list[dict[str, Any]]]:
    """``{repo: [line, ...]}`` from a ``unit_timings.json`` rollup.

    One line per ``(repo, python, run_id)`` — the identity of a measurement, the
    same key the scripts record uses and for the same reason: the libraries'
    artifacts only change when a run happens, so a quiet week hands the daily
    job the SAME run seven times.

    Everything the line needs is on the rollup's ``repos[]`` item (provenance,
    the suite totals, the leg's slowest tests, the import seconds, the cache
    state), so the record can be built from the rollup alone without re-reading
    a sidecar. A leg with no ``run_id`` has no identity and is skipped outright.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(rollup, dict):
        return out

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

        suite_in = leg.get("suite") if isinstance(leg.get("suite"), dict) else {}
        suite = {
            "tests": _as_int(suite_in.get("tests")),
            "failures": _as_int(suite_in.get("failures")),
            "errors": _as_int(suite_in.get("errors")),
            "skipped": _as_int(suite_in.get("skipped")),
            "wall_s": _as_float(suite_in.get("wall_s")),
        }
        slowest: dict[str, float] = {}
        for row in leg.get("slowest") or []:
            if not isinstance(row, dict):
                continue
            nodeid = str(row.get("nodeid") or "")
            seconds = _as_float(row.get("seconds"))
            if not nodeid or seconds is None:
                continue
            slowest[nodeid] = seconds

        out.setdefault(repo, []).append({
            "date": today,
            "at": str(leg.get("at") or ""),
            "python": python,
            "run_id": run_id,
            "run_url": str(leg.get("run_url") or ""),
            "head_branch": str(leg.get("head_branch") or ""),
            "head_sha": str(leg.get("head_sha") or ""),
            "package": str(leg.get("package") or ""),
            # `null`, never 0.0: an import that failed was not an instant one.
            "import_s": _as_float(leg.get("import_s")),
            # What the suite ran UNDER. Recorded beside the seconds because a
            # suite measured with a restored compile cache and a restored numba
            # cache is not the same measurement as one that compiled from cold,
            # and a baseline that cannot say which it was is a baseline nothing
            # can use. "unknown" on both for a rollup that predates the sidecar.
            "cache": _cache_states(leg.get("cache"), UNIT_CACHE_KEYS),
            "suite": suite,
            "slowest": {name: slowest[name] for name in sorted(slowest)},
        })
    for repo in out:
        out[repo].sort(key=lambda line: (str(line.get("python") or ""),
                                         str(line.get("run_id") or "")))
    return out


def append_unit(path: Path | str, lines: list[dict[str, Any]]) -> int:
    """Append the unit lines not already recorded. Returns how many landed.

    Keyed by ``(python, run_id)``, NOT by the day — the scripts record's rule,
    for the scripts record's reason: a quiet week would otherwise write seven
    identical observations and make a flat week look like a week of
    measurements.
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


def previous_unit_rows(
    unit_dir: Path | str = UNIT_DIR, since: str = "",
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """``{(repo, python, nodeid): prev_row}`` — the last recorded observation.

    The shape is exactly what ``smoke_timings.classify_drift`` expects of a
    previous row (``seconds``/``run_id``/``run_url``/``cache_state``), which is
    the same function ``unit_timings`` classifies its test drift with — it
    reads the cache field by name, and this one is called ``cache_state``
    rather than the scripts record's ``cache_jax`` because it is jax and numba
    combined and naming it after half of what it means would mislead every
    reader of the record.

    The *latest* line per python leg wins, and "latest" is file order: the file
    is append-only, so the last line for a leg is the most recently recorded
    one. Only the recorded (slowest) tests are in there to begin with — a test
    that dropped out of the top N simply has no baseline next run, which is
    honest: nothing was recorded about it.

    ``since`` is the current epoch's date, applied BEFORE "latest per leg" is
    chosen, for the reason ``previous_script_rows`` gives: a pre-boundary line
    measured a different world and must not stand in as the latest.
    """
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    root = Path(unit_dir)
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.jsonl")):
        repo = path.name[: -len(".jsonl")]
        records, _ = read_jsonl(path)
        latest: dict[str, dict[str, Any]] = {}
        for record in records:
            if not _within_epoch(record, since):
                continue
            latest[str(record.get("python") or "")] = record
        for python, record in latest.items():
            slowest = record.get("slowest")
            if not isinstance(slowest, dict):
                continue
            for nodeid, value in slowest.items():
                seconds = _as_float(value)
                if seconds is None:
                    continue
                out[(repo, python, str(nodeid))] = {
                    "seconds": seconds,
                    "run_id": record.get("run_id"),
                    "run_url": str(record.get("run_url") or ""),
                    # The state this baseline was measured under, so
                    # `classify_drift` can refuse a comparison across two known
                    # but different ones. A line recorded before the field
                    # existed reads "unknown" and compares as it always did.
                    "cache_state": unit_cache_state(record.get("cache")),
                }
    return out


def import_history(
    unit_dir: Path | str = UNIT_DIR, window: int = 7, since: str = ""
) -> dict[tuple[str, str, str], list[float]]:
    """``{(repo, package, python): [seconds, ...]}`` — oldest first, last N.

    The median of this window is the import baseline (``import_time``'s
    doctrine, kept so a ratio means the same thing from either vantage). A
    ``null`` observation — an import that failed or timed out — is skipped
    rather than carried as a zero, which would drag the median toward a number
    nothing ever measured.

    ``since`` is the current epoch's date, applied BEFORE the window is taken:
    a window that reached back across a boundary would take the median of two
    different worlds, which is a number nothing ever measured either.
    """
    out: dict[tuple[str, str, str], list[float]] = {}
    root = Path(unit_dir)
    if not root.is_dir():
        return out
    cap = int(window) if window and int(window) > 0 else 0
    for path in sorted(root.glob("*.jsonl")):
        repo = path.name[: -len(".jsonl")]
        records, _ = read_jsonl(path)
        for record in records:
            package = str(record.get("package") or "")
            if not package or not _within_epoch(record, since):
                continue
            seconds = _as_float(record.get("import_s"))
            if seconds is None:
                continue
            key = (repo, package, str(record.get("python") or ""))
            out.setdefault(key, []).append(seconds)
    if cap:
        for key in out:
            out[key] = out[key][-cap:]
    return out


# --- census ------------------------------------------------------------------
def census(directory: Path | str = TIMINGS_DIR) -> dict[str, Any]:
    """What the record holds — the one-screen answer, and the board's detail.

    ``unparseable`` is summed across every file so a corrupt line is reported
    rather than silently dropped: the record is evidence, and a hole in it is
    itself a finding.

    ``epoch`` is the boundary every reader is currently comparing inside of,
    carried as ``{"date", "label"}`` — the note is the human's reasoning and
    belongs in the file, not on a board row. ``None`` means one unbroken epoch.
    """
    root = Path(directory)
    out = dict(EMPTY_CENSUS)
    epochs = read_epochs(root / "epochs.jsonl")
    out["epochs"] = len(epochs)
    epoch = current_epoch(epochs)
    out["epoch"] = (
        {"date": epoch["date"], "label": epoch["label"]} if epoch else None
    )
    gates, skipped = read_jsonl(root / "gates.jsonl")
    dates = sorted(str(rec.get("date") or "") for rec in gates if rec.get("date"))
    out["gates_days"] = len(dates)
    out["gates_first"] = dates[0] if dates else ""
    out["gates_last"] = dates[-1] if dates else ""
    out["unparseable"] = skipped

    def _count(sub: str) -> tuple[int, int]:
        """(repos with at least one line, total lines) under ``root/<sub>``."""
        repos = observations = 0
        directory = root / sub
        if directory.is_dir():
            for path in sorted(directory.glob("*.jsonl")):
                records, repo_skipped = read_jsonl(path)
                out["unparseable"] += repo_skipped
                if records:
                    repos += 1
                observations += len(records)
        return repos, observations

    out["repos"], out["scripts_observations"] = _count("scripts")
    out["unit_repos"], out["unit_observations"] = _count("unit")
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

    per_repo_unit = unit_lines_from_rollup(_read_rollup(ns.unit_timings), today)
    appended_unit: dict[str, int] = {}
    unit_added = 0
    for repo in sorted(per_repo_unit):
        lines = per_repo_unit[repo]
        added = append_unit(unit_file(repo, directory), lines)
        skipped += len(lines) - added
        if added:
            appended_unit[repo] = added
            unit_added += added

    payload = dict(census(directory))
    payload["appended_today"] = {"gates": gates_added, "scripts": appended}
    # The unit slice reports itself only when it was ASKED for, so an older
    # invocation's payload and summary line read exactly as they always did.
    # (The census above always carries the unit counts — the record has the
    # slice whether or not this run was handed a rollup for it.)
    if ns.unit_timings:
        payload["appended_today"]["unit"] = appended_unit
    out_path = Path(ns.census_out) if ns.census_out else _default_census_out()
    try:
        state.atomic_write_json(out_path, payload)
    except OSError:
        # The census is a convenience for the render, never the record itself.
        pass

    line = (
        f"timings: gates +{1 if gates_added else 0} line, "
        f"scripts +{scripts_added} lines across {len(appended)} repos"
    )
    if ns.unit_timings:
        line += f", unit +{unit_added} lines across {len(appended_unit)} repos"
    print(f"{line}, {skipped} skipped (already recorded)")
    return 0


def _do_epoch(ns: argparse.Namespace) -> int:
    """The human door: record where the world changed, in a PR.

    Exit 2 is a malformed boundary (a date that is not ``YYYY-MM-DD``, an empty
    label) — that is a typo at a keyboard, and a keyboard is where it should be
    reported. A boundary already recorded is exit 0: re-running the verb is a
    no-op, not a failure.
    """
    directory = Path(ns.dir) if ns.dir else TIMINGS_DIR
    date = ns.date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    label = str(ns.label or "").strip()
    if not label or not _is_iso_date(date):
        print("epoch: need an ISO YYYY-MM-DD --date and a non-empty --label",
              file=sys.stderr)
        return 2
    path = directory / "epochs.jsonl"
    if append_epoch(path, date, label, ns.note):
        print(_dump({"date": date, "label": label, "note": str(ns.note or "")}))
    else:
        print(f"epoch already recorded: {date} {label}")
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
    ap_append.add_argument("--unit-timings", default="",
                           help="the unit_timings.json rollup (per-test "
                                "durations + import seconds); missing => "
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

    # A HUMAN's verb, run in a PR — never the daily job's. `append` above does
    # not touch epochs.jsonl, and must not: an epoch boundary is a judgement
    # about the world (a rebuild, a runner change), not an observation.
    ap_epoch = sub.add_parser(
        "epoch", help="record an epoch boundary — where the world changed"
    )
    ap_epoch.add_argument("--label", required=True,
                          help="the short name of the boundary, e.g. `legacy`")
    ap_epoch.add_argument("--note", default="",
                          help="why this is a boundary — the reasoning a later "
                               "reader needs and cannot re-derive")
    ap_epoch.add_argument("--date", default="",
                          help="ISO date the boundary takes effect "
                               "(default: today, UTC)")
    ap_epoch.add_argument("--dir", default="",
                          help=f"the record directory (default: {TIMINGS_DIR})")

    ap_show = sub.add_parser("show", help="print the record's census")
    ap_show.add_argument("--dir", default="",
                         help=f"the record directory (default: {TIMINGS_DIR})")

    ns = ap.parse_args(argv)
    sys.path.insert(0, str(HEART_HOME))
    if ns.command == "append":
        return _do_append(ns)
    if ns.command == "epoch":
        return _do_epoch(ns)
    return _do_show(ns)


if __name__ == "__main__":
    sys.exit(main())
