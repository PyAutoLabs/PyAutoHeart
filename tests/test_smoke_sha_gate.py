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
# Placeholder repos: the decision logic never needs a real name (and the organ
# tenant firewall keeps real ones out of Heart's Python).
COVERED = ("LibA", "LibB", "ws_a", "ws_b")


def _shas(char="a"):
    return {repo: char * 40 for repo in COVERED}


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
    now["LibB"] = "b" * 40
    sweep, reason, record = gate.decide(now, {}, _previous(_shas()), "100", NOW)
    assert sweep is True and reason.endswith(": LibB")
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
    pins = {"LibB": "a" * 40}
    sweep, reason, record = gate.decide(_shas(), pins, _previous(_shas()), "100", NOW)
    assert sweep is True and reason.startswith("pinned by dispatch")
    assert record["pinned"] == pins


def test_force_sweeps_even_when_nothing_moved():
    sweep, reason, record = gate.decide(_shas(), {}, _previous(_shas()), "100", NOW, force=True)
    assert sweep is True and reason == "forced by dispatch"
    assert record["full_sweep_run_id"] == record["run_id"]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", ""])
def test_a_red_latest_run_is_swept_again_not_masked_by_an_older_green(conclusion):
    """Run 100 passed; run 101 failed at the same SHAs (a flake or a PyPI
    move). Nothing moved since 100, but the night must sweep, not skip."""
    sweep, reason, _ = gate.decide(_shas(), {}, _previous(_shas()), "100", NOW,
                                   last_run=("101", conclusion))
    assert sweep is True
    assert reason == f"last run 101 concluded {conclusion or 'unknown'}"


def test_a_green_latest_run_still_allows_the_skip():
    sweep, reason, _ = gate.decide(_shas(), {}, _previous(_shas()), "100", NOW,
                                   last_run=("100", "success"))
    assert sweep is False and reason == "no main moved since run 100"


def test_the_summary_carries_the_headline_and_every_sha():
    _, _, record = gate.decide(_shas(), {}, _previous(_shas()), "100", NOW)
    text = gate.summary_markdown(record)
    assert "no main moved since run 100" in text
    for repo in COVERED:
        assert f"| {repo} | `{'a' * 40}` | main |" in text


# --- resolving pins ---------------------------------------------------------

def test_resolve_uses_main_unless_pinned_and_resolves_refs():
    calls = []

    def lookup(repo, ref):
        calls.append((repo, ref))
        return ("c" if ref == "feature/x" else "d") * 40

    shas = gate.resolve({"LibA": "E" * 40, "LibB": "feature/x"}, COVERED, lookup)
    assert shas["LibA"] == "e" * 40          # full SHA: taken as given
    assert shas["LibB"] == "c" * 40          # branch: resolved
    assert shas["ws_a"] == "d" * 40          # unpinned: main
    assert ("LibA", "E" * 40) not in calls
    assert ("ws_a", "refs/heads/main") in calls


def test_resolve_rejects_an_unresolvable_ref():
    with pytest.raises(ValueError, match="cannot resolve"):
        gate.resolve({"LibA": "abc123"}, COVERED, lambda repo, ref: "")


def test_pins_must_name_covered_repos():
    with pytest.raises(ValueError, match="does not cover"):
        gate.parse_pins('{"LibC": "abc"}', COVERED)
    assert gate.parse_pins("", COVERED) == {}
    assert gate.parse_pins('{"LibB": " %s "}' % ("a" * 40), COVERED) == {"LibB": "a" * 40}


def _recent_full_sweep_ts():
    """A full-sweep time one day before the REAL clock.

    `main()` reads `datetime.now()`, not `NOW`, so a fixed timestamp here ages
    past FULL_SWEEP_MAX_AGE_DAYS as the calendar moves on and flips these tests
    onto the forced-sweep path (PyAutoHeart#374).
    """
    then = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    return then.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_main_writes_record_outputs_and_summary(tmp_path, monkeypatch):
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps(_previous(_shas(), full_ts=_recent_full_sweep_ts())))
    out, summ = tmp_path / "out", tmp_path / "summary"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gate, "load_covered", lambda body=None: COVERED)
    monkeypatch.setattr(gate, "resolve", lambda pins, covered, lookup=None: _shas())
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


@pytest.mark.parametrize("env, reason", [
    ({"FORCE": "true"}, "forced by dispatch"),
    ({"FORCE": "false", "LAST_RUN_ID": "101", "LAST_RUN_CONCLUSION": "failure"},
     "last run 101 concluded failure"),
    ({"FORCE": "false", "LAST_RUN_ID": "", "LAST_RUN_CONCLUSION": ""},
     "no main moved since run 100"),
])
def test_main_reads_force_and_the_latest_run_from_the_environment(tmp_path, monkeypatch, env, reason):
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps(_previous(_shas(), full_ts=_recent_full_sweep_ts())))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gate, "load_covered", lambda body=None: COVERED)
    monkeypatch.setattr(gate, "resolve", lambda pins, covered, lookup=None: _shas())
    for k, v in {"PINS": "", "PREVIOUS_RECORD": str(prev), "PREVIOUS_RUN_ID": "100",
                 "RUN_ID": "200", "GITHUB_OUTPUT": str(tmp_path / "o"),
                 "GITHUB_STEP_SUMMARY": str(tmp_path / "s"), **env}.items():
        monkeypatch.setenv(k, v)
    assert gate.main() == 0
    assert json.loads((tmp_path / "shas.json").read_text())["reason"] == reason


# --- wiring -----------------------------------------------------------------

def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _body_covered():
    return gate.load_covered(WORKFLOWS / "workspace-validation.yml")


def test_covered_repos_are_read_from_the_body_and_agree_with_every_checkout():
    """The body is the one source of truth for the covered repos. Every
    workspace checkout in it must name a covered repo, and every library loop
    must list the same libraries (load_covered raises otherwise)."""
    covered = _body_covered()
    assert len(covered) == len(set(covered)) and covered
    body = _load("workspace-validation.yml")
    notebook_ws = {p["workspace"] for p in
                   body["jobs"]["generate_notebooks"]["strategy"]["matrix"]["project"]}
    assert notebook_ws <= set(covered)
    for job in ("run_scripts", "run_notebooks"):
        route = next(s["run"] for s in body["jobs"][job]["steps"] if s.get("id") == "ws")
        assert set(re.findall(r"repo=(\w+)", route)) <= set(covered), job
    loops = re.findall(r"for lib in ([\w ]+); do",
                       (WORKFLOWS / "workspace-validation.yml").read_text())
    assert loops and all(tuple(l.split()) == covered[:len(l.split())] for l in loops)


def test_load_covered_fails_loudly_on_a_reshaped_body(tmp_path):
    body = tmp_path / "body.yml"
    body.write_text("for lib in LibA LibB; do\n")
    with pytest.raises(ValueError, match="no workspace map"):
        gate.load_covered(body)
    body.write_text("for lib in LibA; do\nfor lib in LibB; do\ndeclare -A WS=(\n [a]=ws_a\n)\n")
    with pytest.raises(ValueError, match="one library list"):
        gate.load_covered(body)
    body.write_text("for lib in LibA LibB; do\ndeclare -A WS=(\n [a]=ws_a [b]=ws_b\n)\n")
    assert gate.load_covered(body) == COVERED


def test_smoke_entry_gates_the_sweep_and_passes_the_shas():
    data = _load("workspace-smoke.yml")
    triggers = data.get("on", data.get(True))
    assert triggers["schedule"] == [{"cron": "0 3 * * *"}]
    # The smoke channel only ever tests main: no pin input, only force.
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"force"}
    assert inputs["force"]["type"] == "boolean" and inputs["force"]["default"] is False
    decide = next(s for s in data["jobs"]["gate"]["steps"] if s.get("id") == "decide")
    assert "PINS" not in decide["env"]
    assert decide["env"]["FORCE"] == "${{ inputs.force && 'true' || 'false' }}"
    assert {"LAST_RUN_ID", "LAST_RUN_CONCLUSION"} <= set(decide["env"])
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


def test_pinned_entry_has_its_own_history_and_always_sweeps():
    data = _load("workspace-smoke-pinned.yml")
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["commit_shas"]["required"] is True
    jobs = data["jobs"]
    decide = next(s for s in jobs["gate"]["steps"] if s.get("id") == "decide")
    assert decide["env"]["PINS"] == "${{ inputs.commit_shas }}"
    smoke = jobs["smoke"]
    assert smoke["needs"] == "gate" and "if" not in smoke
    assert smoke["uses"].endswith("workspace-validation.yml")
    assert smoke["with"] == {"mode": "smoke",
                             "commit_shas": "${{ needs.gate.outputs.commit_shas }}"}
    upload = next(s for s in jobs["gate"]["steps"]
                  if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["with"]["name"] == test_run.SHA_ARTIFACT


def test_body_pins_every_smoke_checkout_to_the_resolved_refs():
    body = _load("workspace-validation.yml")
    jobs = body["jobs"]
    assert jobs["find_scripts"]["outputs"]["refs"] == "${{ steps.mode.outputs.refs }}"
    for job in ("generate_notebooks", "run_scripts", "run_notebooks"):
        refs = [s["with"]["ref"] for s in jobs[job]["steps"]
                if s.get("name") == "Checkout workspace"]
        assert refs and all("needs.find_scripts.outputs.refs" in r for r in refs), job
    text = (WORKFLOWS / "workspace-validation.yml").read_text()
    assert not re.search(r'git clone --depth 1 "https://github\.com/[^/]+/\$lib"', text)


def test_resolve_mode_keeps_release_checkouts_on_main(tmp_path):
    import os
    import subprocess
    step = next(s for s in _load("workspace-validation.yml")["jobs"]["find_scripts"]["steps"]
                if s.get("id") == "mode")
    for mode, expected in [("smoke", {"LibA": "a" * 40}), ("release", {})]:
        out = tmp_path / f"out-{mode}"
        subprocess.run(["bash", "-ec", step["run"]], check=True, env={
            **os.environ, "MODE_INPUT": mode, "GITHUB_OUTPUT": str(out),
            "COMMIT_SHAS": json.dumps({"LibA": "a" * 40}, indent=2)})
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
    step = next(s for s in _load("workspace-validation.yml")["jobs"]["run_scripts"]["steps"]
                if str(s.get("name", "")).startswith("Checkout libraries"))
    libraries = re.search(r"for lib in ([\w ]+); do", step["run"]).group(1).split()
    url_prefix = re.search(r'git clone --depth 1 "(https://github\.com/[^/"$]+/)\$1"',
                           step["run"]).group(1)
    pinned = libraries[0]
    for lib in libraries:
        repo = origin / lib
        repo.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=repo)
        for n in (1, 2):
            (repo / "f").write_text(str(n))
            git("add", "f", cwd=repo)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", str(n), cwd=repo)
            if n == 1:
                first[lib] = git("rev-parse", "HEAD", cwd=repo)
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, "REFS": json.dumps({pinned: first[pinned]}),
           "GIT_CONFIG_COUNT": "2",
           "GIT_CONFIG_KEY_0": f"url.file://{origin}/.insteadOf",
           "GIT_CONFIG_VALUE_0": url_prefix,
           "GIT_CONFIG_KEY_1": "uploadpack.allowAnySHA1InWant",
           "GIT_CONFIG_VALUE_1": "true"}
    subprocess.run(["bash", "-ec", step["run"]], cwd=work, env=env, check=True,
                   capture_output=True, text=True)
    for lib in libraries:
        assert (work / "libs" / lib / "f").read_text() == ("1" if lib == pinned else "2")
