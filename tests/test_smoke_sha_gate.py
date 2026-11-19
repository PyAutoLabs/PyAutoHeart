"""tests/test_smoke_sha_gate.py — the nightly workspace-smoke gate (PyAutoHeart#354).

.github/scripts/smoke_sha_gate.py decides whether the nightly sweep runs and
records the SHAs every run tested. These tests cover the decision, the record
Heart's test_run leg reads, and the wiring in workspace-smoke.yml and
workspace-validation.yml. No network: ls-remote is injected.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

from heart.checks import test_run

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
spec = importlib.util.spec_from_file_location(
    "smoke_sha_gate", ROOT / ".github/scripts/smoke_sha_gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

NOW = datetime.datetime(2026, 11, 20, 3, 0, 5, tzinfo=datetime.timezone.utc)


def _shas(char="a"):
    return {repo: char * 40 for repo in gate.COVERED}


def _previous(shas, full_id="100", full_ts="2026-11-18T03:00:00Z", swept=True):
    return {"commit_shas": shas, "swept": swept,
            "full_sweep_run_id": full_id, "full_sweep_created_at": full_ts}


# --- decision ---------------------------------------------------------------

def test_nothing_moved_skips_and_names_the_full_sweep():
    sweep, reason, record = gate.decide(_shas(), {}, _previous(_shas()), "100", NOW)
    assert sweep is False
    assert reason == "no main moved since run 100"
    assert record["swept"] is False
    assert record["full_sweep_run_id"] == "100"
    assert record["full_sweep_created_at"] == "2026-11-18T03:00:00Z"
    assert record["commit_shas"] == _shas()


def test_a_skip_after_a_skip_still_attests_the_original_full_sweep():
    previous = _previous(_shas(), full_id="100", swept=False)
    sweep, reason, record = gate.decide(_shas(), {}, previous, "101", NOW)
    assert sweep is False and reason == "no main moved since run 100"
    assert record["full_sweep_run_id"] == "100" and record["previous_run_id"] == "101"


def test_one_moved_main_runs_the_sweep_and_names_it():
    now = _shas()
    now["PyAutoGalaxy"] = "b" * 40
    sweep, reason, record = gate.decide(now, {}, _previous(_shas()), "100", NOW)
    assert sweep is True and "PyAutoGalaxy" in reason
    assert record["full_sweep_run_id"] == record["run_id"]


def test_a_full_sweep_runs_at_least_weekly_even_when_nothing_moved():
    old = _previous(_shas(), full_ts="2026-11-13T03:00:00Z")  # 7 days before NOW
    sweep, reason, _ = gate.decide(_shas(), {}, old, "100", NOW)
    assert sweep is True and "7.0 days old" in reason


@pytest.mark.parametrize("previous", [None, {}, {"commit_shas": "junk"}])
def test_no_readable_record_runs_the_sweep(previous):
    sweep, reason, _ = gate.decide(_shas(), {}, previous, "100", NOW)
    assert sweep is True and "no SHA record" in reason


def test_a_pinned_dispatch_never_skips():
    pins = {"PyAutoGalaxy": "a" * 40}
    sweep, reason, record = gate.decide(_shas(), pins, _previous(_shas()), "100", NOW)
    assert sweep is True and reason.startswith("pinned by dispatch")
    assert record["pinned"] == pins


def test_the_summary_carries_the_headline_and_every_sha():
    _, _, record = gate.decide(_shas(), {}, _previous(_shas()), "100", NOW)
    text = gate.summary_markdown(record)
    assert "no main moved since run 100" in text
    for repo in gate.COVERED:
        assert f"| {repo} | `{'a' * 40}` | main |" in text


# --- resolving pins ---------------------------------------------------------

def test_resolve_uses_main_unless_pinned_and_resolves_refs():
    calls = []

    def lookup(repo, ref):
        calls.append((repo, ref))
        return ("c" if ref == "feature/x" else "d") * 40

    shas = gate.resolve({"PyAutoFit": "E" * 40, "PyAutoLens": "feature/x"}, lookup)
    assert shas["PyAutoFit"] == "e" * 40          # full SHA: taken as given
    assert shas["PyAutoLens"] == "c" * 40         # branch: resolved
    assert shas["HowToLens"] == "d" * 40          # unpinned: main
    assert ("PyAutoFit", "E" * 40) not in calls
    assert ("HowToLens", "refs/heads/main") in calls


def test_resolve_rejects_an_unresolvable_ref():
    with pytest.raises(ValueError, match="cannot resolve"):
        gate.resolve({"PyAutoFit": "abc123"}, lambda repo, ref: "")


def test_pins_must_name_covered_repos():
    with pytest.raises(ValueError, match="does not cover"):
        gate.parse_pins('{"PyAutoGalxy": "abc"}')
    assert gate.parse_pins("") == {}
    assert gate.parse_pins('{"PyAutoGalaxy": " %s "}' % ("a" * 40)) == {"PyAutoGalaxy": "a" * 40}


def test_main_writes_record_outputs_and_summary(tmp_path, monkeypatch):
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps(_previous(_shas())))
    out, summ = tmp_path / "out", tmp_path / "summary"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gate, "ls_remote", lambda repo, ref: "a" * 40)
    monkeypatch.setattr(gate, "resolve", lambda pins, lookup=None: _shas())
    for k, v in {"PINS": "", "PREVIOUS_RECORD": str(prev), "PREVIOUS_RUN_ID": "100",
                 "RUN_ID": "200", "GITHUB_OUTPUT": str(out),
                 "GITHUB_STEP_SUMMARY": str(summ)}.items():
        monkeypatch.setenv(k, v)
    assert gate.main() == 0
    record = json.loads((tmp_path / "shas.json").read_text())
    assert record["run_id"] == "200" and record["swept"] is False
    lines = out.read_text().splitlines()
    assert "sweep=false" in lines
    assert json.loads(lines[1].split("=", 1)[1]) == _shas()
    assert "no main moved since run 100" in summ.read_text()


# --- wiring -----------------------------------------------------------------

def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def test_covered_repos_match_what_the_body_checks_out():
    body = (WORKFLOWS / "workspace-validation.yml").read_text()
    ws_map = re.search(r"declare -A WS=\((.*?)\)\n", body, re.S).group(1)
    assert set(re.findall(r"=(\w+)", ws_map)) == set(gate.WORKSPACES)
    loops = re.findall(r"for lib in ([\w ]+); do", body)
    assert loops and all(tuple(l.split()) == gate.LIBRARIES for l in loops)


def test_smoke_entry_gates_the_sweep_and_passes_the_shas():
    data = _load("workspace-smoke.yml")
    triggers = data.get("on", data.get(True))
    assert triggers["schedule"] == [{"cron": "0 3 * * *"}]
    assert "commit_shas" in triggers["workflow_dispatch"]["inputs"]
    jobs = data["jobs"]
    smoke = jobs["smoke"]
    assert smoke["needs"] == "gate"
    assert smoke["if"] == "needs.gate.outputs.sweep == 'true'"
    assert smoke["with"]["commit_shas"] == "${{ needs.gate.outputs.commit_shas }}"
    upload = next(s for s in jobs["gate"]["steps"]
                  if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["with"]["name"] == test_run.SHA_ARTIFACT
    assert upload["with"]["path"] == "shas.json"
    assert data["permissions"]["actions"] == "read"


def test_body_pins_every_smoke_checkout_to_the_resolved_refs():
    body = _load("workspace-validation.yml")
    jobs = body["jobs"]
    assert jobs["find_scripts"]["outputs"]["refs"] == "${{ steps.mode.outputs.refs }}"
    for job in ("generate_notebooks", "run_scripts", "run_notebooks"):
        refs = [s["with"]["ref"] for s in jobs[job]["steps"]
                if s.get("name") == "Checkout workspace"]
        assert refs and all("needs.find_scripts.outputs.refs" in r for r in refs), job
    text = (WORKFLOWS / "workspace-validation.yml").read_text()
    assert 'git clone --depth 1 "https://github.com/PyAutoLabs/$lib"' not in text


def test_resolve_mode_keeps_release_checkouts_on_main(tmp_path):
    import os
    import subprocess
    step = next(s for s in _load("workspace-validation.yml")["jobs"]["find_scripts"]["steps"]
                if s.get("id") == "mode")
    for mode, expected in [("smoke", {"PyAutoFit": "a" * 40}), ("release", {})]:
        out = tmp_path / f"out-{mode}"
        subprocess.run(["bash", "-ec", step["run"]], check=True, env={
            **os.environ, "MODE_INPUT": mode, "GITHUB_OUTPUT": str(out),
            "COMMIT_SHAS": json.dumps({"PyAutoFit": "a" * 40}, indent=2)})
        lines = dict(l.split("=", 1) for l in out.read_text().splitlines())
        assert lines["mode"] == mode and json.loads(lines["refs"]) == expected


def test_library_clone_step_checks_out_the_pinned_sha(tmp_path):
    """Run the body's library-clone shell verbatim against local repos (git's
    url.insteadOf redirects github.com): the pinned library lands on its pin,
    the rest on main."""
    import os
    import subprocess

    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    origin = tmp_path / "origin"
    first = {}
    for lib in gate.LIBRARIES:
        repo = origin / lib
        repo.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=repo)
        for n in (1, 2):
            (repo / "f").write_text(str(n))
            git("add", "f", cwd=repo)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", str(n), cwd=repo)
            if n == 1:
                first[lib] = git("rev-parse", "HEAD", cwd=repo)
    step = next(s for s in _load("workspace-validation.yml")["jobs"]["run_scripts"]["steps"]
                if str(s.get("name", "")).startswith("Checkout libraries"))
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, "REFS": json.dumps({"PyAutoGalaxy": first["PyAutoGalaxy"]}),
           "GIT_CONFIG_COUNT": "2",
           "GIT_CONFIG_KEY_0": f"url.file://{origin}/.insteadOf",
           "GIT_CONFIG_VALUE_0": "https://github.com/PyAutoLabs/",
           "GIT_CONFIG_KEY_1": "uploadpack.allowAnySHA1InWant",
           "GIT_CONFIG_VALUE_1": "true"}
    subprocess.run(["bash", "-ec", step["run"]], cwd=work, env=env, check=True,
                   capture_output=True, text=True)
    for lib in gate.LIBRARIES:
        assert (work / "libs" / lib / "f").read_text() == ("1" if lib == "PyAutoGalaxy" else "2")
