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
