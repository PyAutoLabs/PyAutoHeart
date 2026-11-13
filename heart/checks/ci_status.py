"""heart/checks/ci_status.py — per-required-workflow CI conclusions on main HEAD.

The bash entry point (``ci_status.sh``) fetches, per polled repo, the recent
workflow runs on ``main`` plus the ``main`` HEAD sha via ``gh``, then pipes the
runs JSON here. This module owns the *logic*: pick the latest run of each
workflow on main, roll the **required** workflows for the repo's group into a
single conclusion, write the structured sidecar, and print the coloured summary
line. Keeping it in Python (not inlined ``python3 -c`` in bash) makes the
gating logic unit-testable in isolation.

Two input shapes are accepted (see ``normalize_runs``): the REST
``/actions/runs`` payload that ``ci_status.sh`` now sends, and the older bare
list from ``gh run list --json``. The shell fetches over ``gh api`` rather than
``gh run list`` because ``gh run list``'s flags and ``--json`` field names moved
between ``gh`` releases (``--branch`` does not exist before gh 2.9), whereas the
REST endpoint is stable across every ``gh`` version that has ``gh api``.

A *failed fetch* is not a *pending CI*. When the shell cannot retrieve runs it
passes ``--fetch-error``, and the sidecar records ``status="unavailable"`` plus
an ``error`` string instead of silently looking like an in-progress run. This
distinction matters because the release gate leans on this evidence: before it
existed, a broken ``gh`` call rendered identically to "CI still running" on
every repo at once, so a genuine red and a dead query were indistinguishable.

Why per-workflow, not "the newest run": each workspace gates on several
workflows (e.g. ``Smoke Tests`` + ``Navigator Check``), each a matrix over two
Pythons. ``gh run list --limit 1`` returns one run of *some* workflow, so it can
report a green ``Navigator Check`` while ``Smoke Tests`` was red, or a run from a
feature branch. For release readiness we need the conclusion of **each required
workflow on the ``main`` HEAD commit**.

Sidecar schema written to ``<name>.ci_status.json``::

    {
      "name": "autolens_workspace",
      "group": "workspaces",
      "head_sha": "abc1234",
      "required": ["Smoke Tests", "Navigator Check"],
      "conclusion": "failure",        # rolled-up over required workflows (back-compat)
      "status": "completed",          # rolled-up status (back-compat)
      "error": "",                    # non-empty => the runs fetch itself failed
      "source": "gh",                 # "gh" (live fetch) | "cloud" (agent/MCP-supplied)
      "sha": "abc1234",               # short HEAD sha (back-compat with old sidecar)
      "workflow": "Smoke Tests",      # the failing/representative workflow, for the summary
      "url": "https://github.com/.../actions/runs/123",
      "workflows": {
        "Smoke Tests":     {"conclusion": "failure", "status": "completed",
                            "head_sha": "abc1234", "on_head": true, "url": "...",
                            "created_at": "..."},
        "Navigator Check": {"conclusion": "success", ...}
      },
      "ts": "..."
    }

The top-level ``conclusion``/``status``/``group``/``sha`` keys are retained so
the existing ``status.py`` repo glyph and the ``readiness`` library loop keep
working unchanged; the new ``workflows`` dict is the structured detail the
workspace-CI readiness gate consumes.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"

# How old an agent/MCP-supplied runs payload may be before it stops counting.
# A CI conclusion is a claim about *now*; past this bound the honest answer is
# the same "unavailable" the failed fetch already produced, never a stale green.
# One hour: long enough to span a mobile session's refresh-then-tick, short
# enough that nobody reads yesterday's CI as today's.
CLOUD_RUNS_MAX_AGE_S = int(os.environ.get("HEART_CLOUD_CI_MAX_AGE", "3600"))

# Conclusions that count as a hard failure for gating. Deliberately a denylist
# of genuine failures rather than "anything != success": ``skipped`` /
# ``neutral`` / ``stale`` are non-events (e.g. a path-filtered job) and must not
# RED a release. Mirrored in readiness.py.
FAILURE_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "startup_failure", "cancelled", "action_required"}
)

# Fallback if config/repos.yaml is unreadable; kept in sync with the YAML.
DEFAULT_REQUIRED_WORKFLOWS: dict[str, list[str]] = {
    "libraries": ["Tests"],
    "workspaces": ["Smoke Tests", "Navigator Check"],
    "workspaces_test": ["Smoke Tests"],
    "howto": ["Smoke Tests", "Navigator Check"],
}


def load_required_workflows(config_path: Path | str = CONFIG_PATH) -> dict[str, list[str]]:
    """Return the group→required-workflows map from repos.yaml, or the default."""
    try:
        import yaml

        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        rw = cfg.get("required_workflows")
        if isinstance(rw, dict) and rw:
            return {str(k): list(v) for k, v in rw.items() if isinstance(v, list)}
    except Exception:
        pass
    return dict(DEFAULT_REQUIRED_WORKFLOWS)


def required_for(group: str, config_path: Path | str = CONFIG_PATH) -> list[str]:
    """Required gating workflows for ``group`` ([] if the group is advisory)."""
    return load_required_workflows(config_path).get(group, [])


def cloud_runs(
    path: Path | str,
    max_age_s: int = CLOUD_RUNS_MAX_AGE_S,
    now: datetime.datetime | None = None,
) -> tuple[Any | None, str]:
    """Read an agent/MCP-supplied runs payload, or explain why it was rejected.

    The mobile/cloud path. There is no ``gh`` in a Claude web/mobile session, so
    the live fetch in ``ci_status.sh`` fails outright; Brain queries the runs via
    its MCP GitHub tools and drops the REST response here instead. This is the
    same hand-off ``test_run.py`` uses for the workspace-validation verdict — the
    MCP ``list_workflow_runs`` response is already the exact
    ``{"workflow_runs": [...]}`` shape :func:`normalize_runs` expects, so nothing
    is reshaped on the way in.

    File shape (``ts`` optional; the file's mtime is used when it is absent)::

        {"ts": "2026-08-25T12:00:00+00:00", "runs": {"workflow_runs": [...]}}

    Returns ``(runs, "")`` when usable, else ``(None, reason)``. Fails closed at
    every step: unreadable, malformed, undated *and* un-stat-able, or simply too
    old all return ``None`` with the reason, so the caller reports "we could not
    ask" rather than dressing up stale evidence as current.
    """
    p = Path(path)
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return None, "no cloud runs payload"
    except (OSError, json.JSONDecodeError, ValueError):
        return None, "cloud runs payload unreadable"

    if not isinstance(data, dict):
        return None, "cloud runs payload malformed"

    runs = data.get("runs", data)
    if not isinstance(runs, (dict, list)) or not runs:
        return None, "cloud runs payload empty"

    stamped = data.get("ts")
    ts: datetime.datetime | None = None
    if stamped:
        try:
            ts = datetime.datetime.fromisoformat(str(stamped).replace("Z", "+00:00"))
        except ValueError:
            ts = None
    if ts is None:
        try:
            ts = datetime.datetime.fromtimestamp(
                p.stat().st_mtime, datetime.timezone.utc
            )
        except OSError:
            return None, "cloud runs payload undated"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)

    now = now or datetime.datetime.now(datetime.timezone.utc)
    age = (now - ts).total_seconds()
    if age > max_age_s:
        return None, f"cloud runs payload stale ({int(age)}s > {max_age_s}s)"

    return runs, ""


def normalize_runs(payload: Any, branch: str = "main") -> list[dict[str, Any]]:
    """Coerce a runs payload into the internal run shape, filtered to ``branch``.

    Accepts either shape:

    - the REST ``GET /repos/{owner}/{repo}/actions/runs`` object, i.e.
      ``{"workflow_runs": [...]}`` with snake_case keys, which is what
      ``ci_status.sh`` sends; or
    - a bare list of camelCase runs, the historical ``gh run list --json``
      output (still accepted so older cached payloads and the existing tests
      keep working).

    REST runs carry the workflow's display name in ``name`` (the commit subject
    lives in ``display_title``), which is exactly the key ``required_workflows``
    is written against. Runs whose ``head_branch`` is known and is not
    ``branch`` are dropped — the REST query already filters by branch, so this
    is belt-and-braces for the legacy shape, which was never branch-filtered on
    a ``gh`` too old to support ``--branch``.
    """
    if isinstance(payload, dict):
        raw = payload.get("workflow_runs") or []
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []

    runs: list[dict[str, Any]] = []
    for run in raw:
        if not isinstance(run, dict):
            continue
        if "workflow_runs" in run:  # guard against a doubly-wrapped payload
            continue
        # REST shape is detected by its snake_case keys; camelCase wins when
        # present so an already-normalized run passes through untouched.
        head_branch = run.get("headBranch", run.get("head_branch"))
        if head_branch and branch and head_branch != branch:
            continue
        runs.append(
            {
                "workflowName": run.get("workflowName") or run.get("name") or "",
                "conclusion": run.get("conclusion") or "",
                "status": run.get("status") or "",
                "headSha": run.get("headSha") or run.get("head_sha") or "",
                "createdAt": run.get("createdAt") or run.get("created_at") or "",
                "url": run.get("url") or run.get("html_url") or "",
                "event": run.get("event") or "",
                "headBranch": head_branch or "",
            }
        )
    return runs


def latest_per_workflow(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collapse ``gh run list`` output to the newest run of each workflow on main.

    Runs are expected already filtered to ``--branch main`` by the caller, but we
    also drop ``pull_request`` events defensively (a PR targeting main has
    headBranch == its source branch, so it normally won't appear, but be safe).
    Keyed by ``workflowName``; the value keeps the fields readiness/status need.
    """
    latest: dict[str, dict[str, Any]] = {}
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        if run.get("event") == "pull_request":
            continue
        wf = run.get("workflowName") or run.get("name")
        if not wf:
            continue
        created = run.get("createdAt") or ""
        prev = latest.get(wf)
        if prev is None or created >= (prev.get("createdAt") or ""):
            latest[wf] = run
    return latest


def _wf_entry(run: dict[str, Any], head_sha: str) -> dict[str, Any]:
    run_sha = run.get("headSha") or ""
    return {
        "conclusion": run.get("conclusion") or "",
        "status": run.get("status") or "",
        "head_sha": run_sha[:7],
        "on_head": bool(head_sha) and run_sha == head_sha,
        "url": run.get("url") or "",
        "created_at": run.get("createdAt") or "",
    }


def rollup(workflows: dict[str, dict[str, Any]], required: list[str]) -> dict[str, str]:
    """Roll required-workflow conclusions into one (conclusion, status, workflow).

    - Any required workflow with a FAILURE_CONCLUSIONS conclusion → ``failure``
      (status ``completed``); ``workflow`` names the first such workflow so the
      summary can point at it.
    - All required present, completed, success **and on HEAD** → ``success``.
    - Otherwise (in-progress, queued, missing, or success on a stale sha) →
      conclusion ``""`` with status ``in_progress`` (an *unknown*, never a green).

    When ``required`` is empty (an advisory group) the caller passes the whole
    latest-per-workflow set as ``workflows`` and we report the single newest
    run's conclusion, preserving the old "latest run" dashboard signal.
    """
    if not required:
        # Advisory group: report the newest run across all workflows.
        newest = max(
            workflows.values(),
            key=lambda e: e.get("created_at", ""),
            default=None,
        )
        if newest is None:
            return {"conclusion": "", "status": "", "workflow": ""}
        return {
            "conclusion": newest.get("conclusion", ""),
            "status": newest.get("status", ""),
            "workflow": "",
        }

    failing = [wf for wf in required
               if (workflows.get(wf, {}).get("conclusion") in FAILURE_CONCLUSIONS)]
    if failing:
        return {"conclusion": "failure", "status": "completed", "workflow": failing[0]}

    all_green = all(
        workflows.get(wf, {}).get("conclusion") == "success"
        and workflows.get(wf, {}).get("status") in ("completed", "")
        and workflows.get(wf, {}).get("on_head", False)
        for wf in required
    )
    if all_green:
        return {"conclusion": "success", "status": "completed", "workflow": ""}

    return {"conclusion": "", "status": "in_progress", "workflow": ""}


def build_sidecar(
    name: str,
    group: str,
    runs: Any,
    head_sha: str,
    ts: str,
    config_path: Path | str = CONFIG_PATH,
    error: str = "",
    source: str = "gh",
) -> dict[str, Any]:
    """Construct the full ci_status sidecar dict for one repo.

    ``error`` is the reason the runs fetch failed. When set, the sidecar
    reports ``status="unavailable"`` rather than the ``in_progress`` that an
    empty run list would otherwise roll up to, so "we could not ask" is
    readable as itself instead of impersonating "CI is still running".

    ``source`` records where the runs came from — ``"gh"`` for the live fetch,
    ``"cloud"`` for an agent/MCP-supplied payload. Provenance is part of the
    evidence, not decoration: a reader must be able to tell a conclusion Heart
    fetched itself from one an agent handed it.
    """
    required = required_for(group, config_path)
    latest = latest_per_workflow(normalize_runs(runs))
    workflows = {wf: _wf_entry(run, head_sha) for wf, run in latest.items()}

    if error:
        roll = {"conclusion": "", "status": "unavailable", "workflow": ""}
    else:
        roll = rollup(workflows, required)
    # Pick a representative url: the failing workflow's, else HEAD's newest.
    rep_wf = roll["workflow"]
    rep = workflows.get(rep_wf) if rep_wf else None
    if rep is None:
        rep = max(workflows.values(), key=lambda e: e.get("created_at", ""), default=None)

    return {
        "name": name,
        "group": group,
        "head_sha": head_sha,
        "sha": (head_sha or "")[:7],
        "required": required,
        "conclusion": roll["conclusion"],
        "status": roll["status"],
        "workflow": roll["workflow"],
        "error": error,
        "source": source,
        "url": (rep or {}).get("url", ""),
        "workflows": workflows,
        "ts": ts,
    }


# --- declared-CI backfill (PyAutoHeart#340) ---------------------------------
#
# The runs page above is the newest 30 runs on main. A repo whose OWN CI is
# declared in config/repos.yaml `dev_ship.ci_workflows` (an organ, say) can
# have a declared workflow fall off that page behind busy ops workflows; the
# development-ship gate would then have no run to judge. For each declared
# workflow absent from the page we fetch its latest main run directly —
# `actions/workflows/<id>/runs?branch=main&per_page=1` — resolving the id from
# the name via `actions/workflows` once and caching it. Bounded: one extra call
# per missing declared workflow per tick (plus the listing, only on a cache
# miss), each under BACKFILL_TIMEOUT_S. A failed fetch leaves the workflow
# absent, which readiness reads as STALE. The rolled-up conclusion is computed
# BEFORE the backfill, so the release-facing rollup is unchanged.

#: Per-call bound for the backfill's gh calls (the tick's ls-remote bound).
BACKFILL_TIMEOUT_S = int(os.environ.get("HEART_LS_REMOTE_TIMEOUT", "10"))


def gh_api(path: str) -> Any:
    """The gh seam: parsed ``gh api <path>`` output, or None on any failure."""
    try:
        result = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                                timeout=BACKFILL_TIMEOUT_S)
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def load_declared_ci(name: str, config_path: Path | str = CONFIG_PATH) -> list[str]:
    """``dev_ship.ci_workflows[name]`` — the repo's declared own CI ([] if none)."""
    try:
        import yaml

        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        return [str(w) for w in ((cfg.get("dev_ship") or {}).get("ci_workflows") or {})
                .get(name) or []]
    except Exception:
        return []


def _read_cache(path: Path | None) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text()) if path and path.is_file() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def backfill_declared(
    sidecar: dict[str, Any],
    owner: str,
    declared: list[str],
    api: Callable[[str], Any],
    cache_path: Path | None = None,
) -> list[str]:
    """Add the latest main run of each declared workflow missing from
    ``sidecar["workflows"]`` (entries flagged ``backfilled``). Returns the
    declared workflows still missing (fetch failed). Mutates ``sidecar``."""
    name = sidecar.get("name") or ""
    workflows = sidecar.setdefault("workflows", {})
    missing = [wf for wf in declared if wf not in workflows]
    if not missing or not owner or not name:
        return missing
    ids = _read_cache(cache_path)
    if any(wf not in ids for wf in missing):
        listing = api(f"repos/{owner}/{name}/actions/workflows?per_page=100")
        for w in (listing or {}).get("workflows") or [] if isinstance(listing, dict) else []:
            if isinstance(w, dict) and w.get("name") and w.get("id"):
                ids[str(w["name"])] = w["id"]
        if cache_path is not None and ids:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(ids, sort_keys=True))
                tmp.replace(cache_path)
            except OSError:
                pass
    still: list[str] = []
    head_sha = sidecar.get("head_sha") or ""
    for wf in missing:
        wid = ids.get(wf)
        payload = api(f"repos/{owner}/{name}/actions/workflows/{wid}/runs"
                      "?branch=main&per_page=1&exclude_pull_requests=true") if wid else None
        runs = normalize_runs(payload) if isinstance(payload, dict) else []
        if not runs:
            still.append(wf)
            continue
        entry = _wf_entry(runs[0], head_sha)
        entry["backfilled"] = True
        workflows[wf] = entry
    if still:
        sidecar["backfill_missing"] = still
    return still


def summary_line(sidecar: dict[str, Any]) -> str:
    """Coloured one-line summary for the daemon log."""
    from heart.heart_color import (
        c_fail, c_info, c_meta, c_ok, c_warn,
        glyph_fail, glyph_ok, glyph_warn,
    )

    name = sidecar.get("name", "?")
    conclusion = sidecar.get("conclusion", "")
    status = sidecar.get("status", "")
    sha = sidecar.get("sha", "")
    workflows = sidecar.get("workflows") or {}
    error = sidecar.get("error", "")

    if error:
        return f"{glyph_warn()} {c_info(name)} {c_warn('CI UNAVAILABLE')} {c_meta(error)}"
    if not workflows and not sha:
        return f"{c_meta('·')} {c_info(name)} {c_meta('(no runs)')}"
    if conclusion == "success":
        return f"{glyph_ok()} {c_info(name)} {c_ok('success')} {c_meta(f'({sha})')}"
    if conclusion == "failure":
        wf = sidecar.get("workflow") or "?"
        return f"{glyph_fail()} {c_info(name)} {c_fail('FAILURE')} {c_meta(f'{wf} @ {sha}')}"
    # unknown / in-progress
    detail = status or "pending"
    return f"{glyph_warn()} {c_info(name)} {c_warn(detail)} {c_meta(f'({sha})')}"


def write_and_summarise(
    name: str,
    group: str,
    runs: Any,
    head_sha: str,
    ts: str,
    out_path: Path,
    config_path: Path | str = CONFIG_PATH,
    error: str = "",
    source: str = "gh",
) -> dict[str, Any]:
    sidecar = build_sidecar(name, group, runs, head_sha, ts, config_path, error, source)
    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(out_path, sidecar)
    return sidecar


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="heart.checks.ci_status")
    ap.add_argument("--name", required=True)
    ap.add_argument("--group", required=True)
    ap.add_argument("--head-sha", default="")
    ap.add_argument("--ts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--fetch-error",
        default="",
        help="reason the runs fetch failed; recorded instead of a bogus pending state",
    )
    ap.add_argument("--owner", default="",
                    help="GitHub owner, for the declared-CI backfill (#340)")
    ap.add_argument(
        "--cloud-runs",
        default="",
        help="agent/MCP-supplied runs payload, consulted only when the live fetch failed",
    )
    ns = ap.parse_args(argv)

    error = ns.fetch_error
    try:
        runs: Any = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Unparseable stdin is itself a fetch failure: report it as one rather
        # than falling through to an empty run list that reads as "pending".
        runs = []
        error = error or "runs payload was not valid JSON"

    # The live fetch is authoritative and is never second-guessed: the drop
    # point is consulted only once `gh` has already failed (or is absent, which
    # is the same thing from here). That ordering is what keeps the dev box's
    # behaviour byte-for-byte unchanged — there, `error` is empty and this never
    # runs — while giving the no-gh session real evidence instead of a blank.
    source = "gh"
    if error and ns.cloud_runs:
        supplied, reason = cloud_runs(ns.cloud_runs)
        if supplied is not None:
            runs, error, source = supplied, "", "cloud"
        else:
            # Keep the original fetch failure as the headline and append why the
            # fallback did not save it, so "no gh AND the payload was stale" is
            # one readable string rather than a silently unchanged error.
            error = f"{error} ({reason})"

    sidecar = build_sidecar(ns.name, ns.group, runs, ns.head_sha, ns.ts,
                            error=error, source=source)
    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    declared = load_declared_ci(ns.name)
    if declared and not error and source == "gh":
        backfill_declared(sidecar, ns.owner, declared, gh_api,
                          state.HEART_STATE_DIR / "ci_workflow_ids" / f"{ns.name}.json")
    state.atomic_write_json(Path(ns.out), sidecar)
    print(summary_line(sidecar))
    return 0


if __name__ == "__main__":
    sys.exit(main())
