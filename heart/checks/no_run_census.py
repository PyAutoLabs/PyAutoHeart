"""heart/checks/no_run_census.py — the census of skipped workspace scripts.

Every workspace carries a ``config/build/no_run.yaml``: the list of scripts the
release run skips. Three marker tiers are established convention, written as a
trailing comment on the entry::

    - path/to/script.py   # SLOW 2026-07-14 - takes 233.7s, over the 300s cap
    - path/to/other.py    # NEEDS_FIX 2026-07-14 - raises on the new API
    - path/to/generator.py                        # (untagged: correct by design)

The board treats these as *claims with timestamps, not facts*, because the
record says so repeatedly: "a SLOW marker is not evidence of slowness" (the
first SLOW-marked entry ever measured was wrong by ~50×), "a NEEDS_FIX marker is
a claim with a timestamp" (a census where the marker had already evaporated),
and a purge that found 41% of entries dead. So the census does three things a
plain YAML load cannot:

1. **Counts the tiers**, so the collapsed "permanent" bulk stays out of the way
   while the to-do rows surface.
2. **Flags unmeasured SLOW rows** — a SLOW claim whose reason carries no actual
   seconds figure has never been measured against the real cap, and is the
   single most likely row to be wrong.
3. **Carries a ready-to-paste prompt on every actionable row**, so acting on one
   is a paste rather than an archaeology session.

**Parsed line-based, never by ``yaml.safe_load`` alone.** Two recorded traps
force this: the markers live in ``#`` comments, which a YAML load discards
outright; and a bare ``- off`` / ``- on`` / ``- yes`` entry parses as a YAML
*boolean*, which crashed ``should_skip`` in production. Reading the raw text
line by line sees the comments, and treats ``off`` as the five-letter path it
actually is.

**The measured heuristic, precisely.** A SLOW reason counts as measured iff it
carries a seconds figure that is NOT a mention of the cap: ``"takes 233.7s"`` is
a measurement, ``"flakes at the 1800s cap"`` is not — it names the limit the
script hit, which is exactly the "no measurement" case this check exists to
find. Hence the negative lookahead on ``cap`` in :data:`SECONDS_RE`.

Per-repo sidecar (``<name>.no_run_census.json``)::

    {"name", "group", "present", "slow", "needs_fix", "permanent",
     "unmeasured_slow", "rows": [{entry, marker, date, reason, measured, prompt}],
     "ts"}

``present: false`` is honest data, not an error: at least one workspace_test repo
genuinely has no ``no_run.yaml`` at all.

Global rollup (``no_run_census.json``): ``{"ts", "totals", "repos", "rows"}``,
where ``rows`` is the actionable set only (unmeasured SLOW first, then
NEEDS_FIX, then measured SLOW), worst first, each with its repo and prompt.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any

HEART_HOME = Path(__file__).resolve().parents[2]

# `- <entry>` with an optional trailing `# comment`. Entries are single tokens
# (paths / globs), which is what every real no_run.yaml carries.
ENTRY_RE = re.compile(r"^\s*-\s*(?P<entry>\S+)\s*(?:#\s*(?P<comment>.*))?$")

# The marker grammar inside the comment: a tier, an optional ISO date, an
# optional dash (ASCII or en dash), then free-text reason.
MARKER_RE = re.compile(
    r"^(?P<marker>SLOW|NEEDS_FIX)\s*(?P<date>\d{4}-\d{2}-\d{2})?\s*[-–]?\s*(?P<reason>.*)$"
)

# A real seconds figure — but NOT one that merely names the cap. "233.7s" is a
# measurement; "the 1800s cap" is the limit the script hit, which is precisely
# the unmeasured case.
SECONDS_RE = re.compile(r"\d+(?:\.\d+)?\s*s\b(?!\s*cap)", re.IGNORECASE)

PERMANENT = "permanent"
SLOW = "SLOW"
NEEDS_FIX = "NEEDS_FIX"

# How much of a marker reason a prompt carries. Long enough to be the whole
# reason in practice, short enough that a pathological comment cannot bloat the
# board's JSON.
REASON_LIMIT = 120

# Worst-first ordering for the actionable rows: an unmeasured SLOW claim is the
# most likely to be simply wrong, a NEEDS_FIX is a to-do with a timestamp, and a
# measured SLOW is at least honest about its own cost.
ROW_RANK = {("SLOW", False): 0, ("NEEDS_FIX", False): 1, ("NEEDS_FIX", True): 1,
            ("SLOW", True): 2}


def is_measured(reason: str) -> bool:
    """True iff ``reason`` carries a real seconds figure (not a cap mention)."""
    return bool(SECONDS_RE.search(reason or ""))


def _truncate(text: str, limit: int = REASON_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def row_prompt(repo: str, entry: str, marker: str, date: str, reason: str,
               measured: bool) -> str | None:
    """The ready-to-paste /bug prompt for one row (None for permanent skips)."""
    when = date or "unknown date"
    if marker == SLOW and not measured:
        return (f"/bug no_run: {repo} {entry} SLOW since {when} with no measurement — "
                f"retime against the real cap, then fix it or delete the marker")
    if marker == SLOW:
        return f"/bug no_run: {repo} {entry} SLOW since {when} — {_truncate(reason)}"
    if marker == NEEDS_FIX:
        return (f"/bug no_run: {repo} {entry} NEEDS_FIX since {when} — "
                f"{_truncate(reason)}. Reproduce before fixing: stale markers have "
                f"evaporated before")
    return None


def parse_no_run(text: str, repo: str) -> list[dict[str, Any]]:
    """Parse a raw ``no_run.yaml`` into rows. Line-based, comments preserved.

    Never ``yaml.safe_load``: the markers live in the comments a YAML load
    throws away, and a bare ``- off`` entry would come back as the boolean
    ``False`` rather than the path it is.
    """
    rows: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = ENTRY_RE.match(line)
        if not m:
            continue
        entry = m.group("entry").strip().strip("'\"")
        if not entry:
            continue
        comment = (m.group("comment") or "").strip()
        marker, date, reason = PERMANENT, "", ""
        mm = MARKER_RE.match(comment) if comment else None
        if mm:
            marker = mm.group("marker")
            date = mm.group("date") or ""
            reason = (mm.group("reason") or "").strip()
        measured = marker == SLOW and is_measured(reason)
        rows.append({
            "entry": entry,
            "marker": marker,
            "date": date,
            "reason": reason,
            "measured": measured,
            "prompt": row_prompt(repo, entry, marker, date, reason, measured),
        })
    return rows


def build_sidecar(
    name: str, group: str, text: str, ts: str, present: bool = True
) -> dict[str, Any]:
    """One repo's census sidecar. ``present=False`` => no ``no_run.yaml`` at all."""
    rows = parse_no_run(text, name) if present else []
    slow = [r for r in rows if r["marker"] == SLOW]
    return {
        "name": name,
        "group": group,
        "present": bool(present),
        "slow": len(slow),
        "needs_fix": sum(1 for r in rows if r["marker"] == NEEDS_FIX),
        "permanent": sum(1 for r in rows if r["marker"] == PERMANENT),
        "unmeasured_slow": sum(1 for r in slow if not r["measured"]),
        "rows": rows,
        "ts": ts,
    }


def _row_sort_key(row: dict[str, Any]) -> tuple:
    rank = ROW_RANK.get((row.get("marker"), bool(row.get("measured"))), 3)
    return (rank, str(row.get("repo") or ""), str(row.get("entry") or ""))


def aggregate(sidecars: list[dict[str, Any]], ts: str) -> dict[str, Any]:
    """Fold the per-repo sidecars into the global census rollup."""
    repos: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    totals = {"slow": 0, "needs_fix": 0, "permanent": 0, "unmeasured_slow": 0,
              "repos": 0, "repos_present": 0}

    for side in sorted(sidecars, key=lambda s: str(s.get("name") or "")):
        if not isinstance(side, dict):
            continue
        repo = str(side.get("name") or "")
        totals["repos"] += 1
        present = bool(side.get("present"))
        if present:
            totals["repos_present"] += 1
        for key in ("slow", "needs_fix", "permanent", "unmeasured_slow"):
            totals[key] += int(side.get(key) or 0)
        repos.append({
            "repo": repo,
            "present": present,
            "slow": int(side.get("slow") or 0),
            "needs_fix": int(side.get("needs_fix") or 0),
            "permanent": int(side.get("permanent") or 0),
            "unmeasured_slow": int(side.get("unmeasured_slow") or 0),
        })
        # Only the actionable tiers travel into the rollup; the permanent bulk
        # is a count, collapsed, exactly as the board renders it.
        for row in side.get("rows") or []:
            if isinstance(row, dict) and row.get("marker") in (SLOW, NEEDS_FIX):
                rows.append(dict(row, repo=repo))

    rows.sort(key=_row_sort_key)
    return {"ts": ts, "totals": totals, "repos": repos, "rows": rows}


def read_sidecars(per_repo_dir: Path) -> list[dict[str, Any]]:
    """Every ``*.no_run_census.json`` sidecar under ``per_repo_dir`` (I/O)."""
    out: list[dict[str, Any]] = []
    for path in sorted(Path(per_repo_dir).glob("*.no_run_census.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


# --- summary lines -----------------------------------------------------------
def repo_summary_line(sidecar: dict[str, Any]) -> str:
    """Coloured one-line per-repo summary for the daemon log."""
    from heart.heart_color import c_info, c_meta, c_ok, c_warn, glyph_info, glyph_ok, glyph_warn

    name = sidecar.get("name", "?")
    if not sidecar.get("present"):
        return f"{glyph_info()} {c_info(name)} {c_meta('no no_run.yaml')}"
    unmeasured = int(sidecar.get("unmeasured_slow") or 0)
    needs_fix = int(sidecar.get("needs_fix") or 0)
    body = (f"{sidecar.get('slow', 0)} SLOW ({unmeasured} unmeasured) / "
            f"{needs_fix} NEEDS_FIX / {sidecar.get('permanent', 0)} permanent")
    if unmeasured or needs_fix:
        return f"{glyph_warn()} {c_info(name)} {c_warn(body)}"
    return f"{glyph_ok()} {c_info(name)} {c_ok(body)}"


def summary_line(rollup: dict[str, Any]) -> str:
    """Coloured one-line summary of the global census."""
    from heart.heart_color import c_info, c_meta, c_ok, c_warn, glyph_ok, glyph_warn

    t = rollup.get("totals") or {}
    unmeasured = int(t.get("unmeasured_slow") or 0)
    needs_fix = int(t.get("needs_fix") or 0)
    body = (f"{t.get('slow', 0)} SLOW ({unmeasured} unmeasured) / "
            f"{needs_fix} NEEDS_FIX / {t.get('permanent', 0)} permanent")
    tail = c_meta(f"across {t.get('repos_present', 0)} workspaces")
    if unmeasured or needs_fix:
        return f"{glyph_warn()} {c_info('no_run_census:')} {c_warn(body)} {tail}"
    return f"{glyph_ok()} {c_info('no_run_census:')} {c_ok(body)} {tail}"


# --- I/O shell ---------------------------------------------------------------
def _write(out_path: Path, payload: dict[str, Any]) -> None:
    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(out_path, payload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="heart.checks.no_run_census")
    ap.add_argument("--aggregate", action="store_true",
                    help="fold the per-repo sidecars into the global census")
    ap.add_argument("--name", default="")
    ap.add_argument("--group", default="")
    ap.add_argument("--ts", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--missing", action="store_true",
                    help="this repo has no config/build/no_run.yaml (honest data, not an error)")
    ap.add_argument("--per-repo-dir", default="",
                    help="where the sidecars live (default: $HEART_STATE_DIR/per-repo)")
    ns = ap.parse_args(argv)

    ts = ns.ts or datetime.datetime.now(datetime.timezone.utc).isoformat()

    if ns.aggregate:
        sys.path.insert(0, str(HEART_HOME))
        from heart import state as _state

        per_repo = Path(ns.per_repo_dir) if ns.per_repo_dir else _state.HEART_PER_REPO_DIR
        rollup = aggregate(read_sidecars(per_repo), ts)
        _write(Path(ns.out), rollup)
        print(summary_line(rollup))
        return 0

    if not ns.name:
        ap.error("--name is required outside --aggregate mode")

    text = "" if ns.missing else sys.stdin.read()
    sidecar = build_sidecar(ns.name, ns.group, text, ts, present=not ns.missing)
    _write(Path(ns.out), sidecar)
    print(repo_summary_line(sidecar))
    return 0


if __name__ == "__main__":
    sys.exit(main())
