"""tests/test_ci_timing_script.py — the shell leg's closed-PR plumbing.

`time_one_repo_ci` makes a second, best-effort `gh api` call for the repo's
recently closed PRs and hands it to the Python step as `--closed-prs`. Driven
like `test_ci_status_script.py`: source the script, call the one function, and
put a stub `gh` first on `PATH` so nothing here touches the network.

Generic repo names only (the tenant firewall): `gh` is stubbed, so the name is
never resolved.
"""

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "heart" / "checks" / "ci_timing.sh"

RUNS = {"workflow_runs": [{
    "name": "Tests", "conclusion": "cancelled", "status": "completed",
    "event": "pull_request", "head_branch": "feat/regroup",
    "created_at": "2026-09-19T10:59:28Z", "run_started_at": "2026-09-19T10:59:30Z",
    "updated_at": "2026-09-19T11:06:13Z", "html_url": "https://github.invalid/run/a",
}]}
CLOSED = [{"head_ref": "feat/regroup", "closed_at": "2026-09-19T11:05:51Z"}]


def write_gh(bin_dir: Path, pulls_body: str) -> None:
    """A `gh` that answers the runs query and runs `pulls_body` for the PR one."""
    path = bin_dir / "gh"
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(f"""\
        case "$2" in
          */actions/runs*) cat <<'JSON'
        {json.dumps(RUNS)}
        JSON
          ;;
          */pulls*) {pulls_body} ;;
          *) exit 1 ;;
        esac
        """))
    path.chmod(0o755)


def run_one_repo(tmp_path: Path, bin_dir: Path) -> dict:
    state = tmp_path / "state"
    (state / "per-repo").mkdir(parents=True)
    result = subprocess.run(
        ["bash", "-c",
         'source "$1" >/dev/null 2>&1; time_one_repo_ci "ExampleOrg/ExampleRepo" libraries',
         "ci_timing", str(SCRIPT)],
        capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
             "HEART_STATE_DIR": str(state), "NO_COLOR": "1"},
    )
    assert result.returncode == 0, result.stderr
    return json.loads((state / "per-repo" / "ExampleRepo.ci_timing.json").read_text())


@pytest.fixture
def bin_dir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


def test_closed_prs_reach_the_python_step(tmp_path, bin_dir):
    write_gh(bin_dir, f"echo '{json.dumps(CLOSED)}'")
    side = run_one_repo(tmp_path, bin_dir)
    assert side["error"] == ""
    assert side["events"] == []
    assert side["workflows"]["Tests"]["abandoned"] == 1


def test_a_failed_closed_pr_fetch_degrades_to_todays_behaviour(tmp_path, bin_dir):
    write_gh(bin_dir, "echo 'HTTP 403' >&2; exit 1")
    side = run_one_repo(tmp_path, bin_dir)
    assert side["error"] == ""                      # not a fetch failure
    assert [e["kind"] for e in side["events"]] == ["suspect_cancelled"]
    assert side["workflows"]["Tests"]["abandoned"] == 0


def test_an_empty_closed_pr_fetch_degrades_to_todays_behaviour(tmp_path, bin_dir):
    write_gh(bin_dir, "true")
    side = run_one_repo(tmp_path, bin_dir)
    assert side["error"] == ""
    assert len(side["events"]) == 1
