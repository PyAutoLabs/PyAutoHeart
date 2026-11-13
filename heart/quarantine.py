"""heart/quarantine.py — the expiring Stage 3 script quarantine (PyAutoHeart#344).

Human ruling, M1 gate 2026-11-12: a failing Stage 3 (release-validation)
workspace script may be set aside, so one script does not hold the release
RED, under this doctrine:

- an entry needs a **filed bug task** (``bug``: an issue ref);
- it **expires** at most 7 days after it was filed;
- at most **3** entries are active at once;
- while any entry is active, readiness shows **YELLOW** with the list;
- an **expired** entry fails the gate again (RED), and its script runs again;
- quarantine never turns RED into GREEN: the script is only skipped, and the
  verdict it leaves is YELLOW at best. Release approval is unchanged.

Workspace scripts only — never unit tests or install checks: ``workspace``
must be a polled release-surface workspace and ``script`` a ``.py`` path
inside it.

The file is ``config/stage3_quarantine.yaml`` (schema in its header). This
module is the one reader: ``classify`` sorts entries into active / expired /
refused, ``readiness`` turns that into YELLOW / RED reasons, and the
``skip-list`` CLI hands Stage 3 (``workspace-validation.yml``, mode=release)
the ACTIVE entries for one workspace, which PyAutoHands' runner skips and
records. Pure stdlib + PyYAML: Stage 3 runs it with nothing else installed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

HEART_HOME = Path(__file__).resolve().parents[1]
QUARANTINE_PATH = HEART_HOME / "config" / "stage3_quarantine.yaml"
REPOS_PATH = HEART_HOME / "config" / "repos.yaml"

#: The ruling's numbers.
MAX_ACTIVE = 3
MAX_DAYS = 7

#: Groups in ``config/repos.yaml`` whose scripts Stage 3 runs.
STAGE3_GROUPS = ("workspaces", "workspaces_test", "howto", "pipelines")

#: A filed bug task: ``Repo#N``, ``Owner/Repo#N`` or a GitHub issue URL.
BUG_REF = re.compile(
    r"^(?:[\w.-]+/)?[\w.-]+#\d+$|^https://github\.com/[\w.-]+/[\w.-]+/issues/\d+$")


def stage3_workspaces(repos_path: Path | str = REPOS_PATH) -> list[str]:
    """The workspaces whose scripts Stage 3 runs (the only quarantinable ones)."""
    cfg = yaml.safe_load(Path(repos_path).read_text()) or {}
    groups = cfg.get("repos") or {}
    return sorted(r["name"] for g in STAGE3_GROUPS for r in groups.get(g) or []
                  if isinstance(r, dict) and r.get("name"))


def load(path: Path | str | None = None) -> list[Any]:
    """The raw ``entries:`` list (default: ``QUARANTINE_PATH``). A missing file
    is no quarantine; a file whose ``entries`` is not a list raises — a
    malformed policy is never "empty"."""
    p = Path(path if path is not None else QUARANTINE_PATH)
    if not p.exists():
        return []
    cfg = yaml.safe_load(p.read_text()) or {}
    entries = cfg.get("entries")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError(f"{p}: entries must be a list")
    return entries


def _date(v: Any) -> datetime.date | None:
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    try:
        return datetime.date.fromisoformat(str(v).strip())
    except ValueError:
        return None


def _label(e: Any) -> str:
    if not isinstance(e, dict):
        return repr(e)
    return f"{e.get('workspace', '?')}/{e.get('script', '?')}"


def _schema_error(e: Any, workspaces: set[str], today: datetime.date) -> str | None:
    """Why an entry is refused on its own merits, or None."""
    if not isinstance(e, dict):
        return "not a mapping"
    bug = str(e.get("bug") or "").strip()
    if not bug:
        return "no bug-task ref (a quarantine needs a filed bug task)"
    if not BUG_REF.match(bug):
        return f"bug ref {bug!r} is not an issue ref (Repo#N, Owner/Repo#N or issue URL)"
    ws = str(e.get("workspace") or "")
    if ws not in workspaces:
        return (f"workspace {ws!r} is not a Stage 3 workspace "
                "(scripts only — never unit tests or install checks)")
    script = str(e.get("script") or "")
    if not script.endswith(".py") or script.startswith("/") or ".." in script.split("/") \
            or "/" not in script:
        return f"script {script!r} is not a .py path inside the workspace"
    filed, expires = _date(e.get("filed")), _date(e.get("expires"))
    if filed is None:
        return "no valid filed date (YYYY-MM-DD)"
    if expires is None:
        return "no valid expires date (YYYY-MM-DD)"
    if filed > today:
        return f"filed {filed.isoformat()} is in the future"
    if expires <= filed:
        return "expires is not after filed"
    if (expires - filed).days > MAX_DAYS:
        return f"expires {(expires - filed).days} days after filed (at most {MAX_DAYS})"
    return None


def classify(
    entries: list[Any],
    today: datetime.date,
    workspaces: list[str] | set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Sort entries into ``active`` / ``expired`` / ``refused``.

    Each row is ``{"label", "entry", "why"}`` (``why`` on refused/expired).
    Refused: a schema error (no bug ref, a non-workspace target, a window over
    7 days...), or a valid live entry beyond the first ``MAX_ACTIVE`` by
    ``filed`` date (file order breaks ties) — the fourth entry is refused, the
    three before it stand. Expired: ``today`` is after ``expires`` (an entry is
    live through its expiry day). A refused or expired entry is never skipped
    by Stage 3."""
    ws = set(workspaces) if workspaces is not None else set(stage3_workspaces())
    active: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    live: list[tuple[datetime.date, int, dict[str, Any]]] = []
    for i, e in enumerate(entries):
        why = _schema_error(e, ws, today)
        if why:
            refused.append({"label": _label(e), "entry": e, "why": why})
            continue
        expires = _date(e["expires"])
        if today > expires:
            expired.append({"label": _label(e), "entry": e,
                            "why": f"expired {expires.isoformat()}"})
            continue
        live.append((_date(e["filed"]), i, e))
    for n, (_, _, e) in enumerate(sorted(live, key=lambda t: (t[0], t[1]))):
        row = {"label": _label(e), "entry": e}
        if n < MAX_ACTIVE:
            active.append(row)
        else:
            refused.append({**row, "why": f"{MAX_ACTIVE} entries already active"})
    return {"active": active, "expired": expired, "refused": refused}


def readiness_reasons(
    entries: list[Any], today: datetime.date, workspaces: list[str] | set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """``(red, yellow)`` reason sentences for the readiness verdict."""
    c = classify(entries, today, workspaces)
    red = [f"Stage 3 quarantine EXPIRED: {r['label']} ({r['entry'].get('bug')}, "
           f"{r['why']}) — the script gates again" for r in c["expired"]]
    red += [f"Stage 3 quarantine entry refused: {r['label']} — {r['why']}"
            for r in c["refused"]]
    yellow = [f"Stage 3 quarantine active ({len(c['active'])}/{MAX_ACTIVE}): {r['label']} "
              f"({r['entry'].get('bug')}, expires {_date(r['entry']['expires'])})"
              for r in c["active"]]
    return red, yellow


def skip_list(workspace: str, entries: list[Any], today: datetime.date,
              workspaces: list[str] | set[str] | None = None) -> dict[str, Any]:
    """The ACTIVE entries for one workspace, as PyAutoHands' runner reads them
    (``run_python.py --quarantine``): ``script`` is the workspace-relative
    path pattern, matched as a path substring."""
    c = classify(entries, today, workspaces)
    return {"workspace": workspace, "date": today.isoformat(), "entries": [
        {"script": str(r["entry"]["script"]), "bug": str(r["entry"]["bug"]),
         "expires": str(_date(r["entry"]["expires"]))}
        for r in c["active"] if r["entry"].get("workspace") == workspace]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m heart.quarantine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check", help="validate the file; exit 1 on a refused entry")
    chk.add_argument("--file", default=None)
    sk = sub.add_parser("skip-list", help="the ACTIVE entries for one workspace, as JSON")
    sk.add_argument("--workspace", required=True)
    sk.add_argument("--file", default=None)
    sk.add_argument("--out", default="-")
    for p in (chk, sk):
        p.add_argument("--date", default=None, help="judge as of YYYY-MM-DD (default today, UTC)")
    ns = ap.parse_args(argv)
    today = (datetime.date.fromisoformat(ns.date) if ns.date
             else datetime.datetime.now(datetime.timezone.utc).date())
    entries = load(ns.file)
    if ns.cmd == "check":
        c = classify(entries, today)
        for r in c["active"]:
            print(f"active   {r['label']} ({r['entry'].get('bug')})")
        for r in c["expired"]:
            print(f"EXPIRED  {r['label']} — {r['why']} (gates RED until removed)")
        for r in c["refused"]:
            print(f"REFUSED  {r['label']} — {r['why']}")
        return 1 if c["refused"] else 0
    out = json.dumps(skip_list(ns.workspace, entries, today), indent=2)
    if ns.out == "-":
        print(out)
    else:
        Path(ns.out).write_text(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
