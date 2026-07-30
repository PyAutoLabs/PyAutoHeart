"""heart/checks/test_run.py — surface the latest PyAutoHands test-run verdict.

PyAutoHands's release pipeline writes an aggregated ``report.json`` into each
run directory (reachable via the ``run_logs/latest`` symlink). It carries
the single most important release signal — a top-level ``ready`` boolean —
plus per-status counts, per-project breakdown, and the ``slow_skips`` /
``needs_fix_skips`` lists (each entry already carrying an ``is_stale`` flag
computed by ``slow_skip_check.py``).

This check reads that file (no heavy imports, just JSON) and emits
``$HEART_STATE_DIR/test_run.json`` so the readiness verdict and the status
dashboard can consume it continuously, instead of the signal only existing at
release time.

Older runs predate ``report.json``; for those we fall back to summing the
per-job ``*__script.json`` ``summary`` blocks, set ``ready`` to ``None`` (the
verdict treats that as "unknown", a yellow — never a silent green), and mark
the parked-script counts unknown.

When the verdict comes from a cloud run and no local report exists, the run's
own ``workspace-validation-report`` artifact supplies the real counts and
failing-script names (fetched once per run id by the tick entrypoint). A
summary must never carry counts nobody measured: ``counts_measured`` says
whether the passed/failed/skipped/timeout keys are real, and every consumer
(readiness, dashboard, this module's own CLI line) checks it before printing.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

# Reuse the same root/latest resolution as script_timing.py.
HEART_HOME = Path(__file__).resolve().parents[2]
_p3 = Path(__file__).resolve().parents[3]
PYAUTO_ROOT = _p3 if _p3.name == "PyAutoLabs" else Path.home() / "Code" / "PyAutoLabs"
TEST_RESULTS_LATEST = PYAUTO_ROOT / "PyAutoHands" / "run_logs" / "latest"
HEART_STATE_DIR = Path(
    os.environ.get("HEART_STATE_DIR")
    or Path.home() / ".pyauto-heart"
)

# The cloud SMOKE channel (Heart-owned entry workflow workspace-smoke.yml,
# calling the shared workspace-validation.yml body) is the continuous source of
# the workspace-integration verdict. The continuous verdict may only come from
# this channel — release rehearsals run on their own entry
# (release-integrate.yml) and reach readiness via `validate --ingest`, so a
# failed rehearsal can never overwrite the smoke verdict here. The tick reads
# only the conclusion + timestamp (cheap, same budget as ci_status); count
# detail comes from a local `autohands run_all` or the run's report artifact.
VALIDATION_REPO = os.environ.get("GITHUB_REPOSITORY", "PyAutoLabs/PyAutoHeart")
VALIDATION_WORKFLOW = "workspace-smoke.yml"
# The aggregated per-run report the workflow's `analyze` job uploads; it holds
# the real counts and per-script failures for a cloud run — the only place they
# exist when there is no local run_logs/.
REPORT_ARTIFACT = "workspace-validation-report"

# Agent/MCP-supplied conclusion drop point. On a mobile/cloud session there is no
# `gh` (and no local report.json); Brain queries the run conclusion via its MCP
# GitHub tools and writes it here so the server signal still reaches readiness.
# Same shape as `_cloud_verdict()`: {ready, ts, run_id, url}. Overridable for
# tests via HEART_VALIDATION_FILE.
VALIDATION_FILE = Path(
    os.environ.get("HEART_VALIDATION_FILE")
    or (HEART_STATE_DIR / "cloud_validation.json")
)


def _verdict_from_run(r: dict[str, Any]) -> dict[str, Any]:
    """Normalise one Actions run record into {ready, ts, run_id, url}.

    ready is True/False on a completed run, None while in progress. Accepts both
    the `gh`/REST shape (`databaseId`) and the MCP shape (`id`)."""
    conclusion = r.get("conclusion")
    status = r.get("status")
    ready: bool | None = None if status != "completed" else (conclusion == "success")
    return {
        "ready": ready,
        "ts": r.get("createdAt") or r.get("created_at"),
        "run_id": r.get("databaseId") or r.get("id"),
        "url": r.get("url") or r.get("html_url"),
    }


def _agent_supplied_verdict() -> dict[str, Any] | None:
    """Read a Brain/MCP-supplied conclusion file, or None if absent/malformed.

    The file may hold either an already-normalised verdict ({ready, ts, ...}) or
    a raw Actions run record (with conclusion/status) which we normalise."""
    data = _read_json(VALIDATION_FILE)
    if not isinstance(data, dict) or not data:
        return None
    if "ready" in data:
        return {
            "ready": data.get("ready"),
            "ts": data.get("ts") or data.get("createdAt"),
            "run_id": data.get("run_id") or data.get("databaseId") or data.get("id"),
            "url": data.get("url") or data.get("html_url"),
        }
    if "conclusion" in data or "status" in data:
        return _verdict_from_run(data)
    return None


def _cloud_verdict() -> dict[str, Any] | None:
    """Latest cloud workspace-validation run via `gh`: {ready, ts, run_id, url}.

    ready is True/False on a completed run, None while in progress. Never raises;
    returns None if gh is unavailable or no run exists."""
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--repo", VALIDATION_REPO,
             "--workflow", VALIDATION_WORKFLOW, "--limit", "1",
             "--json", "conclusion,status,createdAt,databaseId,url"],
            capture_output=True, text=True, timeout=30,
        )
        runs = json.loads(out.stdout or "[]")
    except Exception:
        return None
    if not runs:
        return None
    return _verdict_from_run(runs[0])


def _server_verdict() -> dict[str, Any] | None:
    """Server-first workspace-validation conclusion, gh-independent.

    Prefers a Brain/MCP-supplied conclusion file (works with no `gh` on mobile),
    falling back to a direct `gh` query. This is the PRIMARY test_run signal;
    the local report.json is enrichment (count detail) only."""
    return _agent_supplied_verdict() or _cloud_verdict()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _from_report(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary", {}) or {}
    parked = []
    for key in ("slow_skips", "needs_fix_skips"):
        for entry in report.get(key, []) or []:
            if isinstance(entry, dict) and entry.get("is_stale"):
                parked.append(
                    {
                        "workspace": entry.get("workspace"),
                        "pattern": entry.get("pattern"),
                        "category": entry.get("category"),
                        "age_days": entry.get("age_days"),
                    }
                )
    # Compact failing-script names (the report's `failures` entries also carry
    # full tracebacks — those stay in the report, never in the sidecar).
    failing = []
    for entry in report.get("failures", []) or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("file") or "")
        directory = str(entry.get("directory") or "").strip("/")
        name = path.rsplit("/", 1)[-1]
        failing.append(
            {
                "project": entry.get("project"),
                "script": f"{directory}/{name}" if directory else path.lstrip("/"),
                "status": entry.get("status"),
            }
        )
        if len(failing) >= 10:
            break
    return {
        "ready": report.get("ready"),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "skipped": summary.get("skipped", 0),
        "timeout": summary.get("timeout", 0),
        "per_project": report.get("per_project", {}) or {},
        "run_label": report.get("run_label", ""),
        "parked_stale_count": len(parked),
        "parked_stale": parked,
        "failing_scripts": failing,
        "counts_measured": True,
        # The surface this report measured (projects/shards/run_types/env
        # profile). Carried through so the leg's history is comparable at all:
        # a count is only a trend against the same denominator (#83 §5.3).
        "surface": report.get("surface"),
        "source": "report",
    }


def _from_per_job(results_dir: Path) -> dict[str, Any]:
    """Fallback: sum the per-job ``*__script.json`` summaries. We can't know
    overall readiness from these alone, so ready is left unknown (None)."""
    totals = {"passed": 0, "failed": 0, "skipped": 0, "timeout": 0}
    per_project: dict[str, dict[str, int]] = {}
    found = False
    for jp in sorted(results_dir.glob("*__script.json")):
        data = _read_json(jp)
        if not isinstance(data, dict):
            continue
        found = True
        s = data.get("summary", {}) or {}
        proj = data.get("project", "?")
        pp = per_project.setdefault(proj, {})
        for k in totals:
            v = int(s.get(k, 0) or 0)
            totals[k] += v
            if v:
                pp[k] = pp.get(k, 0) + v
    if not found:
        return {}
    return {
        "ready": None,
        **totals,
        "per_project": per_project,
        "run_label": results_dir.resolve().name,
        "parked_stale_count": 0,
        "parked_stale": [],
        "failing_scripts": [],
        "counts_measured": True,
        "source": "per-job",
    }


def _cloud_report(run_id: Any) -> dict[str, Any] | None:
    """Download run_id's aggregated report artifact and parse its report.json.

    Never raises; None if gh is unavailable, the artifact expired, or the JSON
    is malformed. The artifact is a few kB — cheap, but still network: callers
    cache per run_id (see main()) so the <30s tick pays at most one download
    per new validation run."""
    if not run_id:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            res = subprocess.run(
                ["gh", "run", "download", str(run_id), "--repo", VALIDATION_REPO,
                 "-n", REPORT_ARTIFACT, "-D", td],
                capture_output=True, text=True, timeout=60,
            )
            if res.returncode != 0:
                return None
            report = _read_json(Path(td) / "report.json")
    except Exception:
        return None
    return report if isinstance(report, dict) else None


def counts_measured(summary: dict[str, Any]) -> bool:
    """True if the summary's counts came from an actual report.

    Legacy sidecars predate the explicit flag: the cloud-only shape (source
    "cloud" with a synthesised ``cloud#<id>`` run_label and no local report)
    is exactly the shape whose zeros were fabricated — everything else parsed
    a real report. Consumers (readiness, dashboard) must never print counts
    for which this is False."""
    if "counts_measured" in summary:
        return bool(summary["counts_measured"])
    return not (
        summary.get("source") == "cloud"
        and str(summary.get("run_label", "")).startswith("cloud#")
    )


def run(
    results_dir: Path | None = None,
    fetch_cloud: bool | None = None,
    cloud_report_fetcher: Callable[[Any], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    # fetch_cloud is decided by the CALLER, never inferred: the old
    # `results_dir is None` inference meant main() — the tick and the mobile
    # path — always disabled the cloud fetch, leaving the leg on stale local
    # evidence while claiming to be server-first (PyAutoHeart#83 finding A).
    # Library/test callers default to no network.
    results_dir = results_dir or TEST_RESULTS_LATEST
    if fetch_cloud is None:
        fetch_cloud = False

    summary: dict[str, Any]
    report = _read_json(results_dir / "report.json")
    if isinstance(report, dict):
        summary = _from_report(report)
    else:
        summary = _from_per_job(results_dir)

    report_path = results_dir / "report.json"
    if summary and report_path.is_file():
        summary["ts"] = datetime.datetime.fromtimestamp(
            report_path.stat().st_mtime, datetime.timezone.utc
        ).isoformat()

    # The server workspace-validation run (MCP-supplied file, else `gh`) sets
    # ready/ts so a missing local report.json no longer forces "unknown" when
    # the cloud run is green. The local report, when present, still supplies
    # the count detail. When BOTH surfaces carry a verdict they must agree to
    # be green — a disagreement is surfaced, never silently resolved in either
    # direction (PyAutoHeart#83 §4-§5: a fresh local pass must not green the
    # leg while the server surface fails, and vice versa).
    cloud = _server_verdict() if fetch_cloud else None
    if cloud is not None:
        local_ready = summary.get("ready") if summary else None
        had_local = bool(summary)
        if not summary:
            # No local report: these zeros are placeholders, not measurements —
            # counts_measured stays False unless the run's own artifact report
            # supplies real counts below.
            summary = {
                "passed": 0, "failed": 0, "skipped": 0, "timeout": 0,
                "per_project": {}, "parked_stale_count": 0, "parked_stale": [],
                "failing_scripts": [], "counts_measured": False,
            }
        # Enrich from the run's own aggregated report artifact (counts + the
        # failing script names). The fetcher is injected by the tick/CLI
        # entrypoint only — run() itself stays no-network by default.
        if cloud_report_fetcher is not None and cloud["ready"] is not None:
            cs = cloud_report_fetcher(cloud["run_id"])
            if isinstance(cs, dict):
                counts = {
                    k: int(cs.get(k, 0) or 0)
                    for k in ("passed", "failed", "skipped", "timeout")
                }
                summary["cloud_counts"] = counts
                summary["failing_scripts"] = list(cs.get("failing_scripts") or [])[:10]
                summary["cloud_report_run_id"] = cloud["run_id"]
                if not had_local:
                    summary.update(counts)
                    summary["counts_measured"] = True
        summary["cloud_ready"] = cloud["ready"]
        if local_ready is None:
            summary["ready"] = cloud["ready"]
        elif cloud["ready"] is None:
            summary["ready"] = local_ready  # cloud run in progress: keep local
        else:
            summary["ready"] = bool(local_ready) and bool(cloud["ready"])
            if bool(local_ready) != bool(cloud["ready"]):
                summary["disagreement"] = (
                    f"local ready={local_ready} vs cloud ready={cloud['ready']}"
                )
        summary["ts"] = cloud["ts"]
        summary["run_label"] = summary.get("run_label") or f"cloud#{cloud['run_id']}"
        summary["cloud_url"] = cloud["url"]
        summary["source"] = "cloud"

    return summary


def main(argv: list[str]) -> int:
    results_dir = Path(argv[1]) if len(argv) > 1 else TEST_RESULTS_LATEST

    # Per-run-id cache over the previously persisted sidecar: the tick fires
    # every <30s but a new validation run appears ~twice a day, so the artifact
    # is downloaded once per run and replayed from state thereafter.
    prev = _read_json(HEART_STATE_DIR / "test_run.json")

    def _cached_cloud_counts(run_id: Any) -> dict[str, Any] | None:
        if (
            isinstance(prev, dict)
            and prev.get("cloud_report_run_id") == run_id
            and isinstance(prev.get("cloud_counts"), dict)
        ):
            return {
                **prev["cloud_counts"],
                "failing_scripts": prev.get("failing_scripts") or [],
            }
        report = _cloud_report(run_id)
        return _from_report(report) if isinstance(report, dict) else None

    # The tick/CLI is the real path: always consult the server verdict here,
    # explicitly — this is the entrypoint the old inference silently disabled.
    summary = run(results_dir, fetch_cloud=True, cloud_report_fetcher=_cached_cloud_counts)

    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    # Persist only here, at the tick/CLI entrypoint — run() is side-effect-free
    # so library callers (and the test suite) can never clobber live state.
    state.atomic_write_json(HEART_STATE_DIR / "test_run.json", summary)

    from heart.heart_color import c_ok, c_warn, c_fail, c_info, c_meta, glyph_ok, glyph_warn, glyph_fail

    if not summary:
        print(f"{c_meta('·')} {c_info('test_run')} {c_meta('(no run_logs yet)')}")
        return 0

    ready = summary.get("ready")
    failed = summary.get("failed", 0)
    measured = counts_measured(summary)
    if ready is False or (failed and measured):
        glyph = glyph_fail()
        label = c_fail(
            f"NOT ready ({failed} failed)"
            if measured
            else "NOT ready (counts not ingested — see run)"
        )
    elif ready is True:
        glyph = glyph_ok()
        label = c_ok("ready")
    else:
        glyph = glyph_warn()
        label = c_warn("ready unknown")
    if measured:
        extra = c_meta(
            f" {summary.get('passed', 0)}p/{failed}f/{summary.get('skipped', 0)}s"
            f" @ {summary.get('run_label', '?')}"
        )
    else:
        extra = c_meta(f" @ {summary.get('run_label', '?')}")
    print(f"{glyph} {c_info('test_run')} {label}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
