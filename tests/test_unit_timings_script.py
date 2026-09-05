"""tests/test_unit_timings_script.py — the unit shell leg's failure paths.

The sibling of `test_smoke_timings_script.py`, and it pins the same
load-bearing claim on the other check: ONE artifact's 403 must not cost the repo
its other python leg — the failure is written to `<downloads>/<id>.error` and
the loop continues. Driven the same way: source the file, call the one function,
and put stub executables first on `PATH` so nothing here touches the network, a
real `gh`, or a real zip.

Fake names throughout (the tenant firewall).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_smoke_timings_script import write_stub

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "heart" / "checks" / "unit_timings.sh"

OWNER_NAME = "OwnerX/RepoA"
REPO = "RepoA"

LISTING = {
    "artifacts": [
        {"id": 1, "name": "unit-timings-3.12", "expired": False,
         "created_at": "2026-09-01T10:00:00Z",
         "workflow_run": {"id": 7, "head_branch": "feat/x", "head_sha": "abc123"}},
        {"id": 2, "name": "unit-timings-3.13", "expired": False,
         "created_at": "2026-09-01T10:00:00Z",
         "workflow_run": {"id": 7, "head_branch": "feat/x", "head_sha": "abc123"}},
    ]
}

JUNIT = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<testsuites><testsuite name="pytest" tests="2" failures="0" errors="0" '
    'skipped="0" time="41.0">'
    '<testcase classname="tests.foo.test_bar" name="test_x" time="12.5"/>'
    "</testsuite></testsuites>"
)
IMPORT_JSON = {"schema": "import_time/1", "package": "pkg_a", "python": "3.12",
               "seconds": 3.6, "returncode": 0, "ts": "2026-09-01T09:55:00Z"}


@pytest.fixture
def env(tmp_path):
    """A stub `gh` (artifact 2 is a 403), a stub `unzip`, and a state dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "state" / "per-repo").mkdir(parents=True)
    (tmp_path / "listing.json").write_text(json.dumps(LISTING))
    (tmp_path / "junit.xml").write_text(JUNIT)
    (tmp_path / "import_time.json").write_text(json.dumps(IMPORT_JSON))

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
    # The stub materialises what a successful extraction leaves behind: both
    # halves of the dataset in <downloads>/<id>/.
    write_stub(bin_dir, "unzip", f"""
        dest=""
        while [ $# -gt 0 ]; do
          if [ "$1" = "-d" ]; then dest="$2"; shift; fi
          shift
        done
        mkdir -p "$dest"
        cp "{tmp_path}/junit.xml" "$dest/junit.xml"
        cp "{tmp_path}/import_time.json" "$dest/import_time.json"
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
         "ingest_one_repo", str(SCRIPT), OWNER_NAME, "libraries"],
        capture_output=True, text=True, env={**os.environ, **env},
    )


def _sidecar(env):
    path = (Path(env["HEART_STATE_DIR"]) / "per-repo" / f"{REPO}.unit_timings.json")
    return json.loads(path.read_text())


def test_one_leg_403_still_writes_the_other_leg_and_records_the_error(env):
    proc = _ingest(env)
    assert proc.returncode == 0, proc.stderr
    side = _sidecar(env)
    assert side["name"] == REPO and side["error"] == ""
    legs = {leg["python"]: leg for leg in side["legs"]}
    assert set(legs) == {"3.12", "3.13"}
    # The leg that downloaded is intact — both halves of the dataset...
    assert legs["3.12"]["slowest"][0]["seconds"] == 12.5
    assert legs["3.12"]["suite"]["tests"] == 2
    assert legs["3.12"]["import"]["package"] == "pkg_a"
    assert legs["3.12"]["error"] == ""
    # ...and the 403 leg keeps its provenance with an honest error.
    assert "403" in legs["3.13"]["error"] and legs["3.13"]["slowest"] == []
    assert legs["3.13"]["run_url"].endswith("/OwnerX/RepoA/actions/runs/7")


def test_a_failed_listing_fetch_is_recorded_not_silently_quiet(env, tmp_path):
    write_stub(tmp_path / "bin", "gh", """
        echo "gh: HTTP 502" >&2
        exit 1
    """)
    assert _ingest(env).returncode == 0
    side = _sidecar(env)
    assert "502" in side["error"] and side["legs"] == []
