"""heart/checks/smoke_timings.py — per-script CI timings (the fine-grained speed leg).

``ci_timing`` measures the *gates* — how long a whole workflow keeps a
contributor waiting. That number moves for a reason, and until now the reason
was only recoverable by hand-scraping a job log before it expired: which script
inside the smoke suite got slower, which one hit its kill timer. The runners
already write that data (``smoke_timings.json``, the ``smoke_timings/1``
dataset the shared result collector emits) and the PR gate already uploads
it as an artifact. This check *ingests* it: one row per script per repo per python
leg, standing on the ⏱ board beside the workflow-level gates.

Two modes, same module (the ci_timing shape):

* **per-repo** (``--name/--group/--owner``) — the bash entry point hands us one
  repo's artifacts listing plus a directory of already-extracted artifacts, and
  we write ``$HEART_PER_REPO_DIR/<name>.smoke_timings.json``.
* **aggregate** (``--aggregate``) — read every sidecar plus the *previously
  published* ``board.json`` and write the global rollup at
  ``$HEART_STATE_DIR/smoke_timings.json``: per-repo/per-leg totals, every timed
  row, run-to-run slowdowns, TIMEOUT events, and the errors.

There is also a **--plan** mode: the shell leg needs to know *which* artifact
ids to download before it can download them, and the selection rule lives here
in Python rather than being re-derived in `jq`.

Deliberate choices, each one a recorded lesson:

* **Only the PR gate's artifact name is ingested.** ``ARTIFACT_RE`` matches
  ``smoke-timings-<major>.<minor>`` — the per-python-version legs of the smoke
  gate. The WEEKLY sweep publishes ``smoke-timings-{scripts,notebooks}-<project>-<dir>``
  from a ~50-leg matrix; that is a different channel with a different cadence
  and a different name shape, and it is deliberately NOT ingested here. Mixing
  the two would compare a weekly full run against a PR smoke run and call the
  difference a slowdown.
* **Newest non-expired artifact per name.** One row per python leg, from the
  most recent run that still has a downloadable artifact. An expired artifact
  is not data — GitHub deletes the blob and keeps the metadata.
* **``seconds: null`` is never zero.** The producer emits ``null`` for an entry
  that never ran (skipped, or listed-but-missing). Counting those as 0 s would
  put fabricated rows into a dataset whose whole purpose is timing, so a null
  entry is carried as a row of the leg but never counted as ``timed`` and never
  summed into ``total_s``.
* **Drift needs BOTH gates**, as in ci_timing: ``warn`` only when this run is
  ≥ ``slow_factor`` × the previous observation AND ≥ ``min_delta_s`` slower.
  The numbers differ from ci_timing's on purpose (scripts are seconds, not
  minutes) and live in ``config/repos.yaml``, never here.
* **A run is never compared against itself.** The previous observation is
  self-carried through the published board, so a re-render on the same day sees
  the same run's rows. Every row carries its ``run_id``; a previous row from
  the SAME run yields ``ok`` with no ratio rather than a reassuring 1.0×.
* **The previous observation comes from the committed record first.**
  ``timings/scripts/<repo>.jsonl`` (see ``heart/timings.py``) is durable and
  keyed by ``(python, run_id)``; the published board is the same artifact this
  render produces, so a Pages gap loses it. The board stays as the fallback,
  and the ``performance.scripts.rows`` block is written either way.
* **Drift is advisory.** Rows are ``ok`` or ``warn``, never ``fail``; only
  TIMEOUT entries are hard rows. The readiness verdict is untouched — this is
  a dashboard leg, not a gate.
* **A failed fetch is not a quiet repo.** ``--fetch-error`` records the reason
  and writes NO legs; a leg whose download or parse failed keeps its provenance
  with its own ``error`` and no entries. "We could not ask" must never render
  as "all quiet", and one leg's 403 must never lose the other leg's data.

Per-repo sidecar schema (``<name>.smoke_timings.json``)::

    {
      "name": "RepoA", "group": "workspaces", "owner": "OwnerX",
      "error": "",                       # non-empty => the listing fetch failed
      "ts": "...",
      "legs": [
        {"python": "3.12", "artifact_id": 42, "run_id": 7,
         "run_url": "https://github.com/OwnerX/RepoA/actions/runs/7",
         "head_branch": "feat/x", "head_sha": "abc...",
         "at": "2026-09-01T10:00:00Z",   # the artifact's created_at
         "env_profile": "smoke", "error": "",
         "entries": [{"entry": "imaging/x.py", "kind": "script",
                      "status": "passed", "seconds": 12.5, "cap_s": 600.0,
                      "exit_code": 0}],
         "counts": {"passed": 1, "failed": 0, "skipped": 0, "timeout": 0,
                    "timed": 1},
         "total_s": 12.5}
      ]
    }

Global rollup schema (``smoke_timings.json``)::

    {"ts",
     "repos":  [{repo, python, run_id, run_url, head_branch, head_sha,
                 env_profile, at, entries, timed, total_s, error,
                 slowest: [...]}],
     "rows":   [{repo, python, entry, kind, status, seconds, cap_s, exit_code,
                 run_id, run_url, prev_s, prev_run_id, ratio, delta_s, state,
                 prompt}],
     "slowed": [...], "events": [...], "errors": [{repo, error}],
     "thresholds": {...}}

``rows`` is the block the NEXT render reads back as its previous observation
(through ``performance.scripts.rows`` on the published board), which is why
every row carries its ``run_id`` and ``run_url``.

Every actionable row — a slowed script, a TIMEOUT — carries its own
ready-to-paste prompt string. The producer writes the prompt; the renderer
never re-derives it.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any

from heart.checks.ci_timing import read_prev_board  # reused, never duplicated

HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"

# The PR gate's per-python-version artifact, and ONLY that one. The weekly
# sweep's `smoke-timings-{scripts,notebooks}-<project>-<directory>` names are a
# different channel (different cadence, different matrix) and never match.
ARTIFACT_RE = re.compile(r"^smoke-timings-(\d+\.\d+)$")

TIMINGS_FILENAME = "smoke_timings.json"
TIMINGS_SCHEMA = "smoke_timings/1"

# Statuses the producer emits; counted per leg so the coverage behind a total
# stays visible next to it.
TRACKED_STATUSES = ("passed", "failed", "skipped", "timeout")

# Fallback when config/repos.yaml is unreadable; kept in sync with the YAML.
DEFAULT_SMOKE_TIMINGS_THRESHOLDS = {
    "slow_factor": 2.0,
    "min_delta_s": 5,
    "top_n": 5,
}

NO_DATASET_ERROR = f"no {TIMINGS_FILENAME} in artifact"


# --- config ------------------------------------------------------------------
def _load_config(config_path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    try:
        import yaml

        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def load_thresholds(config_path: Path | str = CONFIG_PATH) -> dict[str, float]:
    """Return the ``thresholds.smoke_timings`` block, defaulted key by key."""
    out = dict(DEFAULT_SMOKE_TIMINGS_THRESHOLDS)
    block = (_load_config(config_path).get("thresholds") or {}).get("smoke_timings") or {}
    if isinstance(block, dict):
        for key in out:
            if isinstance(block.get(key), (int, float)) and not isinstance(
                block.get(key), bool
            ):
                out[key] = block[key]
    return out


# --- pure helpers ------------------------------------------------------------
def _as_float(value: Any) -> float | None:
    """A float, or None. ``bool`` is not a number here, and neither is junk."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    num = _as_float(value)
    return int(num) if num is not None else None


def select_artifacts(listing: Any) -> list[dict[str, Any]]:
    """The newest non-expired ``smoke-timings-<py>`` artifact per python leg.

    Accepts the REST ``{"artifacts": [...]}`` object the shell leg fetches and
    a bare list. Anything that is not an artifact object — a 404 body, a string,
    a null — is skipped rather than raised over; this runs unattended.

    No URL is composed here: the selector is pure and knows nothing about a
    GitHub host. ``build_sidecar`` owns ``run_url``, because it is the thing
    that knows the owner and the repo name.
    """
    if isinstance(listing, dict):
        raw = listing.get("artifacts") or []
    elif isinstance(listing, list):
        raw = listing
    else:
        raw = []
    if not isinstance(raw, list):
        return []

    best: dict[str, dict[str, Any]] = {}
    for art in raw:
        if not isinstance(art, dict):
            continue
        name = str(art.get("name") or "")
        match = ARTIFACT_RE.match(name)
        if not match:
            continue
        if art.get("expired"):
            # The metadata outlives the blob; an expired artifact is not data.
            continue
        art_id = _as_int(art.get("id"))
        if art_id is None:
            continue
        run = art.get("workflow_run")
        run = run if isinstance(run, dict) else {}
        row = {
            "id": art_id,
            "name": name,
            "python": match.group(1),
            "run_id": _as_int(run.get("id")),
            "head_branch": str(run.get("head_branch") or ""),
            "head_sha": str(run.get("head_sha") or ""),
            "created_at": str(art.get("created_at") or ""),
        }
        current = best.get(name)
        if current is None or row["created_at"] > current["created_at"]:
            best[name] = row
    return [best[name] for name in sorted(best)]


def parse_timings(text: str) -> tuple[dict[str, Any] | None, str]:
    """One ``smoke_timings.json`` → (dataset, "") or (None, reason).

    The dataset's entries are normalised to exactly the six keys this check
    stores, with every type coerced defensively: the file is written by another
    organ on a runner we do not control, and a surprising value must degrade to
    ``None`` rather than raise out of an unattended check.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None, f"{TIMINGS_FILENAME} was not valid JSON"
    if not isinstance(data, dict):
        return None, f"{TIMINGS_FILENAME} was not an object"
    if str(data.get("schema") or "") != TIMINGS_SCHEMA:
        return None, f"not a {TIMINGS_SCHEMA} dataset"
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        return None, "entries was not a list"

    entries: list[dict[str, Any]] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        entry = str(item.get("entry") or "")
        if not entry:
            continue
        entries.append(
            {
                "entry": entry,
                "kind": str(item.get("kind") or ""),
                "status": str(item.get("status") or ""),
                # NEVER 0.0 for a missing measurement: null means "never ran".
                "seconds": _as_float(item.get("seconds")),
                "cap_s": _as_float(item.get("cap_s")),
                "exit_code": _as_int(item.get("exit_code")),
            }
        )
    return (
        {
            "schema": TIMINGS_SCHEMA,
            "project": str(data.get("project") or ""),
            "directory": str(data.get("directory") or ""),
            "run_type": str(data.get("run_type") or ""),
            "env_profile": str(data.get("env_profile") or ""),
            "python": str(data.get("python") or ""),
            "ts": str(data.get("ts") or ""),
            "entries": entries,
        },
        "",
    )


def read_downloaded_leg(directory: Path | str) -> tuple[list[dict[str, Any]], str, dict[str, str]]:
    """Every ``smoke_timings.json`` under one extracted artifact → (entries, error, meta).

    An artifact may hold more than one dataset (a report dir per project, the
    glob in the upload step catches them all). They are merged on the entry
    path, first file in sorted path order winning, exactly as the producer's own
    ``merge_timings`` merges legs — the script and notebook legs contribute
    disjoint paths, so both survive.

    A directory with no dataset and a directory whose datasets are all
    unreadable get the SAME honest error: what we have is no usable dataset
    either way, and the leg keeps its provenance so the board can say which run
    it could not read.
    """
    root = Path(directory)
    entries: dict[str, dict[str, Any]] = {}
    meta: dict[str, str] = {}
    for path in sorted(root.rglob(TIMINGS_FILENAME)):
        try:
            text = path.read_text()
        except OSError:
            continue
        data, err = parse_timings(text)
        if data is None or err:
            continue
        if not meta:
            meta = {
                "env_profile": data.get("env_profile", ""),
                "python": data.get("python", ""),
                "ts": data.get("ts", ""),
            }
        for entry in data["entries"]:
            entries.setdefault(entry["entry"], entry)
    if not entries and not meta:
        return [], NO_DATASET_ERROR, {}
    return [entries[key] for key in sorted(entries)], "", meta


def leg_counts(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-status counts + ``timed`` + ``total_s`` for one leg's entries.

    ``timed`` counts entries whose ``seconds`` is a number — the coverage
    behind ``total_s``. A ``null`` duration is not a zero-second run, so it is
    in neither.
    """
    counts: dict[str, Any] = {k: 0 for k in TRACKED_STATUSES}
    counts["timed"] = 0
    total = 0.0
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "")
        if status in counts and status != "timed":
            counts[status] += 1
        seconds = entry.get("seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            counts["timed"] += 1
            total += float(seconds)
    counts["total_s"] = round(total, 1)
    return counts


# --- prompts (written by the producer; never re-derived by a renderer) -------
def timeout_prompt(repo: str, entry: str, cap_s: Any, run_url: str) -> str:
    """The self-contained /bug prompt a TIMEOUT row copies."""
    cap = _as_float(cap_s)
    cap_text = str(int(round(cap))) if cap is not None else "?"
    return (
        f"/bug kill timer: {repo} {entry} TIMEOUT ({cap_text}s) on {run_url} "
        f"— stack tail in the run log"
    )


def slow_prompt(repo: str, entry: str, prev_s: Any, now_s: Any,
                prev_run_url: str, run_url: str) -> str:
    """The self-contained /bug prompt a slowed script copies."""
    prev = _as_float(prev_s) or 0.0
    now = _as_float(now_s) or 0.0
    return (
        f"/bug slow script: {repo} {entry} {int(round(prev))}s → {int(round(now))}s "
        f"between runs {prev_run_url} → {run_url}"
    )


# --- per-repo sidecar --------------------------------------------------------
def build_sidecar(
    name: str,
    group: str,
    owner: str,
    legs: list[dict[str, Any]],
    ts: str,
    error: str = "",
) -> dict[str, Any]:
    """Construct one repo's smoke_timings sidecar.

    ``legs`` is what the shell leg produced: one entry per selected artifact,
    ``{"artifact": <select_artifacts row>, "dir": <extracted dir or None>,
    "error": <download error or "">}``.

    ``error`` is the reason the artifacts listing could not be fetched. When
    set the sidecar carries NO legs at all: a dead query must never render as a
    repo with no slow scripts. A single leg that failed to download keeps its
    provenance (run, python, url) with its own ``error`` and no entries — one
    leg's 403 must not lose the other leg.
    """
    if error:
        return {
            "name": name, "group": group, "owner": owner, "error": error,
            "ts": ts, "legs": [],
        }

    out_legs: list[dict[str, Any]] = []
    for leg in legs or []:
        if not isinstance(leg, dict):
            continue
        art = leg.get("artifact")
        art = art if isinstance(art, dict) else {}
        run_id = art.get("run_id")
        run_url = (
            f"https://github.com/{owner}/{name}/actions/runs/{run_id}"
            if owner and name and run_id is not None
            else ""
        )
        leg_error = str(leg.get("error") or "")
        entries: list[dict[str, Any]] = []
        meta: dict[str, str] = {}
        if not leg_error:
            directory = leg.get("dir")
            if directory:
                entries, leg_error, meta = read_downloaded_leg(directory)
            else:
                leg_error = "artifact was not downloaded"
        counts = leg_counts(entries)
        total_s = counts.pop("total_s")
        out_legs.append(
            {
                "python": str(art.get("python") or meta.get("python") or ""),
                "artifact_id": art.get("id"),
                "run_id": run_id,
                "run_url": run_url,
                "head_branch": str(art.get("head_branch") or ""),
                "head_sha": str(art.get("head_sha") or ""),
                "at": str(art.get("created_at") or ""),
                "env_profile": meta.get("env_profile", ""),
                "error": leg_error,
                "entries": entries,
                "counts": counts,
                "total_s": total_s,
            }
        )
    out_legs.sort(key=lambda leg: (str(leg.get("python") or ""), str(leg.get("artifact_id") or "")))
    return {
        "name": name, "group": group, "owner": owner, "error": "",
        "ts": ts, "legs": out_legs,
    }


# --- aggregate ---------------------------------------------------------------
def prev_rows_of(prev_board: Any) -> dict[tuple[str, str, str], dict[str, Any]]:
    """``performance.scripts.rows`` out of a previously published board.json.

    Keyed by (repo, python, entry) — the identity of a script row across runs.
    Anything unexpected (a 404 page, an older board with no scripts block, a
    rows list of nulls) degrades to no previous observation: a publish gap
    costs a comparison, never a render.
    """
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not isinstance(prev_board, dict):
        return out
    perf = prev_board.get("performance")
    if not isinstance(perf, dict):
        return out
    scripts = perf.get("scripts")
    if not isinstance(scripts, dict):
        return out
    rows = scripts.get("rows")
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("repo") or ""),
            str(row.get("python") or ""),
            str(row.get("entry") or ""),
        )
        if all(key):
            out[key] = row
    return out


def classify_drift(
    seconds: Any,
    prev_row: Any,
    thresholds: dict[str, float],
    run_id: Any = None,
) -> tuple[str, float | None, float | None]:
    """(state, ratio, delta_s) — ``warn`` needs BOTH gates, else ``ok``.

    ``("ok", None, None)`` when either side is missing, or when the previous
    observation came from the SAME run: the previous board is re-read on every
    render, so a second render of the same day sees the same run's rows, and
    comparing a run against itself would report a reassuring 1.0× that means
    nothing. Never ``fail`` — a slowed script is advisory by construction.
    """
    now = _as_float(seconds)
    if not isinstance(prev_row, dict):
        return "ok", None, None
    prev = _as_float(prev_row.get("seconds"))
    if now is None or prev is None or now <= 0 or prev <= 0:
        return "ok", None, None
    prev_run = prev_row.get("run_id")
    if run_id is not None and prev_run is not None and prev_run == run_id:
        return "ok", None, None
    ratio = round(now / prev, 2)
    delta = round(now - prev, 1)
    if (ratio >= float(thresholds.get("slow_factor", 2.0))
            and delta >= float(thresholds.get("min_delta_s", 5))):
        return "warn", ratio, delta
    return "ok", ratio, delta


def aggregate(
    sidecars: list[dict[str, Any]],
    prev_board: Any,
    ts: str,
    thresholds: dict[str, float] | None = None,
    record_prev_rows: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fold the per-repo sidecars + the previous observation into the rollup.

    ``record_prev_rows`` is the COMMITTED record (``timings/scripts/``, via
    ``heart.timings.previous_script_rows``) in the same
    ``(repo, python, entry) -> row`` shape ``prev_rows_of`` returns, and is
    preferred whenever it is non-empty. The published board stays as the
    fallback: it is the same artifact this render produces, so a Pages gap
    costs the comparison, while the record does not.
    """
    thr = dict(thresholds or DEFAULT_SMOKE_TIMINGS_THRESHOLDS)
    prev_rows = dict(record_prev_rows) if record_prev_rows else prev_rows_of(prev_board)
    top_n = int(thr.get("top_n", 5) or 5)

    repos: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    def _name_of(side: Any) -> str:
        return str(side.get("name") or "") if isinstance(side, dict) else ""

    for side in sorted(sidecars or [], key=_name_of):
        if not isinstance(side, dict):
            continue
        repo = str(side.get("name") or "")
        if side.get("error"):
            errors.append({"repo": repo, "error": str(side["error"])})
            continue
        legs = side.get("legs")
        if not isinstance(legs, list):
            continue
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            python = str(leg.get("python") or "")
            run_id = leg.get("run_id")
            run_url = str(leg.get("run_url") or "")
            at = str(leg.get("at") or "")
            leg_error = str(leg.get("error") or "")
            if leg_error:
                # Provenance survives: which run we could not read, and why.
                errors.append({"repo": repo, "error": f"{python or '?'}: {leg_error}"})
            entries = [e for e in (leg.get("entries") or []) if isinstance(e, dict)]

            leg_rows: list[dict[str, Any]] = []
            for entry in entries:
                name = str(entry.get("entry") or "")
                status = str(entry.get("status") or "")
                seconds = _as_float(entry.get("seconds"))
                cap_s = _as_float(entry.get("cap_s"))
                if status == "timeout":
                    # Timed or not: a killed script is a hard row either way.
                    events.append({
                        "kind": "timeout",
                        "repo": repo,
                        "python": python,
                        "entry": name,
                        "cap_s": cap_s,
                        "run_url": run_url,
                        "at": at,
                        "prompt": timeout_prompt(repo, name, cap_s, run_url),
                    })
                if seconds is None:
                    # `null` means the entry never ran. Not a zero-second row.
                    continue
                prev_row = prev_rows.get((repo, python, name))
                state, ratio, delta = classify_drift(seconds, prev_row, thr, run_id=run_id)
                prev_s = _as_float((prev_row or {}).get("seconds"))
                prev_run_id = (prev_row or {}).get("run_id")
                prev_run_url = str((prev_row or {}).get("run_url") or "")
                leg_rows.append({
                    "repo": repo,
                    "python": python,
                    "entry": name,
                    "kind": str(entry.get("kind") or ""),
                    "status": status,
                    "seconds": seconds,
                    "cap_s": cap_s,
                    "exit_code": _as_int(entry.get("exit_code")),
                    "run_id": run_id,
                    "run_url": run_url,
                    "prev_s": prev_s,
                    "prev_run_id": prev_run_id,
                    "ratio": ratio,
                    "delta_s": delta,
                    "state": state,
                    "prompt": (slow_prompt(repo, name, prev_s, seconds,
                                           prev_run_url, run_url)
                               if state == "warn" else None),
                })

            counts = leg.get("counts") if isinstance(leg.get("counts"), dict) else {}
            timed = _as_int(counts.get("timed"))
            repos.append({
                "repo": repo,
                "python": python,
                "run_id": run_id,
                "run_url": run_url,
                "head_branch": str(leg.get("head_branch") or ""),
                # Provenance the committed record stores per observation: which
                # commit was measured, and under which environment profile.
                "head_sha": str(leg.get("head_sha") or ""),
                "env_profile": str(leg.get("env_profile") or ""),
                "at": at,
                "entries": len(entries),
                "timed": timed if timed is not None else len(leg_rows),
                "total_s": _as_float(leg.get("total_s")) or 0.0,
                "error": leg_error,
                "slowest": sorted(leg_rows, key=lambda r: r["seconds"], reverse=True)[:top_n],
            })
            rows.extend(leg_rows)

    repos.sort(key=lambda r: (str(r.get("repo") or ""), str(r.get("python") or "")))
    rows.sort(key=lambda r: (str(r.get("repo") or ""), str(r.get("python") or ""),
                             str(r.get("entry") or "")))
    return {
        "ts": ts,
        "repos": repos,
        "rows": rows,
        "slowed": [r for r in rows if r.get("state") == "warn"],
        "events": events,
        "errors": errors,
        "thresholds": thr,
    }


def read_sidecars(per_repo_dir: Path) -> list[dict[str, Any]]:
    """Every ``*.smoke_timings.json`` sidecar under ``per_repo_dir`` (I/O)."""
    out: list[dict[str, Any]] = []
    for path in sorted(Path(per_repo_dir).glob("*.smoke_timings.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def read_downloads(
    artifacts: list[dict[str, Any]], downloads_dir: Path | str | None
) -> list[dict[str, Any]]:
    """Pair each selected artifact with its extracted dir (or its error).

    The shell leg writes ``<downloads>/<id>/`` for a download that worked and
    ``<downloads>/<id>.error`` (one line of text) for one that did not, so a
    403 on one python leg still leaves the other leg's data intact.
    """
    legs: list[dict[str, Any]] = []
    root = Path(downloads_dir) if downloads_dir else None
    for art in artifacts:
        directory = None
        error = ""
        if root is None:
            error = "no downloads directory"
        else:
            err_file = root / f"{art['id']}.error"
            art_dir = root / str(art["id"])
            if err_file.is_file():
                try:
                    error = " ".join(err_file.read_text().split())[:200]
                except OSError:  # pragma: no cover - unreadable marker file
                    error = ""
                error = error or "artifact download failed"
            elif art_dir.is_dir():
                directory = art_dir
            else:
                error = "artifact download failed"
        legs.append({"artifact": art, "dir": directory, "error": error})
    return legs


# --- summary lines -----------------------------------------------------------
def repo_summary_line(sidecar: dict[str, Any]) -> str:
    """Coloured one-line per-repo summary for the daemon log."""
    from heart.heart_color import (
        c_info, c_meta, c_ok, c_warn, glyph_ok, glyph_warn,
    )

    name = sidecar.get("name", "?")
    if sidecar.get("error"):
        return (f"{glyph_warn()} {c_info(name)} {c_warn('smoke_timings UNAVAILABLE')} "
                f"{c_meta(str(sidecar['error']))}")
    legs = [leg for leg in (sidecar.get("legs") or []) if isinstance(leg, dict)]
    timed = sum(_as_int((leg.get("counts") or {}).get("timed")) or 0 for leg in legs)
    broken = [leg for leg in legs if leg.get("error")]
    if broken:
        return (f"{glyph_warn()} {c_info(name)} "
                f"{c_warn(f'{len(broken)} leg(s) unavailable')} "
                f"{c_meta(f'{timed} script(s) timed')}")
    return (f"{glyph_ok()} {c_info(name)} {c_ok(f'{timed} script(s) timed')} "
            f"{c_meta(f'{len(legs)} leg(s)')}")


def summary_line(rollup: dict[str, Any]) -> str:
    """Coloured one-line summary of the global rollup."""
    from heart.heart_color import (
        c_info, c_meta, c_ok, c_warn, glyph_ok, glyph_warn,
    )

    rows = rollup.get("rows") or []
    repos = {str(r.get("repo") or "") for r in (rollup.get("repos") or [])
             if isinstance(r, dict)}
    slowed = len(rollup.get("slowed") or [])
    events = len(rollup.get("events") or [])
    errors = len(rollup.get("errors") or [])
    body = (f"{len(rows)} scripts timed across {len(repos)} repos, "
            f"{slowed} slowed, {events} timeouts")
    if errors:
        body += f", {errors} unavailable"
    legs = len(rollup.get("repos") or [])
    glyph = glyph_warn() if (slowed or events or errors) else glyph_ok()
    tint = c_warn if (slowed or events or errors) else c_ok
    return (f"{glyph} {c_info('smoke_timings:')} {tint(body)} "
            f"{c_meta(f'({legs} leg(s))')}")


# --- I/O shell ---------------------------------------------------------------
def _write(out_path: Path, payload: dict[str, Any]) -> None:
    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(out_path, payload)


def _read_listing(path: str) -> tuple[Any, str]:
    """The artifacts listing file → (payload, error)."""
    try:
        return json.loads(Path(path).read_text()), ""
    except (OSError, ValueError):
        return None, "artifacts listing was not valid JSON"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="heart.checks.smoke_timings")
    ap.add_argument("--plan", action="store_true",
                    help="read an artifacts listing on stdin and print the "
                         "'<id> <name>' lines the shell leg should download")
    ap.add_argument("--aggregate", action="store_true",
                    help="fold the per-repo sidecars into the global rollup")
    ap.add_argument("--name", default="")
    ap.add_argument("--group", default="")
    ap.add_argument("--owner", default="")
    ap.add_argument("--ts", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--listing", default="",
                    help="file holding the artifacts listing JSON (per-repo mode)")
    ap.add_argument("--downloads", default="",
                    help="dir holding <artifact id>/ extractions and "
                         "<artifact id>.error markers (per-repo mode)")
    ap.add_argument("--prev-board", default="",
                    help="previously published board.json (aggregate mode); "
                         "missing/unreadable => no comparison, never an error")
    ap.add_argument("--per-repo-dir", default="",
                    help="where the sidecars live (default: $HEART_STATE_DIR/per-repo)")
    ap.add_argument("--record-dir", default="",
                    help="the committed timing record (default: HEART_HOME/timings); "
                         "its scripts/ files are the preferred previous "
                         "observation, the previous board.json only the fallback")
    ap.add_argument("--fetch-error", default="",
                    help="reason the artifacts listing fetch failed; recorded "
                         "instead of a bogus quiet repo")
    ns = ap.parse_args(argv)

    if ns.plan:
        try:
            listing: Any = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            # Nothing to download, and nothing to say: the per-repo call reads
            # the same listing file and records the failure honestly there.
            return 0
        for art in select_artifacts(listing):
            print(f"{art['id']} {art['name']}")
        return 0

    if not ns.out:
        ap.error("--out is required outside --plan mode")
    ts = ns.ts or datetime.datetime.now(datetime.timezone.utc).isoformat()

    if ns.aggregate:
        sys.path.insert(0, str(HEART_HOME))
        from heart import state as _state
        from heart import timings as _timings

        per_repo = Path(ns.per_repo_dir) if ns.per_repo_dir else _state.HEART_PER_REPO_DIR
        record_dir = Path(ns.record_dir) if ns.record_dir else _timings.TIMINGS_DIR
        rollup = aggregate(
            read_sidecars(per_repo), read_prev_board(ns.prev_board), ts,
            load_thresholds(),
            record_prev_rows=_timings.previous_script_rows(record_dir / "scripts"),
        )
        _write(Path(ns.out), rollup)
        print(summary_line(rollup))
        return 0

    if not ns.name or not ns.group:
        ap.error("--name and --group are required outside --aggregate mode")

    error = ns.fetch_error
    legs: list[dict[str, Any]] = []
    if not error:
        listing, listing_error = _read_listing(ns.listing)
        error = listing_error
        if not error:
            legs = read_downloads(select_artifacts(listing), ns.downloads or None)

    sidecar = build_sidecar(ns.name, ns.group, ns.owner, legs, ts, error=error)
    _write(Path(ns.out), sidecar)
    print(repo_summary_line(sidecar))
    return 0


if __name__ == "__main__":
    sys.exit(main())
