"""heart/checks/rehearsal_ancestry.py — is each rehearsed SHA still on ``main``?

M2.4 (PyAutoHands#337, human ruling 2026-11-12): a release publishes EXACTLY
the commits the rehearsal built, even when ``main`` has moved on since. So a
release-validation report whose ``commit_shas`` differ from the current ``main``
HEADs is not stale merely because ``main`` moved: it is still evidence for what
will ship, provided each rehearsed commit is an ANCESTOR of its repo's ``main``
(main moved forward). It is stale when a rehearsed commit is not on ``main``
(main rewritten, or an off-main commit was rehearsed), or when that cannot be
established.

This check records the fact; ``heart/readiness.py`` reads it (``compute`` stays
a pure function of the snapshot). For each release-gate library whose rehearsed
SHA differs from the ``main`` HEAD that ``ci_status`` recorded, it asks GitHub's
compare API (``gh api repos/<owner>/<repo>/compare/<rehearsed>...<head>``):
``status`` ``ahead`` or ``identical`` means the rehearsed commit is an ancestor
of HEAD, and ``ahead_by`` is how many commits ``main`` is ahead. Anything else
(``behind``, ``diverged``, an error) is recorded as not proven.

Writes ``$HEART_STATE_DIR/rehearsal_ancestry.json``; ``state.aggregate`` folds
it into the snapshot as ``rehearsal_ancestry``. ``collect`` is pure apart from
the injected ``api`` callable (the gh seam), so tests stay offline.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

import yaml

from heart import state

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "repos.yaml"
SIDECAR = "rehearsal_ancestry.json"
#: Compare statuses under which the base (rehearsed) commit is an ancestor of head.
ANCESTOR_STATUSES = frozenset({"ahead", "identical"})
#: Per-call gh timeout, and the total wall-clock budget for all compare calls
#: (they run in parallel). A call still out at the budget is recorded as
#: unknown — readiness reads that as unproven, i.e. STALE, never as fresh.
CALL_TIMEOUT_S = 15
TOTAL_BUDGET_S = 20.0


def gh_api(path: str) -> dict:
    """The gh seam: ``gh api <path>`` as a dict, ``{}`` on any failure."""
    try:
        result = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                                timeout=CALL_TIMEOUT_S)
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def library_owners(config_path: Path | str = CONFIG_PATH) -> dict[str, str]:
    """Release-gate library -> GitHub owner, from the policy file."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    return {
        r["name"]: r["owner"]
        for r in (cfg.get("repos") or {}).get("libraries") or []
        if isinstance(r, dict) and r.get("name") and r.get("owner")
        and r.get("release_gate", True)
    }


def ancestry(owner: str, repo: str, rehearsed: str, head: str,
             api: Callable[[str], dict]) -> dict[str, Any]:
    """One repo's answer: is ``rehearsed`` an ancestor of ``head``?"""
    data = api(f"repos/{owner}/{repo}/compare/{rehearsed}...{head}")
    status = str(data.get("status") or "")
    row: dict[str, Any] = {"rehearsed": rehearsed, "head": head,
                           "status": status or "unknown",
                           "ancestor": status in ANCESTOR_STATUSES}
    if status in ANCESTOR_STATUSES:
        try:
            row["ahead_by"] = int(data.get("ahead_by", 0))
        except (TypeError, ValueError):
            row["ahead_by"] = None
    if not status:
        row["error"] = "compare unavailable"
    return row


def collect(snapshot: dict, owners: dict[str, str], api: Callable[[str], dict],
            now: datetime.datetime | None = None,
            budget_s: float = TOTAL_BUDGET_S) -> dict[str, Any]:
    """The sidecar for ``snapshot``: one row per library whose rehearsed SHA
    differs from its recorded ``main`` HEAD. Libraries that match need no row.
    The compare calls run in parallel under one total ``budget_s``; a call not
    back in time is a row with status ``unknown`` (error ``timeout``)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    report = snapshot.get("validation_report") or {}
    shas = report.get("commit_shas") if isinstance(report, dict) else None
    repos = snapshot.get("repos") or {}
    todo: dict[str, tuple[str, str, str]] = {}
    for lib, owner in owners.items():
        want = str((shas or {}).get(lib) or "")
        head = str((((repos.get(lib) or {}).get("ci_status")) or {}).get("head_sha") or "")
        if not want or not head:
            continue
        n = min(len(want), len(head))
        if n >= 7 and want[:n].lower() == head[:n].lower():
            continue  # rehearsed == HEAD: nothing to prove
        todo[lib] = (owner, want, head)
    rows: dict[str, Any] = {}
    if todo:
        pool = ThreadPoolExecutor(max_workers=len(todo))
        futures = {lib: pool.submit(ancestry, o, lib, w, h, api)
                   for lib, (o, w, h) in todo.items()}
        wait(futures.values(), timeout=budget_s)
        for lib, fut in futures.items():
            owner, want, head = todo[lib]
            if fut.done() and fut.exception() is None:
                rows[lib] = fut.result()
            else:
                rows[lib] = {"rehearsed": want, "head": head, "status": "unknown",
                             "ancestor": False,
                             "error": "timeout" if not fut.done() else "compare failed"}
        pool.shutdown(wait=False, cancel_futures=True)
    return {"checked_at": now.isoformat(), "report_ts": (report or {}).get("ts"),
            "repos": rows}


def read_inputs(state_dir: Path | None = None) -> dict[str, Any]:
    """The two inputs, read straight from this tick's sidecars — the
    validation report and each library's ci_status — WITHOUT aggregating
    (the tick's own aggregate, at its end, folds this check's sidecar in)."""
    base = state_dir or state.HEART_STATE_DIR
    per_repo = base / "per-repo"

    def read(path: Path) -> dict:
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    repos = {}
    for f in sorted(per_repo.glob("*.ci_status.json")) if per_repo.is_dir() else ():
        repos[f.name.split(".")[0]] = {"ci_status": read(f)}
    return {"validation_report": read(base / "validation_report.json"), "repos": repos}


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    sidecar = collect(read_inputs(), library_owners(), gh_api)
    state.atomic_write_json(state.HEART_STATE_DIR / SIDECAR, sidecar)
    print(f"rehearsal_ancestry: {len(sidecar['repos'])} moved repo(s) checked "
          f"in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
