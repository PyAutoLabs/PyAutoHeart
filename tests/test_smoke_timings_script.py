"""tests/test_smoke_timings_script.py — the shell leg's per-artifact failure path.

The load-bearing claim in `smoke_timings.sh` is that ONE artifact's 403 must not
cost the repo its other python leg: the failure is written to
`<downloads>/<id>.error` and the loop continues. Exercised the way
`test_ci_status_script.py` drives its helper — source the file, call the one
function, and put stub executables first on `PATH` so nothing here touches the
network, a real `gh`, or a real zip.

Fake names throughout (the tenant firewall).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "heart" / "checks" / "smoke_timings.sh"

OWNER_NAME = "OwnerX/RepoA"
REPO = "RepoA"

LISTING = {
    "artifacts": [
        {"id": 1, "name": "smoke-timings-3.11", "expired": False,
         "created_at": "2026-09-01T10:00:00Z",
         "workflow_run": {"id": 7, "head_branch": "feat/x", "head_sha": "abc123"}},
        {"id": 2, "name": "smoke-timings-3.12", "expired": False,
         "created_at": "2026-09-01T10:00:00Z",
         "workflow_run": {"id": 7, "head_branch": "feat/x", "head_sha": "abc123"}},
    ]
}

DATASET = {
    "schema": "smoke_timings/1", "project": "ProjA", "directory": "imaging",
    "run_type": "scripts", "env_profile": "smoke", "python": "3.11",
    "ts": "2026-09-01T09:55:00Z",
    "entries": [{"entry": "imaging/x.py", "kind": "script", "status": "passed",
                 "seconds": 12.0, "cap_s": 600.0, "exit_code": 0}],
}


def write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    """A stub `gh` (artifact 2 is a 403), a stub `unzip`, and a state dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "state" / "per-repo").mkdir(parents=True)
    (tmp_path / "listing.json").write_text(json.dumps(LISTING))
    (tmp_path / "dataset.json").write_text(json.dumps(DATASET))

    write_stub(bin_dir, "gh", f"""
        case "$*" in
          *"/actions/artifacts?per_page=100") cat "{tmp_path}/listing.json" ;;
          *"/artifacts/1/zip") printf 'PK-not-a-real-zip' ;;
          *"/artifacts/2/zip")
            echo "gh: HTTP 403: Resource not accessible by integration" >&2
            exit 1 ;;
          *) echo "unexpected gh call: $*" >&2; exit 1 ;;
        esac
    """)
    # The real unzip would extract the artifact; the stub just materialises what
    # a successful extraction leaves behind — the dataset file in <downloads>/<id>/.
    write_stub(bin_dir, "unzip", f"""
        dest=""
        while [ $# -gt 0 ]; do
          if [ "$1" = "-d" ]; then dest="$2"; shift; fi
          shift
        done
        mkdir -p "$dest"
        cp "{tmp_path}/dataset.json" "$dest/smoke_timings.json"
    """)
    return {
        "PATH": f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "HEART_STATE_DIR": str(tmp_path / "state"),
        "NO_COLOR": "1",
        "PYTHONPATH": str(ROOT),
    }


def _ingest(env):
    return subprocess.run(
        ["bash", "-c",
         'source "$1" >/dev/null 2>&1; ingest_one_repo "$2" "$3"',
         "ingest_one_repo", str(SCRIPT), OWNER_NAME, "workspaces"],
        capture_output=True, text=True, env={**os.environ, **env},
    )


def test_one_leg_403_still_writes_the_other_leg_and_records_the_error(env):
    proc = _ingest(env)
    assert proc.returncode == 0, proc.stderr
    side = json.loads(
        (Path(env["HEART_STATE_DIR"]) / "per-repo" / f"{REPO}.smoke_timings.json").read_text()
    )
    assert side["name"] == REPO and side["error"] == ""
    legs = {leg["python"]: leg for leg in side["legs"]}
    assert set(legs) == {"3.11", "3.12"}
    # The leg that downloaded is intact...
    assert legs["3.11"]["entries"][0]["entry"] == "imaging/x.py"
    assert legs["3.11"]["error"] == "" and legs["3.11"]["total_s"] == 12.0
    # ...and the 403 leg keeps its provenance with an honest error.
    assert "403" in legs["3.12"]["error"] and legs["3.12"]["entries"] == []
    assert legs["3.12"]["run_url"].endswith("/OwnerX/RepoA/actions/runs/7")


def test_a_failed_listing_fetch_is_recorded_not_silently_quiet(env, tmp_path):
    write_stub(tmp_path / "bin", "gh", """
        echo "gh: HTTP 502" >&2
        exit 1
    """)
    assert _ingest(env).returncode == 0
    side = json.loads(
        (Path(env["HEART_STATE_DIR"]) / "per-repo" / f"{REPO}.smoke_timings.json").read_text()
    )
    assert "502" in side["error"] and side["legs"] == []
