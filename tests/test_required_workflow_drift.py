"""tests/test_required_workflow_drift.py — required workflows with no file.

The defect under test is silent by construction: ``ci_status.rollup()`` scores a
repo over its group's required workflows, and one with no runs is never scored
as a failure — it just never satisfies ``all_green``. These tests pin the two
things that make the finding useful: it is derived from the workflow *list*
(the only source that separates "no file" from "file exists, never ran"), and it
never reports a hollow green when the list cannot be read.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from heart.checks import required_workflow_drift as rwd


CONFIG = """\
repos:
  libraries:
    - name: LibraryOne
      owner: ExampleOrg
  workspaces:
    - name: alpha_workspace
      owner: ExampleOrg
    - name: beta_workspace
      owner: ExampleOrg
  organism:
    - name: SomeOrgan
      owner: ExampleOrg

required_workflows:
  libraries: ["Tests"]
  workspaces: ["Smoke Tests", "Navigator Check"]
"""


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text(CONFIG)
    return path


def test_polled_repos_reads_every_group(config):
    repos = rwd.polled_repos(config)
    assert {r["name"] for r in repos} == {
        "LibraryOne",
        "alpha_workspace",
        "beta_workspace",
        "SomeOrgan",
    }
    assert {r["group"] for r in repos} == {"libraries", "workspaces", "organism"}


def test_gating_repos_skips_advisory_groups(config):
    rows = rwd.gating_repos(config)
    # `organism` declares no required_workflows: advisory, nothing to be
    # missing, and nothing to spend an API call on.
    assert {r["name"] for r in rows} == {
        "LibraryOne",
        "alpha_workspace",
        "beta_workspace",
    }
    by_name = {r["name"]: r for r in rows}
    assert by_name["LibraryOne"]["required"] == ["Tests"]
    assert by_name["beta_workspace"]["required"] == ["Smoke Tests", "Navigator Check"]


def _fake_fetch(table):
    """Build a fetch_workflow_names stub from {owner/name: names | error str}."""

    def fetch(owner_name):
        entry = table[owner_name]
        if isinstance(entry, str):
            return None, entry
        return list(entry), ""

    return fetch


def test_missing_required_workflow_is_named(monkeypatch):
    monkeypatch.setattr(
        rwd,
        "fetch_workflow_names",
        _fake_fetch({"ExampleOrg/beta_workspace": ["Smoke Tests"]}),
    )
    row = rwd.check_one(
        {
            "owner": "ExampleOrg",
            "name": "beta_workspace",
            "group": "workspaces",
            "required": ["Smoke Tests", "Navigator Check"],
        }
    )
    assert row["missing"] == ["Navigator Check"]
    assert row["present"] == ["Smoke Tests"]
    assert row["error"] == ""


def test_workflow_present_but_never_run_is_not_missing(monkeypatch):
    """The distinction the runs payload cannot make.

    A file that exists but has never run on `main` leaves the roll-up genuinely
    pending — that is a run in flight, not a gate that was never wired up, and
    this check must not claim otherwise.
    """
    monkeypatch.setattr(
        rwd,
        "fetch_workflow_names",
        _fake_fetch(
            {"ExampleOrg/beta_workspace": ["Smoke Tests", "Navigator Check"]}
        ),
    )
    row = rwd.check_one(
        {
            "owner": "ExampleOrg",
            "name": "beta_workspace",
            "group": "workspaces",
            "required": ["Smoke Tests", "Navigator Check"],
        }
    )
    assert row["missing"] == []


def test_matching_is_on_name_not_filename(monkeypatch):
    """`ci_status` matches runs on the workflow's `name`, so this must too.

    A repo whose file is called `navigator_check.yml` but whose `name:` field
    says something else is exactly the case a filename-based check would pass
    and the roll-up would still starve on.
    """
    monkeypatch.setattr(
        rwd,
        "fetch_workflow_names",
        _fake_fetch({"ExampleOrg/x": ["navigator_check.yml", "Smoke Tests"]}),
    )
    row = rwd.check_one(
        {
            "owner": "ExampleOrg",
            "name": "x",
            "group": "workspaces",
            "required": ["Smoke Tests", "Navigator Check"],
        }
    )
    assert row["missing"] == ["Navigator Check"]


def test_fetch_error_is_an_unknown_never_an_implied_pass(monkeypatch):
    monkeypatch.setattr(
        rwd, "fetch_workflow_names", _fake_fetch({"ExampleOrg/x": "HTTP 403"})
    )
    row = rwd.check_one(
        {
            "owner": "ExampleOrg",
            "name": "x",
            "group": "workspaces",
            "required": ["Smoke Tests", "Navigator Check"],
        }
    )
    assert row["error"] == "HTTP 403"
    assert row["missing"] == []      # not "nothing missing" — unknown
    assert row["present"] == []


def test_timeout_is_reported_as_an_error(monkeypatch):
    def boom(owner_name):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=rwd.FETCH_TIMEOUT_S)

    monkeypatch.setattr(rwd, "fetch_workflow_names", boom)
    row = rwd.check_one(
        {"owner": "o", "name": "n", "group": "workspaces", "required": ["Smoke Tests"]}
    )
    assert "timed out" in row["error"]
    assert row["missing"] == []


def test_run_without_gh_is_unavailable_not_green(tmp_path, monkeypatch, config):
    monkeypatch.setattr(rwd.shutil, "which", lambda _: None)
    monkeypatch.setattr(rwd, "HEART_STATE_DIR", tmp_path)
    result = rwd.run(config)
    assert result["available"] is False
    assert "gh" in result["reason"]
    on_disk = json.loads((tmp_path / "required_workflow_drift.json").read_text())
    assert on_disk == result


def test_run_writes_the_sidecar_and_counts(tmp_path, monkeypatch, config):
    monkeypatch.setattr(rwd.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(rwd, "HEART_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        rwd,
        "fetch_workflow_names",
        _fake_fetch(
            {
                "ExampleOrg/LibraryOne": ["Tests"],
                "ExampleOrg/alpha_workspace": ["Smoke Tests", "Navigator Check"],
                "ExampleOrg/beta_workspace": ["Smoke Tests"],
            }
        ),
    )
    result = rwd.run(config)
    assert result["available"] is True
    assert result["checked"] == 3
    assert result["missing_count"] == 1
    assert result["error_count"] == 0
    missing = {r["name"]: r["missing"] for r in result["repos"] if r["missing"]}
    assert missing == {"beta_workspace": ["Navigator Check"]}
    on_disk = json.loads((tmp_path / "required_workflow_drift.json").read_text())
    assert on_disk == result


def test_fetch_workflow_names_reads_the_workflow_list(monkeypatch):
    """The one call this check adds, and the field it reads from it."""
    seen = {}

    class Proc:
        returncode = 0
        stdout = "Smoke Tests\nNavigator Check\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(rwd.subprocess, "run", fake_run)
    names, error = rwd.fetch_workflow_names("ExampleOrg/beta_workspace")
    assert names == ["Smoke Tests", "Navigator Check"]
    assert error == ""
    assert "repos/ExampleOrg/beta_workspace/actions/workflows" in seen["cmd"][2]
    assert seen["cmd"][-1] == ".workflows[].name"


def test_fetch_workflow_names_collapses_gh_stderr(monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "gh: Not Found (HTTP 404)\nsomething\n  else\n"

    monkeypatch.setattr(rwd.subprocess, "run", lambda cmd, **kw: Proc())
    names, error = rwd.fetch_workflow_names("ExampleOrg/nope")
    assert names is None
    assert "\n" not in error
    assert error.startswith("gh: Not Found")
