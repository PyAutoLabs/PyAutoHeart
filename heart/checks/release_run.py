"""heart/checks/release_run.py — dev-box freshness for the release channel.

``validation_report.json`` historically refreshed only on a manual local
``pyauto-heart validate --ingest``: the nightly driver ingests into an
ephemeral CI state dir, so after every library merge the dev box reported
``release validation stale: source moved since rehearsal`` until someone
manually ingested — even though a fresh rehearsal ran (or failed, with
evidence) last night on the ``release-integrate.yml`` channel.

This check mirrors ``test_run.py``'s cached-artifact pattern: the tick reads
the channel's latest run (conclusion + id via ``gh run list``), downloads its
``release-stage-report`` artifact once per run id, and — when the cloud run is
COMPLETED and NEWER than the ingested report — refreshes ``validation_report``
through the existing ``heart.validate.run()`` ingest path (stage reports embed
their own ``commit_shas`` / ``testpypi_version``). Rules:

- a fresher local ingest is never regressed (report ts vs run creation time);
- a run already ingested (sidecar-cached id) is never re-downloaded;
- a FAILED rehearsal ingests too: ``release_ready: false`` is evidence, not an
  evidence gap — readiness then shows the accurate ``release validation
  FAILED (stage integrate)`` instead of week-old STALE, and it self-clears on
  the next green night.

``decide()`` is pure and no-network: the gh-backed callables are injected only
by the ``main()`` tick/CLI entrypoint (the #83/#120 discipline).
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

HEART_HOME = Path(__file__).resolve().parents[2]
HEART_STATE_DIR = Path(os.environ.get("HEART_STATE_DIR") or Path.home() / ".pyauto-heart")

RELEASE_REPO = os.environ.get("GITHUB_REPOSITORY", "PyAutoLabs/PyAutoHeart")
RELEASE_WORKFLOW = "release-integrate.yml"
STAGE_ARTIFACT = "release-stage-report"
SIDECAR = HEART_STATE_DIR / "release_run.json"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _parse_ts(ts: Any) -> datetime.datetime | None:
    try:
        t = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=datetime.timezone.utc) if t.tzinfo is None else t


def latest_run() -> dict[str, Any] | None:
    """Latest release-integrate run via ``gh``; None if unavailable/no runs."""
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--repo", RELEASE_REPO,
             "--workflow", RELEASE_WORKFLOW, "--limit", "1",
             "--json", "conclusion,status,createdAt,databaseId,url"],
            capture_output=True, text=True, timeout=30,
        )
        runs = json.loads(out.stdout or "[]")
    except Exception:
        return None
    return runs[0] if runs else None


def download_stage_report(run_id: Any, dest: Path) -> Path | None:
    """Fetch the run's stage-report artifact into ``dest``; None on any failure."""
    try:
        res = subprocess.run(
            ["gh", "run", "download", str(run_id), "--repo", RELEASE_REPO,
             "-n", STAGE_ARTIFACT, "-D", str(dest)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    report = dest / "stage_report.json"
    return report if res.returncode == 0 and report.is_file() else None


def decide(
    current_report: dict[str, Any] | None,
    sidecar: dict[str, Any] | None,
    run_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """Pure refresh decision: {action, run_id?, created?, url?}.

    Actions: no-runs · in-progress · cached (already ingested) ·
    local-fresher (never regress a newer local ingest) · ingest.
    """
    if not run_record:
        return {"action": "no-runs"}
    run_id = run_record.get("databaseId") or run_record.get("id")
    out = {
        "run_id": run_id,
        "conclusion": run_record.get("conclusion"),
        "created": run_record.get("createdAt") or run_record.get("created_at"),
        "url": run_record.get("url") or run_record.get("html_url"),
    }
    if run_record.get("status") != "completed":
        return {**out, "action": "in-progress"}
    if isinstance(sidecar, dict) and sidecar.get("last_ingested_run_id") == run_id:
        return {**out, "action": "cached"}
    report_ts = _parse_ts((current_report or {}).get("ts"))
    created = _parse_ts(out["created"])
    if report_ts is not None and created is not None and report_ts >= created:
        return {**out, "action": "local-fresher"}
    return {**out, "action": "ingest"}


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(HEART_HOME))
    from heart import state, validate

    decision = decide(validate.load(), _read_json(SIDECAR), latest_run())
    action = decision.get("action")
    ingested: dict[str, Any] | None = None

    if action == "ingest":
        with tempfile.TemporaryDirectory() as td:
            report_path = download_stage_report(decision.get("run_id"), Path(td))
            if report_path is None:
                action = decision["action"] = "artifact-unavailable"
            else:
                ingested = validate.run([td])
                # Record the ingest so the tick never re-downloads this run.
                state.atomic_write_json(SIDECAR, {
                    "last_ingested_run_id": decision.get("run_id"),
                    "ingested_ts": ingested.get("ts"),
                    "release_ready": ingested.get("release_ready"),
                    "run_url": decision.get("url"),
                })

    from heart.heart_color import c_fail, c_info, c_meta, c_ok, c_warn, glyph_fail, glyph_ok, glyph_warn

    label_id = decision.get("run_id", "?")
    if action == "ingest":
        ready = (ingested or {}).get("release_ready")
        if ready is True:
            glyph, label = glyph_ok(), c_ok(f"rehearsal ingested (run {label_id}: pass)")
        else:
            glyph, label = glyph_fail(), c_fail(f"rehearsal ingested (run {label_id}: FAILED)")
    elif action in ("cached", "local-fresher"):
        glyph, label = glyph_ok(), c_ok(f"validation report current ({action})")
    elif action == "in-progress":
        glyph, label = glyph_warn(), c_warn(f"rehearsal run {label_id} in progress")
    else:  # no-runs / artifact-unavailable
        glyph, label = glyph_warn(), c_warn(action or "unknown")
    print(f"{glyph} {c_info('release_run')} {label} {c_meta(str(decision.get('url') or ''))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
