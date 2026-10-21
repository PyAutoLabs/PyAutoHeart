"""heart/checks/ci_timing.py — CI wall-clock per tracked workflow (the speed leg).

The speed of development is the speed of the gates a change must pass, and
until now that number was only recoverable by hand-scraping job logs: every
timing diagnosis in the record ("the diagnosis had to be rebuilt from CI job
logs by hand") started with an archaeology session. This check makes the
wall-clock of every tracked gate a *standing* observation — measured daily from
the Actions REST API, carried forward with its own history, and rendered on the
board with a ready-to-paste prompt on every actionable row.

Two modes, same module:

* **per-repo** (``--name/--group/--owner``) — the bash entry point pipes one
  repo's ``GET /repos/{owner}/{repo}/actions/runs`` payload in; we time each
  tracked workflow and write ``$HEART_PER_REPO_DIR/<name>.ci_timing.json``.
* **aggregate** (``--aggregate``) — read every sidecar plus the *previously
  published* ``board.json`` and write the global rollup at
  ``$HEART_STATE_DIR/ci_timing.json``: per-gate numbers, drift classification
  against the gate's own history, the rolled-forward history, and the events.

Deliberate choices, each one a recorded lesson:

* **duration = ``updated_at`` − ``run_started_at``, never ``created_at``.** A
  re-attempted run keeps its original ``created_at``, so the ``created_at``
  arithmetic reports multi-day durations for runs that took nine minutes. Runs
  with no ``run_started_at`` are skipped outright rather than guessed at.
* **No branch filter, no ``exclude_pull_requests``.** The contributor-facing
  number *is* the PR gate: what someone waits on before a merge. ``pull_request``
  runs are the point of the query, and are also medianed separately
  (``pr_median_s``) from everything else.
* **Medians over ``success`` runs only.** A failed run stops early and a
  cancelled one stops arbitrarily; pooling them into the median makes a gate
  look faster the more it breaks. The conclusion *mix* is recorded beside the
  median so the coverage behind the number stays visible.
* **``cancelled`` is disambiguated, not counted.** The concurrency block
  cancels a superseded run by design — benign, on ``main`` pushes as much as on
  PRs. The signature is a newer run of the same workflow on the same branch
  created no later than the cancelled run ended (``updated_at``, plus
  ``SUPERSEDE_SLACK_S``): the arrival of that run is what cancelled it. A
  ``cancelled`` run with no such successor is a kill/hang suspect and becomes
  an *event* (``pull_request`` runs also keep the older, looser "any newer run
  on the PR branch" test). ``timed_out`` is always an event.
* **A PR cancelled because its PR closed is ``abandoned``, not a suspect.**
  Closing a PR cancels its in-flight runs with no successor at all, and GitHub
  empties the run's ``pull_requests`` once the PR is closed, so the closure is
  read from the repo's recently closed PRs (``--closed-prs``). A cancelled
  ``pull_request`` run that was not superseded is abandoned when a PR on the
  same head branch closed between the run's creation and its end
  (``updated_at`` + ``SUPERSEDE_SLACK_S``). Push/``main`` cancels never use
  this rule, and with no closed-PR feed it simply does not fire.
* **Drift needs BOTH gates.** A gate is flagged ``warn`` only when today's
  median is ≥ ``yellow_factor`` × its recent history *and* ≥ ``min_delta_s``
  above it in absolute terms — the profiling conductor's doctrine. The ratio
  alone screams at jitter on fast gates; the absolute floor alone misses a big
  relative drift on a cheap one, and an alarm that cries wolf gets ignored.
* **Drift is advisory.** Gates are ``ok`` or ``warn``, never ``fail``; only
  events (hangs, kill-timer suspects) are hard rows. The four-tier readiness
  verdict is untouched by this check — it is a dashboard leg, not a gate.
* **A failed fetch is not a quiet repo.** ``--fetch-error`` records the reason
  and writes empty ``workflows``/``events``; "we could not ask" must never
  render as "all quiet".
* **The history comes from the committed record first.** ``timings/gates.jsonl``
  (see ``heart/timings.py``) is durable; the previously published ``board.json``
  is the same artifact this render produces, so a Pages gap loses it. The board
  stays as the fallback — first run, fresh clone, no record yet — and the
  rolled-forward ``performance.history`` is written either way.

Per-repo sidecar schema (``<name>.ci_timing.json``)::

    {
      "name": "RepoA", "group": "workspaces", "owner": "OwnerX",
      "error": "",                       # non-empty => the runs fetch failed
      "ts": "...",
      "workflows": {
        "Smoke Tests": {
          "median_s": 553.0,             # p50 over SUCCESS runs (all events)
          "pr_median_s": 601.0,          # p50 over SUCCESS pull_request runs
          "queue_median_s": 12.0,        # p50 queue delay over the same runs
          "max_s": 912.0, "runs_counted": 14,
          "window_from": "2026-08-17T...",
          "conclusions": {"success": 14, "failure": 1, "cancelled": 3,
                          "timed_out": 0},
          "superseded": 2,               # of those `cancelled`, benign ones
          "abandoned": 1,                # cancelled because their PR closed
          "actions_url": "https://github.com/OwnerX/RepoA/actions"
        }
      },
      "events": [{"kind": "timed_out", "workflow": ..., "run_url": ...,
                  "duration_s": ..., "head_branch": ..., "at": ...,
                  "prompt": "Use the bug skill. kill timer: ..."}]
    }

Global rollup schema (``ci_timing.json``)::

    {"ts", "gates": [...], "history": [...], "events": [...],
     "errors": [{"repo", "error"}], "thresholds": {...}}

Every actionable row — a warn gate, an event — carries its own ready-to-paste
prompt string. The producer writes the prompt; the renderer never re-derives it.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import sys
from pathlib import Path
from typing import Any

HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"

# Conclusions whose *count* is recorded per workflow. `cancelled` is recorded
# raw (superseded and abandoned PR runs included) with the benign shares
# reported separately as `superseded` and `abandoned`, so the mix always adds
# up to what the API actually said.
TRACKED_CONCLUSIONS = ("success", "failure", "cancelled", "timed_out")

# How long after a cancelled run's recorded end (``updated_at``) its successor
# may still be created and count as the run that superseded it. A concurrency
# cancel is caused BY the successor's arrival, so the successor's created_at
# normally precedes the cancelled run's updated_at (observed: 1-2 s before it,
# e.g. a library Tests main-push burst, 2026-10-17 20:01). The slack only absorbs second-level
# stamp granularity and API write ordering; it is far shorter than any push
# cadence, so a real kill followed by an unrelated later push stays a suspect.
SUPERSEDE_SLACK_S = 60

# Fallback when config/repos.yaml is unreadable; kept in sync with the YAML.
DEFAULT_CI_TIMING_THRESHOLDS = {
    "yellow_factor": 1.5,
    "min_delta_s": 120,
    "history_cap": 30,
}


# --- config ------------------------------------------------------------------
def _load_config(config_path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    try:
        import yaml

        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def load_thresholds(config_path: Path | str = CONFIG_PATH) -> dict[str, float]:
    """Return the ``thresholds.ci_timing`` block, defaulted key by key."""
    out = dict(DEFAULT_CI_TIMING_THRESHOLDS)
    block = ((_load_config(config_path).get("thresholds") or {}).get("ci_timing") or {})
    if isinstance(block, dict):
        for key in out:
            if isinstance(block.get(key), (int, float)):
                out[key] = block[key]
    return out


def tracked_workflows(
    repo: str, group: str, config_path: Path | str = CONFIG_PATH
) -> list[str]:
    """Workflow display names to time for ``repo``.

    The repo group's ``required_workflows`` (the gates that already decide
    whether a release is safe — the same names, so the board's speed row and
    its health row can never be about different workflows) PLUS any
    ``performance.extra_workflows`` entry naming this repo. Names live in
    declared config, never in organ code (the tenant firewall).
    """
    cfg = _load_config(config_path)
    names: list[str] = []
    required = (cfg.get("required_workflows") or {}).get(group)
    if isinstance(required, list):
        names.extend(str(w) for w in required)
    extra = (cfg.get("performance") or {}).get("extra_workflows")
    if isinstance(extra, list):
        for item in extra:
            if not isinstance(item, dict):
                continue
            if str(item.get("repo") or "") == repo and item.get("workflow"):
                names.append(str(item["workflow"]))
    # Stable order, no duplicates.
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


# --- pure timing helpers -----------------------------------------------------
def _parse_iso(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def normalize_runs(payload: Any) -> list[dict[str, Any]]:
    """Coerce a runs payload into the internal shape (REST or a bare list).

    Accepts the REST ``{"workflow_runs": [...]}`` object ``ci_timing.sh`` sends
    and a bare list of runs. Unlike ``ci_status`` there is **no branch filter**:
    PR-gate runs are exactly what this check is here to time.
    """
    if isinstance(payload, dict):
        raw = payload.get("workflow_runs") or []
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []

    runs: list[dict[str, Any]] = []
    for run in raw:
        if not isinstance(run, dict) or "workflow_runs" in run:
            continue
        runs.append(
            {
                "workflow": str(run.get("name") or run.get("workflowName") or ""),
                "conclusion": str(run.get("conclusion") or ""),
                "status": str(run.get("status") or ""),
                "event": str(run.get("event") or ""),
                "head_branch": str(run.get("head_branch") or run.get("headBranch") or ""),
                "created_at": str(run.get("created_at") or run.get("createdAt") or ""),
                "run_started_at": str(
                    run.get("run_started_at") or run.get("runStartedAt") or ""
                ),
                "updated_at": str(run.get("updated_at") or run.get("updatedAt") or ""),
                "url": str(run.get("html_url") or run.get("url") or ""),
            }
        )
    return runs


def duration_s(run: dict[str, Any]) -> float | None:
    """Wall-clock seconds: ``updated_at`` − ``run_started_at``.

    NEVER ``created_at``: a re-attempted run keeps its original creation stamp,
    so that arithmetic reports multi-day durations for a nine-minute run (the
    trap the Hands release board already hit). No ``run_started_at`` → no
    duration; the run is skipped rather than guessed at.
    """
    started = _parse_iso(run.get("run_started_at"))
    ended = _parse_iso(run.get("updated_at"))
    if started is None or ended is None:
        return None
    return round(max(0.0, (ended - started).total_seconds()), 1)


def queue_s(run: dict[str, Any]) -> float | None:
    """Queue delay: ``run_started_at`` − ``created_at``, floored at 0."""
    created = _parse_iso(run.get("created_at"))
    started = _parse_iso(run.get("run_started_at"))
    if created is None or started is None:
        return None
    return round(max(0.0, (started - created).total_seconds()), 1)


def normalize_closed_prs(payload: Any) -> list[dict[str, str]]:
    """Coerce a closed-PR feed into ``[{"head_ref", "closed_at"}]``.

    Accepts the slim list ``ci_timing.sh`` writes (``head_ref``/``closed_at``)
    and the raw REST ``/pulls`` shape (``head.ref``). Anything unusable — no
    feed, a fetch error, garbage — degrades to ``[]``, which disables the
    abandoned rule rather than failing the check.
    """
    if not isinstance(payload, list):
        return []
    out: list[dict[str, str]] = []
    for pr in payload:
        if not isinstance(pr, dict):
            continue
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        ref = str(pr.get("head_ref") or head.get("ref") or "")
        closed = str(pr.get("closed_at") or "")
        if ref and closed:
            out.append({"head_ref": ref, "closed_at": closed})
    return out


def read_closed_prs(path: str | Path | None) -> list[dict[str, str]]:
    """The closed-PR feed at ``path``, or ``[]`` on any problem (I/O)."""
    if not path:
        return []
    try:
        return normalize_closed_prs(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return []


def classify_cancelled(
    run: dict[str, Any],
    siblings: list[dict[str, Any]],
    closed_prs: list[dict[str, str]] | None = None,
) -> str:
    """``"superseded"`` / ``"abandoned"`` (benign) or ``"suspect"``.

    A workflow's concurrency group cancels the in-flight run when a newer run
    of the same workflow on the same ``head_branch`` arrives — on ``main``
    pushes as well as on PRs. So a cancelled run of ANY event is benign when
    such a sibling was created after it and no later than its own end
    (``updated_at`` + ``SUPERSEDE_SLACK_S``): that sibling is what cancelled
    it. A cancelled ``pull_request`` run (not on ``main``) additionally stays
    benign under the original rule — any newer run on the PR branch — so PR
    handling is no looser and no stricter than before. Anything else — no
    successor at all, or one created long after the run had already ended —
    is nobody's design: a kill-timer or hang suspect, and an event.

    The one exception is a closed PR. Closing a PR cancels its in-flight
    ``pull_request`` runs (not on ``main``) with no successor, so such a run
    is ``"abandoned"`` when ``closed_prs`` holds a PR on the same head branch
    whose ``closed_at`` lies in [run ``created_at``, ``updated_at`` +
    ``SUPERSEDE_SLACK_S``]. The lower bound stops an older PR on a reused
    branch name from matching; the upper bound keeps a PR that stayed open
    past the cancel, or closed long after, a suspect. Supersession is checked
    first, so a run that was both superseded and later abandoned counts once.
    """
    branch = run.get("head_branch")
    workflow = run.get("workflow")
    mine = run.get("created_at") or run.get("run_started_at") or ""
    mine_dt = _parse_iso(mine)
    ended = _parse_iso(run.get("updated_at"))
    deadline = (ended + datetime.timedelta(seconds=SUPERSEDE_SLACK_S)
                if ended is not None else None)
    legacy_pr = (str(run.get("event") or "") == "pull_request"
                 and str(branch or "") != "main")
    for other in siblings:
        if other is run:
            continue
        if other.get("workflow") != workflow or other.get("head_branch") != branch:
            continue
        theirs = other.get("created_at") or other.get("run_started_at") or ""
        if legacy_pr and theirs > mine:
            return "superseded"
        theirs_dt = _parse_iso(theirs)
        if (mine_dt is not None and theirs_dt is not None and deadline is not None
                and mine_dt < theirs_dt <= deadline):
            return "superseded"
    if legacy_pr and mine_dt is not None and deadline is not None:
        for pr in closed_prs or []:
            if pr.get("head_ref") != branch:
                continue
            closed_dt = _parse_iso(pr.get("closed_at"))
            if closed_dt is not None and mine_dt <= closed_dt <= deadline:
                return "abandoned"
    return "suspect"


def event_prompt(repo: str, event: dict[str, Any]) -> str:
    """The self-contained Use the bug skill. prompt a hang/kill row copies."""
    return (
        f"Use the bug skill. kill timer: {repo} {event.get('workflow')} {event.get('kind')} after "
        f"{int(round(float(event.get('duration_s') or 0)))}s on "
        f"{event.get('head_branch') or '?'} — {event.get('run_url') or '?'}"
    )


def gate_prompt(repo: str, workflow: str, old_s: float, new_s: float,
                actions_url: str) -> str:
    """The self-contained Use the bug skill. prompt a slowed gate copies."""
    return (
        f"Use the bug skill. smoke gate {repo}: {workflow} median wall-clock rose "
        f"{int(round(old_s))}s → {int(round(new_s))}s vs its recent history — "
        f"{actions_url}"
    )


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def time_workflow(
    repo: str, workflow: str, runs: list[dict[str, Any]], actions_url: str,
    closed_prs: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Time one workflow's runs → (entry, events).

    Only *completed* runs with a usable ``run_started_at`` participate: an
    in-flight run has no duration, and a run with no start stamp cannot be
    timed honestly. ``closed_prs`` (the repo's recently closed PRs) lets a
    PR run cancelled by its PR's closure count as ``abandoned`` rather than
    become an event; ``None``/``[]`` keeps the superseded-only behaviour.
    """
    mine = [
        r for r in runs
        if r.get("workflow") == workflow and r.get("conclusion") and r.get("run_started_at")
    ]
    # The run that cancelled a superseded one is often still in flight when
    # we look, so supersession is judged against EVERY run of the workflow,
    # not only the completed ones being timed.
    siblings = [r for r in runs if r.get("workflow") == workflow]
    durations: list[float] = []
    pr_durations: list[float] = []
    queues: list[float] = []
    conclusions = {k: 0 for k in TRACKED_CONCLUSIONS}
    superseded = 0
    abandoned = 0
    events: list[dict[str, Any]] = []
    window_from = ""

    for run in mine:
        concl = str(run.get("conclusion") or "")
        if concl in conclusions:
            conclusions[concl] += 1
        dur = duration_s(run)
        if dur is None:
            continue
        started = str(run.get("run_started_at") or "")
        if started and (not window_from or started < window_from):
            window_from = started

        if concl == "success":
            durations.append(dur)
            q = queue_s(run)
            if q is not None:
                queues.append(q)
            if str(run.get("event") or "") == "pull_request":
                pr_durations.append(dur)

        kind = ""
        if concl == "timed_out":
            kind = "timed_out"
        elif concl == "cancelled":
            verdict = classify_cancelled(run, siblings, closed_prs)
            if verdict == "superseded":
                superseded += 1
            elif verdict == "abandoned":
                abandoned += 1
            else:
                kind = "suspect_cancelled"
        if kind:
            event = {
                "kind": kind,
                "workflow": workflow,
                "run_url": str(run.get("url") or ""),
                "duration_s": dur,
                "head_branch": str(run.get("head_branch") or ""),
                "at": started,
            }
            event["prompt"] = event_prompt(repo, event)
            events.append(event)

    entry = {
        "median_s": _median(durations),
        "pr_median_s": _median(pr_durations),
        "queue_median_s": _median(queues),
        "max_s": round(max(durations), 1) if durations else None,
        "runs_counted": len(durations),
        "window_from": window_from,
        "conclusions": conclusions,
        "superseded": superseded,
        "abandoned": abandoned,
        "actions_url": actions_url,
    }
    return entry, events


def build_sidecar(
    name: str,
    group: str,
    owner: str,
    runs: Any,
    ts: str,
    config_path: Path | str = CONFIG_PATH,
    error: str = "",
    closed_prs: Any = None,
) -> dict[str, Any]:
    """Construct one repo's ci_timing sidecar.

    ``error`` is the reason the runs fetch failed. When set the sidecar carries
    NO workflows and NO events: a dead query must never render as a quiet repo.
    ``closed_prs`` is the repo's recently closed PRs (see
    ``normalize_closed_prs``); missing or unusable means no abandoned rule.
    """
    actions_url = f"https://github.com/{owner}/{name}/actions" if owner else ""
    if error:
        return {
            "name": name, "group": group, "owner": owner, "error": error,
            "ts": ts, "workflows": {}, "events": [],
        }

    normalized = normalize_runs(runs)
    closed = normalize_closed_prs(closed_prs)
    workflows: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    for workflow in tracked_workflows(name, group, config_path):
        entry, evts = time_workflow(name, workflow, normalized, actions_url, closed)
        # A workflow with no completed runs in the window is not evidence of
        # anything; recording it as a zero-run gate keeps the board honest
        # about coverage rather than dropping it silently.
        workflows[workflow] = entry
        events.extend(evts)

    return {
        "name": name, "group": group, "owner": owner, "error": "",
        "ts": ts, "workflows": workflows, "events": events,
    }


# --- aggregate ---------------------------------------------------------------
def gate_key(repo: str, workflow: str) -> str:
    return f"{repo}/{workflow}"


def history_baseline(history: list[dict[str, Any]], key: str, today: str) -> float | None:
    """Median of a gate's historical p50s, over PREVIOUS days only.

    Today's own entry is excluded so a re-render cannot compare a gate against
    itself and conclude everything is fine.
    """
    values: list[float] = []
    for entry in history or []:
        if not isinstance(entry, dict) or str(entry.get("date") or "") == today:
            continue
        gates = entry.get("gates")
        if not isinstance(gates, dict):
            continue
        row = gates.get(key)
        if isinstance(row, dict) and isinstance(row.get("p50_s"), (int, float)):
            values.append(float(row["p50_s"]))
    return round(statistics.median(values), 1) if values else None


def classify_drift(
    median_s: float | None, baseline_s: float | None, thresholds: dict[str, float]
) -> tuple[str, float | None, float | None]:
    """(state, ratio, delta_s) — ``warn`` needs BOTH gates, else ``ok``.

    Ratio alone screams at jitter on a fast gate ("host load alone has produced
    7× errors in this corpus"); the absolute floor alone misses a large relative
    drift on a cheap one. Requiring both is the profiling conductor's doctrine,
    carried over verbatim.
    """
    if not median_s or not baseline_s:
        return "ok", None, None
    ratio = round(median_s / baseline_s, 2)
    delta = round(median_s - baseline_s, 1)
    if (ratio >= float(thresholds.get("yellow_factor", 1.5))
            and delta >= float(thresholds.get("min_delta_s", 120))):
        return "warn", ratio, delta
    return "ok", ratio, delta


def roll_history(
    prev_history: Any, gates: list[dict[str, Any]], today: str, cap: int
) -> list[dict[str, Any]]:
    """Self-carrying roll-forward: drop today, append today, cap.

    One entry per date, so re-rendering the same day is idempotent rather than
    stacking duplicate points into the sparkline.
    """
    kept = [
        e for e in (prev_history if isinstance(prev_history, list) else [])
        if isinstance(e, dict) and str(e.get("date") or "") != today and e.get("date")
    ]
    today_gates = {
        gate_key(g["repo"], g["workflow"]): {
            "p50_s": g["median_s"], "runs": g["runs_counted"],
        }
        for g in gates
        if g.get("median_s") is not None
    }
    kept.append({"date": today, "gates": today_gates})
    return kept[-int(cap):] if cap and cap > 0 else kept


def prev_history_of(prev_board: Any) -> list[dict[str, Any]]:
    """``performance.history`` out of a previously published board.json.

    Anything unexpected — missing file, a 404 page, half a JSON document, an
    older board with no performance block — degrades to no history. A publish
    gap costs a sparkline, never a render.
    """
    if not isinstance(prev_board, dict):
        return []
    perf = prev_board.get("performance")
    if not isinstance(perf, dict):
        return []
    hist = perf.get("history")
    return [e for e in hist if isinstance(e, dict)] if isinstance(hist, list) else []


def aggregate(
    sidecars: list[dict[str, Any]],
    prev_board: Any,
    today: str,
    ts: str,
    thresholds: dict[str, float] | None = None,
    record_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fold the per-repo sidecars + the history into the global rollup.

    ``record_history`` is the COMMITTED record (``timings/gates.jsonl``, via
    ``heart.timings.gates_history``) and is preferred whenever it is non-empty:
    it is durable, whereas the previously published board is the same artifact
    the render produces and a Pages gap loses it. The board stays as the
    fallback for the very first run, a fresh clone, or a record that is not
    there yet.

    Whichever history was used is also what ``roll_history`` rolls forward, so
    ``board.json``'s ``performance.history`` keeps its shape either way — it
    remains the fallback cache and the Brain board's sparkline input.
    """
    thr = dict(thresholds or DEFAULT_CI_TIMING_THRESHOLDS)
    history_in = (
        [e for e in record_history if isinstance(e, dict)]
        if record_history
        else prev_history_of(prev_board)
    )

    gates: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for side in sorted(sidecars, key=lambda s: str(s.get("name") or "")):
        if not isinstance(side, dict):
            continue
        repo = str(side.get("name") or "")
        if side.get("error"):
            errors.append({"repo": repo, "error": str(side["error"])})
            continue
        workflows = side.get("workflows")
        if not isinstance(workflows, dict):
            continue
        for workflow in sorted(workflows):
            entry = workflows[workflow]
            if not isinstance(entry, dict):
                continue
            key = gate_key(repo, workflow)
            baseline = history_baseline(history_in, key, today)
            median = entry.get("median_s")
            state, ratio, delta = classify_drift(median, baseline, thr)
            actions_url = str(entry.get("actions_url") or "")
            gates.append({
                "repo": repo,
                "workflow": workflow,
                "median_s": median,
                "pr_median_s": entry.get("pr_median_s"),
                "queue_median_s": entry.get("queue_median_s"),
                "max_s": entry.get("max_s"),
                "runs_counted": entry.get("runs_counted", 0),
                "window_from": entry.get("window_from", ""),
                "conclusions": entry.get("conclusions") or {},
                "superseded": entry.get("superseded", 0),
                "abandoned": entry.get("abandoned", 0),
                "actions_url": actions_url,
                "baseline_s": baseline,
                "ratio": ratio,
                "delta_s": delta,
                "state": state,
                "prompt": (gate_prompt(repo, workflow, baseline, median, actions_url)
                           if state == "warn" else None),
            })
        for event in side.get("events") or []:
            if isinstance(event, dict):
                events.append(dict(event, repo=repo))

    history = roll_history(history_in, gates, today, int(thr.get("history_cap", 30)))
    return {
        "ts": ts,
        "gates": gates,
        "history": history,
        "events": events,
        "errors": errors,
        "thresholds": thr,
    }


def read_sidecars(per_repo_dir: Path) -> list[dict[str, Any]]:
    """Every ``*.ci_timing.json`` sidecar under ``per_repo_dir`` (I/O)."""
    out: list[dict[str, Any]] = []
    for path in sorted(Path(per_repo_dir).glob("*.ci_timing.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def read_prev_board(path: str | Path | None) -> Any:
    """The previously published board.json, or ``{}`` on any problem."""
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


# --- summary lines -----------------------------------------------------------
def repo_summary_line(sidecar: dict[str, Any]) -> str:
    """Coloured one-line per-repo summary for the daemon log."""
    from heart.heart_color import (
        c_info, c_meta, c_ok, c_warn, glyph_ok, glyph_warn,
    )

    name = sidecar.get("name", "?")
    if sidecar.get("error"):
        return (f"{glyph_warn()} {c_info(name)} {c_warn('ci_timing UNAVAILABLE')} "
                f"{c_meta(str(sidecar['error']))}")
    workflows = sidecar.get("workflows") or {}
    timed = sum(1 for e in workflows.values()
                if isinstance(e, dict) and e.get("runs_counted"))
    events = sidecar.get("events") or []
    if events:
        return (f"{glyph_warn()} {c_info(name)} "
                f"{c_warn(f'{len(events)} hang/kill event(s)')} "
                f"{c_meta(f'{timed} gate(s) timed')}")
    return (f"{glyph_ok()} {c_info(name)} {c_ok(f'{timed} gate(s) timed')} "
            f"{c_meta(f'{len(workflows)} tracked')}")


def summary_line(rollup: dict[str, Any]) -> str:
    """Coloured one-line summary of the global rollup."""
    from heart.heart_color import (
        c_info, c_meta, c_ok, c_warn, glyph_ok, glyph_warn,
    )

    gates = rollup.get("gates") or []
    timed = sum(1 for g in gates if g.get("runs_counted"))
    slowed = sum(1 for g in gates if g.get("state") == "warn")
    events = len(rollup.get("events") or [])
    errors = len(rollup.get("errors") or [])
    body = f"{timed} gates timed, {slowed} slowed, {events} hang events"
    if errors:
        body += f", {errors} unavailable"
    glyph = glyph_warn() if (slowed or events or errors) else glyph_ok()
    tint = c_warn if (slowed or events or errors) else c_ok
    return (f"{glyph} {c_info('ci_timing:')} {tint(body)} "
            f"{c_meta(f'({len(gates)} tracked)')}")


# --- I/O shell ---------------------------------------------------------------
def _write(out_path: Path, payload: dict[str, Any]) -> None:
    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(out_path, payload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="heart.checks.ci_timing")
    ap.add_argument("--aggregate", action="store_true",
                    help="fold the per-repo sidecars into the global rollup")
    ap.add_argument("--name", default="")
    ap.add_argument("--group", default="")
    ap.add_argument("--owner", default="")
    ap.add_argument("--ts", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--prev-board", default="",
                    help="previously published board.json (aggregate mode); "
                         "missing/unreadable => no history, never an error")
    ap.add_argument("--today", default="",
                    help="ISO date for the history entry (default: today, UTC)")
    ap.add_argument("--per-repo-dir", default="",
                    help="where the sidecars live (default: $HEART_STATE_DIR/per-repo)")
    ap.add_argument("--record-dir", default="",
                    help="the committed timing record (default: HEART_HOME/timings); "
                         "its gates.jsonl is the preferred history source, the "
                         "previous board.json only the fallback")
    ap.add_argument("--fetch-error", default="",
                    help="reason the runs fetch failed; recorded instead of a bogus quiet repo")
    ap.add_argument("--closed-prs", default="",
                    help="JSON list of the repo's recently closed PRs (head_ref, "
                         "closed_at); missing/unreadable => no abandoned rule, "
                         "never an error")
    ns = ap.parse_args(argv)

    ts = ns.ts or datetime.datetime.now(datetime.timezone.utc).isoformat()

    if ns.aggregate:
        sys.path.insert(0, str(HEART_HOME))
        from heart import state as _state
        from heart import timings as _timings

        per_repo = Path(ns.per_repo_dir) if ns.per_repo_dir else _state.HEART_PER_REPO_DIR
        today = ns.today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        thr = load_thresholds()
        record_dir = Path(ns.record_dir) if ns.record_dir else _timings.TIMINGS_DIR
        # compare within the current epoch only — a boundary in
        # timings/epochs.jsonl is where the world changed (a rebuild, a runner
        # change), and a baseline from before it is not a baseline.
        epochs = _timings.read_epochs(record_dir / "epochs.jsonl")
        since = ((_timings.current_epoch(epochs) or {}).get("date") or "")
        rollup = aggregate(
            read_sidecars(per_repo), read_prev_board(ns.prev_board), today, ts,
            thr,
            record_history=_timings.gates_history(
                record_dir / "gates.jsonl", int(thr.get("history_cap", 30)),
                since=since,
            ),
        )
        _write(Path(ns.out), rollup)
        print(summary_line(rollup))
        return 0

    if not ns.name or not ns.group:
        ap.error("--name and --group are required outside --aggregate mode")

    error = ns.fetch_error
    try:
        runs: Any = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Unparseable stdin is itself a fetch failure — say so rather than
        # writing an empty, quiet-looking sidecar.
        runs = []
        error = error or "runs payload was not valid JSON"

    sidecar = build_sidecar(ns.name, ns.group, ns.owner, runs, ts, error=error,
                            closed_prs=read_closed_prs(ns.closed_prs))
    _write(Path(ns.out), sidecar)
    print(repo_summary_line(sidecar))
    return 0


if __name__ == "__main__":
    sys.exit(main())
