"""tests/test_state.py — atomic JSON write + aggregation behaviour."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def temp_state_dir(tmp_path, monkeypatch):
    """Redirect HEART_STATE_DIR to a tmp path; reload heart.state to use it."""
    monkeypatch.setenv("HEART_STATE_DIR", str(tmp_path))
    # Force a fresh import so the module-level constants pick up the env.
    import importlib

    import heart.state as state_mod
    importlib.reload(state_mod)
    config = tmp_path / "repos.yaml"
    config.write_text("repos:\n  test:\n    - name: PyAutoFit\n    - name: PyAutoArray\n    - name: Foo\n")
    monkeypatch.setattr(state_mod, "CONFIG_PATH", config, raising=False)
    return tmp_path, state_mod


def test_atomic_write_is_atomic(temp_state_dir):
    tmp_path, state = temp_state_dir
    target = tmp_path / "out.json"
    state.atomic_write_json(target, {"hello": "world"})
    assert target.is_file()
    assert json.loads(target.read_text()) == {"hello": "world"}
    # No leftover tempfiles
    leftovers = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_aggregate_collapses_per_repo_sidecars(temp_state_dir):
    tmp_path, state = temp_state_dir
    per_repo = tmp_path / "per-repo"
    per_repo.mkdir(parents=True)

    (per_repo / "PyAutoFit.repo_state.json").write_text(json.dumps({"name": "PyAutoFit", "branch": "main"}))
    (per_repo / "PyAutoFit.ci_status.json").write_text(json.dumps({"name": "PyAutoFit", "conclusion": "success"}))
    (per_repo / "PyAutoArray.repo_state.json").write_text(json.dumps({"name": "PyAutoArray", "branch": "main"}))

    snap = state.aggregate()
    assert set(snap["repos"].keys()) == {"PyAutoFit", "PyAutoArray"}
    assert snap["repos"]["PyAutoFit"]["repo_state"]["branch"] == "main"
    assert snap["repos"]["PyAutoFit"]["ci_status"]["conclusion"] == "success"
    assert snap["repos"]["PyAutoArray"]["repo_state"]["branch"] == "main"

    # state.json was written.
    assert (tmp_path / "state.json").is_file()


def test_load_returns_none_when_no_cache(temp_state_dir):
    _, state = temp_state_dir
    assert state.load() is None


def test_load_roundtrips_after_aggregate(temp_state_dir):
    tmp_path, state = temp_state_dir
    per_repo = tmp_path / "per-repo"
    per_repo.mkdir(parents=True)
    (per_repo / "Foo.bar.json").write_text(json.dumps({"name": "Foo"}))
    state.aggregate()
    snap = state.load()
    assert snap is not None
    assert "Foo" in snap["repos"]


def test_age_seconds_returns_none_for_missing_cache(temp_state_dir):
    _, state = temp_state_dir
    assert state.age_seconds() is None


def test_aggregate_filters_retired_repos_without_deleting_evidence(temp_state_dir):
    tmp_path, state = temp_state_dir
    state.CONFIG_PATH.write_text("repos:\n  libraries:\n    - name: PyAutoFit\n    - name: MissingRepo\n")
    retired = tmp_path / "per-repo/PyAutoConf.ci_status.json"
    current = tmp_path / "per-repo/PyAutoFit.ci_status.json"
    state.atomic_write_json(retired, {"conclusion": "failure"})
    state.atomic_write_json(current, {"conclusion": "failure"})
    state.atomic_write_json(tmp_path / "manifest_drift.json", {"problem_count": 7})
    contents = retired.read_bytes()
    snap = state.aggregate()
    assert set(snap["repos"]) == {"PyAutoFit"}
    assert snap["repos"]["PyAutoFit"]["ci_status"]["conclusion"] == "failure"
    assert snap["manifest_drift"] == {"problem_count": 7}
    assert retired.read_bytes() == contents
    assert not (tmp_path / "per-repo/MissingRepo.ci_status.json").exists()


@pytest.mark.parametrize("config", [None, "[invalid", "", "[]", "{}",
    "repos: null", "repos: []", "repos: {libraries: null}",
    "repos: {libraries: [null]}", "repos: {libraries: [{}]}",
    "repos: {libraries: [{name: 123}]}", "repos: {libraries: [{name: ''}]}",
    "repos: {libraries: [{name: 'bad name'}]}",
    "repos: {libraries: [{name: '../bad'}]}",
    "repos: {libraries: [{name: PyAutoFit}, {name: null}]}" ])
def test_bad_or_missing_config_retains_observations(temp_state_dir, config):
    tmp_path, state = temp_state_dir
    if config is None:
        state.CONFIG_PATH.unlink()
    else:
        state.CONFIG_PATH.write_text(config)
    state.atomic_write_json(tmp_path / "per-repo/PyAutoConf.ci_status.json",
                            {"conclusion": "failure"})
    assert "PyAutoConf" in state.aggregate()["repos"]


def test_explicit_empty_config_roster_excludes_cached_repos(temp_state_dir):
    tmp_path, state = temp_state_dir
    state.CONFIG_PATH.write_text("repos: {libraries: []}")
    state.atomic_write_json(tmp_path / "per-repo/PyAutoConf.ci_status.json", {})
    assert state.aggregate()["repos"] == {}
