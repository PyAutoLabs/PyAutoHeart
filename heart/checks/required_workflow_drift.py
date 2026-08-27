"""heart/checks/required_workflow_drift.py — required workflows with no file.

``config/repos.yaml`` declares ``required_workflows`` per group, and
``heart/checks/ci_status.py``'s ``rollup()`` scores a repo over exactly those
workflows. A required workflow that has **no runs at all** is not scored as a
failure — it simply never satisfies ``all_green``, so the repo sits at
``{"conclusion": "", "status": "in_progress"}`` forever: able to go red, never
able to go green, and never CI-clean to the readiness gate.

On the dashboard that is indistinguishable from *a run is in flight*, which is
why it survives: one workspace repo sat in it from whenever it joined its group
until 2026-08-24, when a human noticed the asymmetry (a *red* required workflow
still reported failure correctly, so the repo could go red but never green).
This module makes that state nameable.

**What it can and cannot see.** The runs payload ``ci_status`` already fetches
cannot distinguish the two causes of "no runs" — a workflow file that does not
exist, and a workflow file that exists but has never run on ``main``. Only the
workflow *list* (``GET /repos/{owner}/{repo}/actions/workflows``) can, so that
is the one extra call this check makes: one per repo in a group that has
``required_workflows``, in parallel, the same cheap metadata read (and the same
shape) as the two ``ci_status.sh`` already makes per repo.

Matching is on each workflow's ``name`` field, never its filename. ``name`` is
what ``ci_status`` matches runs against, so a filename-based check could pass
while the roll-up still starves.

Classification: a missing file is **YELLOW**, and a *configuration* finding
rather than red CI — the repo's code is fine, its gate is not wired up; colouring
it red misattributes the fault. Same monitoring-not-gating stance as
``manifest_drift``, whose shape this mirrors.

Degradation is honest, never a hollow green: no ``gh`` (the web/mobile session)
or a failed fetch reports ``available: false`` / a per-repo ``error``, the same
distinction ``ci_status.sh`` draws between "no runs" and "the query broke".

The result lands at ``$HEART_STATE_DIR/required_workflow_drift.json``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"
HEART_STATE_DIR = Path(
    os.environ.get("HEART_STATE_DIR") or Path.home() / ".pyauto-heart"
)

# Seconds any single workflow-list fetch may take. The repos are scanned in
# parallel, so the worst case this adds to a tick is one timeout rather than one
# per repo — but it is capped anyway so a stalled network can never eat the
# <30s tick budget. Same reasoning as ci_status.sh's ls-remote bound.
FETCH_TIMEOUT_S = int(os.environ.get("HEART_WORKFLOW_LIST_TIMEOUT", "10"))

# Parallel fetches. Matches the order of the repo count (17 today across the
# four groups that gate), so in practice every repo is in flight at once.
MAX_WORKERS = 8


def polled_repos(config_path: Path | str = CONFIG_PATH) -> list[dict[str, str]]:
    """Return ``{owner, name, group}`` for every repo in ``repos:``.

    Groups are returned as declared; the caller decides which of them gate.
    """
    import yaml

    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    out: list[dict[str, str]] = []
    for group, entries in (cfg.get("repos") or {}).items():
        for repo in entries or []:
            if isinstance(repo, dict) and repo.get("name") and repo.get("owner"):
                out.append(
                    {
                        "owner": str(repo["owner"]),
                        "name": str(repo["name"]),
                        "group": str(group),
                    }
                )
    return out


def gating_repos(config_path: Path | str = CONFIG_PATH) -> list[dict[str, Any]]:
    """Polled repos whose group declares ``required_workflows``, with that list.

    A group with no required workflows is *advisory* — ``rollup()`` reports its
    newest run rather than gating on a set — so there is nothing here to be
    missing, and nothing to fetch for it.
    """
    sys.path.insert(0, str(HEART_HOME))
    from heart.checks.ci_status import load_required_workflows

    required = load_required_workflows(config_path)
    rows = []
    for repo in polled_repos(config_path):
        want = required.get(repo["group"])
        if want:
            rows.append({**repo, "required": list(want)})
    return rows


def fetch_workflow_names(owner_name: str) -> tuple[list[str] | None, str]:
    """Return this repo's workflow ``name`` fields, or ``(None, error)``.

    ``per_page=100`` rather than ``--paginate``: no PyAuto repo is near 100
    workflow files, and an unpaginated single call keeps this one cheap metadata
    read per repo — the cost argument the whole check rests on.
    """
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{owner_name}/actions/workflows?per_page=100",
            "--jq",
            ".workflows[].name",
        ],
        capture_output=True,
        text=True,
        timeout=FETCH_TIMEOUT_S,
    )
    if proc.returncode != 0:
        # Collapse gh's stderr to one line so it fits the sidecar and the log.
        err = " ".join((proc.stderr or "").split())[:200]
        return None, err or f"gh api exited {proc.returncode}"
    return [line for line in proc.stdout.splitlines() if line.strip()], ""


def check_one(repo: dict[str, Any]) -> dict[str, Any]:
    """Resolve one repo's required-vs-present workflow names."""
    owner_name = f"{repo['owner']}/{repo['name']}"
    try:
        names, error = fetch_workflow_names(owner_name)
    except subprocess.TimeoutExpired:
        names, error = None, f"timed out after {FETCH_TIMEOUT_S}s"
    except OSError as exc:  # gh vanished between the probe and the call
        names, error = None, str(exc)[:200]

    row: dict[str, Any] = {
        "name": repo["name"],
        "group": repo["group"],
        "required": list(repo["required"]),
        "error": error,
    }
    if names is None:
        row["present"] = []
        row["missing"] = []
        return row
    present = sorted(set(names))
    row["present"] = present
    row["missing"] = [wf for wf in repo["required"] if wf not in set(names)]
    return row


def run(config_path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    """Check every gating repo, write the sidecar, return the result."""
    if shutil.which("gh") is None:
        result: dict[str, Any] = {
            "available": False,
            "reason": "gh not installed (web/mobile session)",
            "repos": [],
            "missing_count": 0,
        }
    else:
        repos = gating_repos(config_path)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            rows = list(pool.map(check_one, repos))
        rows.sort(key=lambda r: (r["group"], r["name"]))
        result = {
            "available": True,
            "checked": len(rows),
            "repos": rows,
            "missing_count": sum(len(r["missing"]) for r in rows),
            "error_count": sum(1 for r in rows if r["error"]),
        }

    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(
        HEART_STATE_DIR / "required_workflow_drift.json", result
    )
    return result


def main(argv: list[str]) -> int:
    result = run()
    sys.path.insert(0, str(HEART_HOME))
    from heart.heart_color import (
        c_info,
        c_meta,
        c_ok,
        c_warn,
        glyph_ok,
        glyph_warn,
    )

    label = c_info("required_workflow_drift")
    if not result["available"]:
        print(f"{glyph_warn()} {label} {c_meta('skipped: ' + str(result.get('reason', '')))}")
        return 0

    missing = result["missing_count"]
    checked = result["checked"]
    errors = result["error_count"]
    suffix = c_meta(f"({checked} gating repos" + (f", {errors} unreadable)" if errors else ")"))
    if missing:
        worst = [
            f"{r['name']}: {', '.join(r['missing'])}"
            for r in result["repos"]
            if r["missing"]
        ]
        print(f"{glyph_warn()} {label} {c_warn(f'{missing} required workflow(s) with no file')} {suffix}")
        for line in worst:
            print(f"    {c_meta(line)}")
    else:
        print(f"{glyph_ok()} {label} {c_ok('every required workflow has a file')} {suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
