"""Decide whether tonight's workspace smoke sweep runs, and record its SHAs.

The first job of workspace-smoke.yml (main only) and of
workspace-smoke-pinned.yml (bisect runs) runs this. It resolves the commit
every repo the sweep covers will be tested at (the `main` head, or the
SHA/ref the pinned entry's `commit_shas` input names), compares that map with
the record of the last successful run, and writes:

  shas.json            the record, uploaded as the `workspace-smoke-shas`
                       artifact of EVERY run (swept or skipped);
  $GITHUB_OUTPUT       sweep=true|false and commit_shas=<compact JSON>;
  $GITHUB_STEP_SUMMARY one headline plus a table of the tested SHAs.

The sweep is skipped only when all of these hold: nothing was pinned, the
dispatch did not set force, the latest completed run on main concluded
success (a red run at the same SHAs must be swept again, not masked by an
older green one), a previous record exists and is readable, every covered
repo's SHA equals that record's, and the last full sweep is younger than
FULL_SWEEP_MAX_AGE_DAYS (so a dependency that moved on PyPI without any
`main` moving is still caught within a week, as before). Anything else runs
the sweep. A previous record that expired or predates this script counts as
"no record": run the sweep.

Heart's test_run leg reads the record: a skipped run carries `swept: false`
and the id of the full sweep it re-attests (`full_sweep_run_id`), so Heart
takes the counts from that run and never reports the skip as a fresh sweep.

Environment (all optional except where the workflow sets them):
  PINS              JSON {repo: sha-or-ref}; default "{}" (pinned entry only)
  FORCE             "true" sweeps even when nothing moved
  LAST_RUN_ID, LAST_RUN_CONCLUSION   latest completed run on main (not this one)
  PREVIOUS_RECORD   path to the last successful run's shas.json
  PREVIOUS_RUN_ID   that run's id
  RUN_ID, RUN_URL, EVENT   this run's identity
Stdlib only: it runs on the bare runner python3 before any setup step.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

# Must match what workspace-validation.yml checks out: the libraries cloned
# in run_scripts/run_notebooks and the workspace map in find_scripts.
# tests/test_smoke_sha_gate.py asserts the two lists agree.
LIBRARIES = (
    "PyAutoNerves", "PyAutoArray", "PyAutoFit", "PyAutoGalaxy", "PyAutoLens",
)
WORKSPACES = (
    "autofit_workspace", "autogalaxy_workspace", "autolens_workspace",
    "autofit_workspace_test", "autogalaxy_workspace_test", "autolens_workspace_test",
    "HowToGalaxy", "HowToLens", "HowToFit",
    "euclid_strong_lens_modeling_pipeline",
)
COVERED = LIBRARIES + WORKSPACES
OWNER = "PyAutoLabs"
FULL_SWEEP_MAX_AGE_DAYS = 7
SCHEMA = 1
_SHA = re.compile(r"[0-9a-f]{40}")


def ls_remote(repo: str, ref: str) -> str:
    """The commit `ref` names in PyAutoLabs/<repo>, or "" if none."""
    out = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{OWNER}/{repo}", ref],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name in (ref, f"refs/heads/{ref}", f"refs/tags/{ref}"):
            return sha
    return ""  # short SHAs and unknown refs: resolve() reports them


def parse_pins(raw: str | None) -> dict[str, str]:
    """The dispatch input as a {repo: ref} dict; rejects unknown repos."""
    pins = json.loads(raw or "{}") or {}
    if not isinstance(pins, dict):
        raise ValueError("commit_shas must be a JSON object {repo: sha}")
    unknown = sorted(set(pins) - set(COVERED))
    if unknown:
        raise ValueError(
            f"commit_shas names repos the sweep does not cover: {unknown}; "
            f"covered: {list(COVERED)}"
        )
    return {k: str(v).strip() for k, v in pins.items() if str(v).strip()}


def resolve(pins: dict[str, str], lookup: Callable[[str, str], str] = ls_remote) -> dict[str, str]:
    """Full 40-hex SHA for every covered repo: the pin if given, else main."""
    shas = {}
    for repo in COVERED:
        ref = pins.get(repo, "refs/heads/main")
        sha = ref.lower() if _SHA.fullmatch(ref.lower()) else lookup(repo, ref)
        if not _SHA.fullmatch(sha or ""):
            raise ValueError(f"{repo}: cannot resolve {ref!r} to a commit")
        shas[repo] = sha
    return shas


def _age_days(ts: str | None, now: datetime.datetime) -> float | None:
    try:
        then = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - then).total_seconds() / 86400


def decide(
    shas: dict[str, str],
    pins: dict[str, str],
    previous: dict | None,
    previous_run_id: str,
    now: datetime.datetime,
    force: bool = False,
    last_run: tuple[str, str] | None = None,
) -> tuple[bool, str, dict]:
    """(sweep, reason, record) for this run.

    last_run is (id, conclusion) of the latest completed run on main, or None
    when there is none."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    full_id, full_ts = None, None
    if pins:
        sweep, reason = True, f"pinned by dispatch: {', '.join(sorted(pins))}"
    elif force:
        sweep, reason = True, "forced by dispatch"
    elif last_run and last_run[1] != "success":
        sweep, reason = True, f"last run {last_run[0]} concluded {last_run[1] or 'unknown'}"
    elif not isinstance(previous, dict) or not isinstance(previous.get("commit_shas"), dict):
        sweep = True
        reason = (
            f"no SHA record from the last successful run ({previous_run_id or 'none'}); "
            "running the sweep"
        )
    else:
        full_id = previous.get("full_sweep_run_id") or previous_run_id
        full_ts = previous.get("full_sweep_created_at")
        moved = sorted(r for r in COVERED if previous["commit_shas"].get(r) != shas[r])
        age = _age_days(full_ts, now)
        if moved:
            sweep, reason = True, f"main moved since run {previous_run_id}: {', '.join(moved)}"
        elif age is None or age >= FULL_SWEEP_MAX_AGE_DAYS:
            sweep = True
            reason = (
                f"no main moved, but the last full sweep (run {full_id}) is "
                f"{'of unknown age' if age is None else f'{age:.1f} days old'}; "
                f"a full sweep runs at least every {FULL_SWEEP_MAX_AGE_DAYS} days"
            )
        else:
            sweep, reason = False, f"no main moved since run {full_id}"
    run_id = os.environ.get("RUN_ID", "")
    record = {
        "schema": SCHEMA,
        "run_id": run_id,
        "run_url": os.environ.get("RUN_URL", ""),
        "event": os.environ.get("EVENT", ""),
        "created_at": stamp,
        "swept": sweep,
        "reason": reason,
        "full_sweep_run_id": run_id if sweep else full_id,
        "full_sweep_created_at": stamp if sweep else full_ts,
        "previous_run_id": previous_run_id or None,
        "pinned": pins,
        "commit_shas": shas,
    }
    return sweep, reason, record


def summary_markdown(record: dict) -> str:
    head = (
        f"**Workspace sweep running:** {record['reason']}."
        if record["swept"]
        else f"**Workspace sweep skipped: {record['reason']}.** "
        f"These SHAs passed the full sweep in run {record['full_sweep_run_id']}."
    )
    rows = [
        f"| {repo} | `{sha}` | {'pinned' if repo in record['pinned'] else 'main'} |"
        for repo, sha in record["commit_shas"].items()
    ]
    return "\n".join(
        [head, "", "| repo | commit | source |", "|---|---|---|", *rows, ""]
    )


def main() -> int:
    pins = parse_pins(os.environ.get("PINS"))
    previous = None
    prev_path = os.environ.get("PREVIOUS_RECORD", "")
    if prev_path and Path(prev_path).is_file():
        try:
            previous = json.loads(Path(prev_path).read_text())
        except ValueError:
            previous = None
    shas = resolve(pins)
    last_id = os.environ.get("LAST_RUN_ID", "")
    sweep, reason, record = decide(
        shas, pins, previous, os.environ.get("PREVIOUS_RUN_ID", ""),
        datetime.datetime.now(datetime.timezone.utc),
        force=os.environ.get("FORCE", "").lower() == "true",
        last_run=(last_id, os.environ.get("LAST_RUN_CONCLUSION", "")) if last_id else None,
    )
    Path("shas.json").write_text(json.dumps(record, indent=2) + "\n")
    compact = json.dumps(shas, separators=(",", ":"))
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as fh:
        fh.write(f"sweep={'true' if sweep else 'false'}\ncommit_shas={compact}\n")
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as fh:
        fh.write(summary_markdown(record))
    print(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
