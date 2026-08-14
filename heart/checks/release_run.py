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
- a FAILED rehearsal ingests too: ``validation_outcome: "fail"`` is evidence,
  not an evidence gap — readiness then shows the accurate ``release validation
  FAILED (stage integrate)`` instead of week-old STALE, and it self-clears on
  the next green night.

The artifact this check downloads is an **integrate-only** stage report, so the
folded report can never carry a ``rehearse`` stage and its ``release_ready`` is
``false`` by construction — however green the run was. That is why severity is
read from ``validation_outcome`` (``incomplete``, an evidence gap → STALE) and
not from the boolean, which would report a failure that never happened.

``decide()`` is pure and no-network: the gh-backed callables are injected only
by the ``main()`` tick/CLI entrypoint (the #83/#120 discipline).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

HEART_HOME = Path(__file__).resolve().parents[2]
HEART_STATE_DIR = Path(os.environ.get("HEART_STATE_DIR") or Path.home() / ".pyauto-heart")

def _own_repo_slug() -> str:
    """owner/repo of this checkout's origin — the release channel lives here.

    Derived rather than hard-coded so the tenant firewall
    (PyAutoMind/scripts/repos_sync.py) holds: organ code carries no
    instance facts. Empty when there is no origin; the gh-backed callables
    then fail and the check reports unavailable instead of guessing.
    """
    try:
        url = subprocess.run(
            ["git", "-C", str(HEART_HOME), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError:
        url = ""
    m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?$", url)
    return m.group(1) if m else ""


RELEASE_REPO = os.environ.get("GITHUB_REPOSITORY") or _own_repo_slug()
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

    A report that predates ``validation_outcome`` is re-ingested once even when
    the run id is cached: without that, a report already folded by the old code
    would keep its missing discriminator forever, and the readiness gate — which
    fails closed on reports it cannot classify — would stay RED until some
    unrelated future run happened to come along.
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
    # A one-time re-fold for reports written before `validation_outcome` existed.
    #
    # Strictly limited to reports this check's own integrate-only artifact can
    # reproduce in full, because the re-fold OVERWRITES the canonical report:
    #
    #  - skipped when a `rehearse` stage is present — evidence from a manual
    #    multi-stage ingest during a release drive, which this artifact cannot
    #    reproduce; re-folding would turn its `pass` into an `incomplete`;
    #  - skipped when the stored report carries ANY adverse evidence — a failed
    #    stage, failing/timed-out counts in `totals` OR in any `per_project`
    #    entry, or a failures list. Those can come from a run that broke before
    #    it ever reached the rehearsal, and re-folding a green artifact over them
    #    would silently convert a real RED into a STALE. The definition of
    #    "adverse" must match `validate._Accumulator._has_adverse_evidence`;
    #    when it did not, per-project failures were an escape hatch.
    #  - triggered only when the field is genuinely ABSENT, not merely invalid.
    #    A present-but-malformed discriminator is graded RED on purpose, so
    #    treating it as "predates the schema" would let the migration overwrite
    #    the very report that RED rests on.
    #
    # Note the test is neither `cached` nor `local-fresher`: every ingest happens
    # after the run it ingests, so "the report is fresher than the run" says
    # nothing about where the report came from.
    report = current_report if isinstance(current_report, dict) else {}
    stages = report.get("stages")
    stages = stages if isinstance(stages, dict) else {}

    def _adverse_counts(counts: Any) -> bool:
        return bool(
            isinstance(counts, dict)
            and (counts.get("failed", 0) or counts.get("timeout", 0))
        )

    per_project = report.get("per_project")
    per_project = per_project if isinstance(per_project, dict) else {}
    stored_adverse = (
        any(isinstance(s, dict) and s.get("status") == "fail" for s in stages.values())
        or _adverse_counts(report.get("totals"))
        or any(_adverse_counts(c) for c in per_project.values())
        or bool(report.get("failures"))
    )
    stale_schema = (
        bool(report)
        and "rehearse" not in stages
        and not stored_adverse
        and "validation_outcome" not in report
    )
    if not stale_schema:
        if isinstance(sidecar, dict) and sidecar.get("last_ingested_run_id") == run_id:
            return {**out, "action": "cached"}
        report_ts = _parse_ts(report.get("ts"))
        created = _parse_ts(out["created"])
        if report_ts is not None and created is not None and report_ts >= created:
            return {**out, "action": "local-fresher"}
    return {**out, "action": "ingest"}


def resolve_outcome(ingested: dict[str, Any] | None) -> str:
    """``pass`` | ``fail`` | ``incomplete`` for an ingested report.

    Pure, like ``decide()``, so the tick's wording is testable without the
    network. Reports predating ``validation_outcome`` fall back to the legacy
    boolean and fail closed.
    """
    from heart import validate
    return validate.report_outcome(ingested) or "fail"


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
                # A run whose conclusion is not `success` is a failure even if
                # the stage report it uploaded says otherwise — the workflow can
                # break outside anything that artifact captures, and the artifact
                # is written by a step that may have run before the break.
                ingested = validate.run(
                    [td],
                    force_fail=str(decision.get("conclusion") or "").lower() != "success",
                )
                # Record the ingest so the tick never re-downloads this run.
                state.atomic_write_json(SIDECAR, {
                    "last_ingested_run_id": decision.get("run_id"),
                    "ingested_ts": ingested.get("ts"),
                    "release_ready": ingested.get("release_ready"),
                    "validation_outcome": ingested.get("validation_outcome"),
                    "run_url": decision.get("url"),
                })

    from heart.heart_color import c_fail, c_info, c_meta, c_ok, c_warn, glyph_fail, glyph_ok, glyph_warn

    label_id = decision.get("run_id", "?")
    if action == "ingest":
        outcome = resolve_outcome(ingested)
        if outcome == "pass":
            glyph, label = glyph_ok(), c_ok(f"rehearsal ingested (run {label_id}: pass)")
        elif outcome == "incomplete":
            # This is the ordinary state for this path: the artifact is an
            # integrate-only stage report, so it carries no rehearsal evidence.
            # Nothing failed — do not say FAILED.
            glyph, label = glyph_warn(), c_warn(
                f"integrate ingested (run {label_id}: no rehearsal evidence)"
            )
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
