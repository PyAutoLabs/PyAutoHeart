"""tests/test_repo_config.py — config/repos.yaml schema validity."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def config():
    here = Path(__file__).resolve().parents[1]
    return yaml.safe_load((here / "config" / "repos.yaml").read_text())


def test_repos_block_present(config):
    assert "repos" in config
    assert isinstance(config["repos"], dict)


def test_every_repo_has_owner_and_name(config):
    for group, entries in config["repos"].items():
        assert isinstance(entries, list), f"group {group} must be a list"
        for repo in entries:
            assert "name" in repo, f"missing name in group {group}: {repo}"
            assert "owner" in repo, f"missing owner in group {group}: {repo}"
            assert isinstance(repo["name"], str)
            assert isinstance(repo["owner"], str)


def test_no_duplicate_repo_names(config):
    seen = set()
    for entries in config["repos"].values():
        for repo in entries:
            assert repo["name"] not in seen, f"duplicate repo: {repo['name']}"
            seen.add(repo["name"])


def test_excluded_repos_block_present(config):
    assert "excluded" in config
    assert isinstance(config["excluded"], list)


def test_thresholds_have_expected_fields(config):
    thresholds = config["thresholds"]
    assert thresholds["script_timing"]["yellow_factor"] > 1.0
    assert thresholds["script_timing"]["red_factor"] > thresholds["script_timing"]["yellow_factor"]
    assert thresholds["script_timing"]["baseline_window"] >= 3


def test_25_repos_polled(config):
    """Sanity check the polled count — bumps need a deliberate update.
    (25 since the CTI resurrection added PyAutoCTI + autocti_workspace +
    autocti_workspace_test to the polled registry.)"""
    total = sum(len(v) for v in config["repos"].values())
    assert total == 25, f"expected 25 polled repos, got {total}"


def test_cti_polled_but_not_release_gating(config):
    """A resurrecting library is polled for health but excluded from the
    release-validation gate until it ships (release_gate: false)."""
    from heart import readiness

    assert "PyAutoCTI" in readiness.load_library_names()
    assert "PyAutoCTI" not in readiness.load_release_gate_names()


def _all_names(config):
    return {r["name"] for entries in config["repos"].values() for r in entries}


def test_registry_renames_applied(config):
    names = _all_names(config)
    # PyAutoPrompt → PyAutoMind, PyAutoPaper → PyAutoMemory.
    assert "PyAutoMind" in names and "PyAutoPrompt" not in names
    assert "PyAutoMemory" in names and "PyAutoPaper" not in names
    # Stale names must not linger in the excluded list either.
    assert "PyAutoPaper" not in config.get("excluded", [])


def test_organism_repos_polled(config):
    names = _all_names(config)
    for repo in ("PyAutoBrain", "PyAutoHeart", "PyAutoMemory"):
        assert repo in names, f"organism repo {repo} not polled"


def test_required_workflows_block(config):
    rw = config["required_workflows"]
    assert isinstance(rw, dict)
    # Every gated group must reference real repo groups and list workflow names.
    for group, workflows in rw.items():
        assert group in config["repos"], f"required_workflows group {group} not in repos"
        assert isinstance(workflows, list) and workflows
        assert all(isinstance(w, str) for w in workflows)
    # The smoke gate must exist for the user-facing workspaces; url is NOT a
    # required workflow (advisory only).
    assert "Smoke Tests" in rw["workspaces"]
    assert "Navigator Check" in rw["workspaces"]
    for workflows in rw.values():
        assert not any("url" in w.lower() for w in workflows)


def test_smoke_block_covers_every_cti_workspace(config):
    """Both CTI smoke surfaces must be runnable through the local runner.

    Until PyAutoHeart#172 the `smoke:` block had no autocti entry at all and
    `import_names` had no PyAutoCTI, so neither CTI suite could be exercised
    without pushing and waiting for CI.
    """
    smoke = config["smoke"]
    assert smoke["import_names"]["PyAutoCTI"] == "autocti"

    workspaces = smoke["workspaces"]
    for key, directory in (
        ("autocti", "autocti_workspace"),
        ("autocti_test", "autocti_workspace_test"),
    ):
        spec = workspaces[key]
        assert spec["directory"] == directory
        # Asserted through import_names rather than as repo-name literals: the
        # tenant firewall (PyAutoMind/scripts/repos_sync.py) treats a satellite
        # repo name in organ code as an instance fact, and this says the same
        # thing while additionally proving every chain repo resolves in the map.
        chain_packages = [smoke["import_names"][repo] for repo in spec["chain"]]
        assert chain_packages == ["autonerves", "autofit", "autoarray", "autocti"]
        # The CTI stack does NOT sit on the galaxy/lens libraries — the lens
        # stack does. This mirrors what both repos' CI callers declare.
        assert "autogalaxy" not in chain_packages
        assert "autolens" not in chain_packages
        # `import autocti` hard-requires arcticpy, which is not a pip
        # dependency; the flag mirrors the `arcticpy: true` input those same
        # CI callers pass to the reusable smoke-tests.yml.
        assert spec["arcticpy"] is True


def test_only_cti_workspaces_request_arcticpy(config):
    """Every other workspace must be untouched by the arcticpy leg."""
    for key, spec in config["smoke"]["workspaces"].items():
        if key.startswith("autocti"):
            continue
        assert "arcticpy" not in spec, f"{key} should not request arcticpy"


def test_every_smoke_chain_repo_has_an_import_name(config):
    """A chain repo missing from import_names is invisible to the preflight.

    The preflight proves each chain library imports from local source; it skips
    any repo absent from the map, so an omission silently weakens the check
    rather than failing it. That is how PyAutoCTI went unnoticed.
    """
    smoke = config["smoke"]
    known = set(smoke["import_names"])
    for key, spec in smoke["workspaces"].items():
        missing = [repo for repo in spec["chain"] if repo not in known]
        assert not missing, f"{key} chain has no import name for {missing}"
