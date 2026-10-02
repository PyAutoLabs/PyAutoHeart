"""Complete dashboard coverage, separate from the authoritative release gate.

Pure projection: no checks run here. Family penalties are capped so one large
repo cannot dominate the score. Every unresolved observation remains in the
inventory, including those omitted by display and clipboard budgets.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from collections import Counter
from pathlib import Path

import yaml

# Expected evidence produced by tick or the daily/deep collectors. Metadata
# (timings_record) and duplicate unit timing summaries are not separate checks.
FAMILIES = {
    "worktree_drift": "Worktree drift",
    "script_timing": "Script timing",
    "import_time": "Import timing",
    "unit_test_timing": "Unit-test timing",
    "profiling_drift": "Profiling drift",
    "test_run": "Workspace validation",
    "ci_timing": "CI wall-clock",
    "smoke_timings": "Smoke scripts",
    "no_run_census": "Skipped-script census",
    "version_skew": "Version floors",
    "version_skew_pypi": "PyPI version floors",
    "manifest_drift": "Repository manifest drift",
    "required_workflow_drift": "Required workflow coverage",
    "verify_install": "Install verification",
    "url_check": "URL hygiene",
    "validation_report": "Release validation",
}
ALIASES = {"release_validation": "validation_report"}
STATES = {"ok": "green", "success": "green", "passed": "green", "pass": "green",
          "warn": "yellow", "fail": "red", "failed": "red", "failure": "red",
          "timed_out": "red", "timeout": "red", "unobserved": "grey",
          "info": "grey", "unknown": "grey", "building": "grey",
          "satisfiable": "green", "unsatisfiable": "red", "bad": "red",
          "floor_yanked": "yellow", "in_progress": "grey", "queued": "grey"}
ORDER = {"red": 0, "yellow": 1, "stale": 2, "grey": 3, "green": 4, "na": 5}
POINTS = {"red": 10, "yellow": 5, "stale": 2, "grey": 2}


def status(value):
    value = str(value or "grey").lower()
    return value if value in ORDER else STATES.get(value, "grey")


def timestamp(value):
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t
    except (ValueError, TypeError):
        return None


def remedy(family, subject, gap=False):
    if family in {"worktree_drift", "repo_state"}:
        text = (f"Use the health and repo-cleanup skills. Inspect {subject} in the canonical "
                "checkout and active task ledger on the dev box. Preserve edits and claims; "
                "refresh with pyauto-heart tick and publish with pyauto-heart publish. "
                "Do not delete, reset, merge or close work without authorization.")
    elif family in {"validation_report", "test_run", "verify_install"}:
        text = (f"Use the health and release skills to inspect {subject}. "
                "For missing evidence, use the documented validation or install-verification "
                "procedure in the required environment, ingest results and re-assess. "
                "A rehearsal is not authorization to publish a release.")
    elif family in {"import_time", "unit_test_timing", "unit_timings", "ci_timing", "smoke_timings", "script_timing", "workspace_testmode_timing", "no_run_census"}:
        text = (f"Use the hygiene and ci-speedup skills to inspect {subject}. Read the full "
                f"{family} evidence and its collector instructions. Obtain missing runtime "
                "measurements or baselines in the required environment; diagnose regressions "
                "and skipped scripts through start-dev. Refresh the collector and publish "
                "Heart evidence. A tick alone does not run deep timing measurements.")
    elif family == "url_check":
        text = "Use the hygiene skill. Run pyauto-heart url_sweep; route broken links through start-dev, then refresh and publish the evidence."
    elif family == "profiling_drift":
        text = "Use the profiling skill. Inspect pinned results and the available local results checkout; obtain missing evidence through the profiling workflow, then refresh Heart and publish."
    else:
        text = (f"Use the health skill to inspect {subject} ({family}) and its source collector. "
                "Refresh missing evidence in the required environment; route confirmed defects "
                "through bug and start-dev. Report the missing capability if observation is "
                "unavailable. Re-assess and publish Heart evidence.")
    return {"kind": "prompt", "label": "refresh evidence" if gap else "inspect finding", "payload": text}


def expected_repos():
    config = yaml.safe_load((Path(__file__).parents[1] / "config/repos.yaml").read_text())
    return [r["name"] for rows in config["repos"].values() for r in rows]


def family_data(snapshot, family):
    """Whether this vantage actually carries a family's observation."""
    data = snapshot.get(family)
    if not data and family in {"import_time", "unit_test_timing"}:
        data = snapshot.get("unit_timings")
    return data


def published_checks(family, section):
    """Validate public rows; malformed inventory never establishes coverage."""
    stored = section.get("monitoring_checks")
    if not isinstance(stored, list):
        return [], False
    rows = [item for item in stored if isinstance(item, dict)
            and isinstance(item.get("id"), str) and item["id"]
            and item.get("family", family) == family
            and item.get("status") in ORDER
            and isinstance(item.get("summary"), str)
            and isinstance(item.get("subject"), str)]
    complete = (len(rows) == len(stored)
                and any(item["id"] == f"{family}:coverage" for item in rows))
    return rows, complete


def assess(board, snapshot, *, devbox=None, now=None, repos=None, families=None):
    """Return one uncapped inventory used by score, repair and all consumers.

    ``repos``/``families`` let tests exercise small complete universes; production
    always uses Heart's complete configured registry. N/A requires an explicit
    applicability reason, never merely an absent checkout or measurement.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    devbox = devbox if isinstance(devbox, dict) else {}
    checks = {}

    def add(family, subject, state, summary, evidence=None, *, source=None,
            observed_at=None, action=None, identity=None, na_reason=None):
        state = status(state)
        parsed = timestamp(observed_at)
        max_age = (14 if family in {"verify_install", "validation_report"} else
                   7 if "timing" in family or family == "import_time" else 2) * 86400
        fresh = parsed is not None and -300 <= (now - parsed).total_seconds() <= max_age
        if na_reason:
            state = "na"
        elif state in {"green", "na"} and not fresh:
            state = "stale" if parsed else "grey"
            summary += " — evidence expired" if parsed else " — observation time missing"
        identity = identity or f"{family}:{subject}"
        item = {"id": identity, "family": family, "subject": subject,
                "status": state, "summary": summary, "observed_at": observed_at,
                "fresh": fresh, "source": source or f"snapshot.{family}",
                "owner": "health" if family == "release" else family,
                "applicable": not bool(na_reason), "applicability_reason": na_reason,
                "evidence": evidence, "action": action,
                "blocked_reason": None}
        if state not in {"green", "na"}:
            item["action"] = action or remedy(family, subject, state in {"grey", "stale"})
            if state in {"grey", "stale"}:
                item["blocked_reason"] = "Fresh evidence required from the named collector and environment."
        existing = checks.get(identity)
        if existing is None or ORDER[state] < ORDER[existing["status"]]:
            checks[identity] = item

    def walk(family, value, path, inherited_ts):
        """Read typed child results without scanning history or deriving thresholds."""
        if isinstance(value, list):
            for i, row in enumerate(value):
                walk(family, row, f"{path}/{i}", inherited_ts)
            return
        if not isinstance(value, dict):
            return
        ts = value.get("ts") or value.get("observed_at") or value.get("at") or inherited_ts
        if family == "unit_timings":
            if value.get("package") and "seconds" in value:
                family = "import_time"
            elif value.get("nodeid"):
                family = "unit_test_timing"
        subject = "/".join(str(value[k]) for k in ("repo", "name", "workspace", "project", "workflow", "package", "entry", "nodeid", "test", "file", "check", "python") if value.get(k)) or path
        raw = value.get("state", value.get("status"))
        if value.get("conclusion"):
            raw = value["conclusion"]
        if not any(value.get(k) for k in ("repo", "name", "workspace", "project", "workflow", "package", "entry", "nodeid", "test", "file", "check")) and value.get("reason"):
            subject = str(value["reason"])
        if value.get("marker") in {"SLOW", "NEEDS_FIX"}:
            raw = "yellow"
        elif value.get("marker") == "permanent":
            raw = "na"
        if value.get("error") or value.get("available") is False or value.get("observed") is False:
            raw = "grey"
        if value.get("ok") is False or value.get("ready") is False:
            raw = "red"
        if value.get("kind") in {"suspect_cancelled", "timed_out", "timeout", "hang", "kill_timer"}:
            raw = "red"
        if raw is not None:
            summary = str(value.get("reason") or value.get("detail") or value.get("error") or value.get("marker") or raw)
            action = value.get("action")
            if value.get("prompt"):
                action = {"kind": "prompt", "payload": value["prompt"]}
            # Semantic identity is stable under row reordering and deduplicates
            # the same measurement in rows/slowed/events projections.
            discriminator = value.get("run_id") or value.get("run_url") or ""
            identity = f"{family}:{subject}:{discriminator}"
            add(family, subject, raw, summary, value, source=f"snapshot.{family}:{path}",
                observed_at=ts, action=action, identity=identity,
                na_reason="Permanent exclusion by design" if value.get("marker") == "permanent" else None)
        for key, rows in value.items():
            if key in {"history", "spark", "thresholds", "evidence", "action", "commit_shas"}:
                continue
            if key in {"red", "yellow", "events", "errors", "findings", "failures", "problems",
                       "orphans", "orphaned", "missing", "dirty", "canonical_dirty", "packages_unavailable", "repos_unavailable",
                       "failing_scripts", "parked_stale"} and isinstance(rows, list):
                for i, row in enumerate(rows):
                    row = dict(row) if isinstance(row, dict) else {"reason": str(row)}
                    row.setdefault("state", "red" if key in {"red", "events", "failures", "dirty", "missing", "failing_scripts"} else
                                   "grey" if key in {"errors", "packages_unavailable", "repos_unavailable"} else "yellow")
                    walk(family, row, f"{path}/{key}/{i}", ts)
            elif isinstance(rows, (list, dict)):
                walk(family, rows, f"{path}/{key}", ts)

    section_map = {ALIASES.get(s.key, s.key): s for s in board.sections}
    selected = FAMILIES if families is None else families
    for family in selected:
        sec = section_map.get(family)
        data = snapshot.get(family)
        # Unit summaries are generated from the unit artifact collector, whose
        # timestamp is the evidence timestamp (aggregate time must not refresh it).
        ts = (data.get("ts") if isinstance(data, dict) else None)
        if family in {"import_time", "unit_test_timing"} and not ts and not (isinstance(data, dict) and "ts" in data):
            ts = (snapshot.get("unit_timings") if isinstance(snapshot.get("unit_timings"), dict) else {}).get("ts")
        ds = (devbox.get("sections") or {}).get(family)
        # The publication envelope timestamp is not an observation timestamp.
        # Consume the uncapped inventory even when no legacy section exists.
        if not family_data(snapshot, family) and isinstance(ds, dict):
            rows, complete = published_checks(family, ds)
            for item in rows:
                add(family, item["subject"], item["status"], item["summary"],
                    item.get("evidence"), observed_at=item.get("observed_at"),
                    action=item.get("action"), identity=item["id"],
                    source=f"devbox.sections.{family}", na_reason=item.get("applicability_reason"))
            if not complete:
                add(family, FAMILIES.get(family, family), "grey",
                    "Published summary lacks the complete inventory; republish from the dev box",
                    identity=f"{family}:coverage", source=f"devbox.sections.{family}")
            continue
        label = FAMILIES.get(family, family)
        state = status(sec.state) if sec else "grey"
        summary = sec.summary if sec else "No observation available"
        if isinstance(data, dict) and data and not sec:
            if data.get("available") is True and "checks" in data:
                state = "green" if all(c.get("ok") for c in data["checks"].values()) and data["checks"] else "grey"
            elif data.get("available") is True and "repos" in data:
                state = "yellow" if (data.get("drift_count") or data.get("missing_count") or data.get("error_count")) else "green"
            elif "workspaces" in data:
                state = "green" if data["workspaces"] and all(str(w.get("status")).upper() in {"OK", "SATISFIABLE"} for w in data["workspaces"]) else "grey"
            summary = f"{label}: {state}"
        if isinstance(data, dict) and (data.get("available") is False or data.get("observed") is False):
            state, summary = "grey", str(data.get("reason") or "Collector could not observe this check")
        # An INFO state may mean an unfinished baseline; only explicit permanent
        # exclusions are inapplicable. No inference from colours alone.
        na_reason = None
        if family == "no_run_census" and isinstance(data, dict):
            totals = data.get("totals") or {}
            if totals.get("permanent") and not totals.get("slow") and not totals.get("needs_fix"):
                na_reason = "Only permanent exclusions by design; no repairable skips"
        if family in {"script_timing", "unit_test_timing", "workspace_testmode_timing"} and isinstance(data, dict):
            if not any(data.get(k) for k in ("green_count", "red_count", "yellow_count")):
                state, summary = "grey", "No comparisons measured; collect timings and establish baselines"
        add(family, label, state, summary, observed_at=ts,
            action=sec.action if sec and state not in {"grey", "stale"} else None,
            identity=f"{family}:coverage", na_reason=na_reason)
        if isinstance(data, dict):
            for counter in ("new_scripts_no_baseline", "building_count", "new_tests_no_baseline", "new_packages_no_baseline"):
                if data.get(counter):
                    add(family, counter, "grey", f"{data[counter]} observations lack a baseline comparison", observed_at=ts)
            if data.get("orphaned_count", 0) > len(data.get("orphaned") or []):
                add(family, "orphaned baseline details", "grey", "Collector omitted orphaned baseline details; refresh the complete collector", observed_at=ts)
        if data:
            walk(family, data, family, ts)
    # Raw unit evidence carries individual rows omitted from legacy summaries.
    if isinstance(snapshot.get("unit_timings"), dict) and snapshot["unit_timings"]:
        walk("unit_timings", snapshot["unit_timings"], "unit_timings", snapshot["unit_timings"].get("ts"))

    for sec in board.sections:
        family = ALIASES.get(sec.key, sec.key)
        if family not in selected and not sec.entries:
            na = "Only active PR information; no failed checks" if sec.state == "info" and sec.entries and all(e.get("state") in {"ok", "info"} and (e.get("state") == "ok" or e.get("key") == "open_pr") for e in sec.entries) else None
            add(family, sec.title, sec.state, sec.summary, observed_at=board.ts, action=sec.action, identity=f"{family}:coverage", na_reason=na)
        for entry in sec.entries:
            state = entry.get("state")
            na = "Open PR is active work, not a failed check" if entry.get("key") == "open_pr" and state == "info" else None
            entry_family = {"CI": "ci_status", "local checkout": "repo_state", "GitHub PRs": "open_prs"}.get(entry.get("original_source", entry.get("source")), family)
            # Full repo observations below cover these missing local summaries.
            if entry.get("key") == "checkout_unobserved":
                subject = entry.get("subject")
                raw_local = ((snapshot.get("repos") or {}).get(subject) or {}).get("repo_state") or {}
                published_local = ((devbox.get("repo_observations") or {}).get(subject) or {}).get("repo_state") or {}
                if raw_local.get("branch") or published_local.get("branch"):
                    continue
            add(entry_family, entry.get("subject", sec.title), state, entry.get("reason", ""), entry.get("evidence"),
                observed_at=entry.get("observed_at"), action=entry.get("action"), identity=entry.get("id"), na_reason=na)
    for name in expected_repos() if repos is None else repos:
        data = (snapshot.get("repos") or {}).get(name) or {}
        published_repo = (devbox.get("repo_observations") or {}).get(name) or {}
        for family in ("ci_status", "repo_state", "open_prs"):
            obs = data.get(family) or {}
            if family == "repo_state" and not obs.get("branch") and published_repo.get("repo_state"):
                obs = published_repo["repo_state"]
            observed = isinstance(obs, dict) and bool(obs) and not obs.get("error")
            if family == "ci_status":
                observed = observed and bool(obs.get("conclusion") or obs.get("status") or obs.get("workflows"))
            if family == "repo_state":
                observed = observed and bool(obs.get("branch"))
            if family == "open_prs":
                observed = observed and "open_count" in obs
            add(family, name, "green" if observed else "grey", "Observed" if observed else "Observation missing or unavailable",
                observed_at=obs.get("ts"), identity=f"{family}:{name}:coverage")
            if family == "repo_state" and observed:
                for key in ("dirty_real", "behind", "ahead"):
                    if obs.get(key):
                        add(family, name + ":" + key, "yellow", f"{key}: {obs[key]}", observed_at=obs.get("ts"), evidence=obs)
                if obs.get("branch") != "main":
                    add(family, name + ":branch", "yellow", "Checkout is not on main", observed_at=obs.get("ts"), evidence=obs)
            walk(family, obs, name, obs.get("ts"))
    for item in board.blockers:
        add("release", item.get("repo") or "Release readiness", item["severity"], item["text"], item,
            identity="release:" + hashlib.sha256(item["text"].encode()).hexdigest()[:16],
            action={"kind": "prompt", "payload": item["prompt"]} if item.get("prompt") else None, observed_at=board.ts)
    if board.verdict != "green" and not board.blockers:
        add("release", "Release readiness", board.verdict, "Release verdict requires attention")
    if timestamp(board.ts) is None:
        add("snapshot", "Heart snapshot", "grey", "Snapshot timestamp missing")

    inventory = sorted(checks.values(), key=lambda c: (ORDER[c["status"]], c["id"]))
    findings = [c for c in inventory if c["status"] not in {"green", "na"}]
    by_family = {}
    for finding in findings:
        by_family.setdefault(finding["family"], []).append(finding)
    penalties = [{"key": key, "count": len(rows), "weight": max(POINTS[r["status"]] for r in rows), "cap": 10,
                  "points": max(POINTS[r["status"]] for r in rows)}
                 for key, rows in sorted(by_family.items())]
    return {"schema_version": 1, "status": findings[0]["status"] if findings else "green",
            "score": max(0, 100 - sum(p["points"] for p in penalties)),
            "complete": not findings, "counts": dict(Counter(c["status"] for c in inventory)),
            "checks": inventory, "findings": findings, "penalties": penalties}
