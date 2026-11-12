"""tests/test_ci_backfill.py — declared CI absent from the 30-run page is
fetched directly (PyAutoHeart#340), with a fake gh (no network).
"""

from __future__ import annotations

import io
import json

from heart.checks import ci_status

HEAD = "f" * 40


def run(name, conclusion, sha=HEAD, created="2026-11-12T10:00:00Z"):
    return {"name": name, "conclusion": conclusion, "status": "completed",
            "head_sha": sha, "created_at": created, "html_url": f"u/{name}",
            "head_branch": "main", "event": "push"}


def sidecar(*runs):
    return ci_status.build_sidecar("OrganRepo", "organism",
                                   {"workflow_runs": list(runs)}, HEAD, "2026-11-12T12:00:00+00:00")


class FakeGh:
    def __init__(self, ids=None, latest=None, fail=()):
        self.ids = ids or {}
        self.latest = latest or {}
        self.fail = set(fail)
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path.endswith("/actions/workflows?per_page=100"):
            return None if "list" in self.fail else {
                "workflows": [{"name": n, "id": i} for n, i in self.ids.items()]}
        wid = int(path.split("/actions/workflows/")[1].split("/")[0])
        name = next(n for n, i in self.ids.items() if i == wid)
        if name in self.fail:
            return None
        return {"workflow_runs": [self.latest[name]]}


def test_a_declared_workflow_off_the_page_is_fetched_by_id_and_cached(tmp_path):
    sc = sidecar(run("Organ Board", "success"))  # the declared gate is not on the page
    gh = FakeGh(ids={"Organ Gate": 7, "Organ Board": 8},
                latest={"Organ Gate": run("Organ Gate", "failure", created="2026-11-01T00:00:00Z")})
    cache = tmp_path / "ids" / "OrganRepo.json"
    assert ci_status.backfill_declared(sc, "org", ["Organ Gate"], gh, cache) == []
    entry = sc["workflows"]["Organ Gate"]
    assert entry["conclusion"] == "failure" and entry["backfilled"] is True
    assert entry["on_head"] is True
    assert gh.calls == ["repos/org/OrganRepo/actions/workflows?per_page=100",
                        "repos/org/OrganRepo/actions/workflows/7/runs"
                        "?branch=main&per_page=1&exclude_pull_requests=true"]
    assert json.loads(cache.read_text())["Organ Gate"] == 7
    # Next tick: the id comes from the cache — exactly one call per missing workflow.
    gh2 = FakeGh(ids={"Organ Gate": 7}, latest=gh.latest)
    sc2 = sidecar(run("Organ Board", "success"))
    ci_status.backfill_declared(sc2, "org", ["Organ Gate"], gh2, cache)
    assert len(gh2.calls) == 1 and "/workflows/7/runs" in gh2.calls[0]


def test_declared_workflows_on_the_page_cost_no_call(tmp_path):
    sc = sidecar(run("Organ Gate", "success"))
    gh = FakeGh()
    assert ci_status.backfill_declared(sc, "org", ["Organ Gate"], gh, tmp_path / "c.json") == []
    assert gh.calls == []
    assert "backfilled" not in sc["workflows"]["Organ Gate"]


def test_a_failed_fetch_leaves_it_missing_for_readiness_to_read_stale(tmp_path):
    sc = sidecar(run("Organ Board", "success"))
    gh = FakeGh(ids={"Organ Gate": 7}, fail={"Organ Gate"})
    assert ci_status.backfill_declared(sc, "org", ["Organ Gate"], gh, tmp_path / "c.json") == ["Organ Gate"]
    assert "Organ Gate" not in sc["workflows"]
    assert sc["backfill_missing"] == ["Organ Gate"]
    gh = FakeGh(fail={"list"})
    assert ci_status.backfill_declared(sc, "org", ["Organ Gate"], gh, tmp_path / "d.json") == ["Organ Gate"]


def test_backfill_never_moves_the_release_rollup(tmp_path):
    sc = sidecar(run("Organ Board", "success"))
    before = (sc["conclusion"], sc["status"], sc["workflow"])
    gh = FakeGh(ids={"Organ Gate": 7}, latest={"Organ Gate": run("Organ Gate", "failure")})
    ci_status.backfill_declared(sc, "org", ["Organ Gate"], gh, tmp_path / "c.json")
    assert (sc["conclusion"], sc["status"], sc["workflow"]) == before


def test_main_backfills_declared_ci_and_the_dev_gate_sees_it(tmp_path, monkeypatch):
    from heart import readiness, state
    monkeypatch.setattr(state, "HEART_STATE_DIR", tmp_path)
    monkeypatch.setattr(ci_status, "load_declared_ci", lambda name, *a: ["Organ Gate"])
    gh = FakeGh(ids={"Organ Gate": 7}, latest={"Organ Gate": run("Organ Gate", "success")})
    monkeypatch.setattr(ci_status, "gh_api", gh)
    monkeypatch.setattr(ci_status, "required_for", lambda group, *a: [])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"workflow_runs": [run("Organ Board", "success")]})))
    out = tmp_path / "OrganRepo.ci_status.json"
    ci_status.main(["--name", "OrganRepo", "--owner", "org", "--group", "organism",
                    "--head-sha", HEAD, "--ts", "2026-11-12T12:00:00+00:00", "--out", str(out)])
    body = json.loads(out.read_text())
    assert body["workflows"]["Organ Gate"]["backfilled"] is True
    import datetime
    v = readiness.dev_ship(
        {"ts": "2026-11-12T12:00:00+00:00", "repos": {"OrganRepo": {"ci_status": body}}},
        ["OrganRepo"], now=datetime.datetime(2026, 11, 12, 13, tzinfo=datetime.timezone.utc),
        libraries=[], required_workflows={}, dependencies={"X": []},
        groups={"OrganRepo": "organism"}, ci_workflows={"OrganRepo": ["Organ Gate"]})
    assert v["verdict"] == "green"
