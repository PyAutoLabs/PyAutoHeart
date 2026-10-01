"""Fresh cloud state must reconstruct evidence, never manufacture readiness."""

import json
import pytest
from heart import readiness, state, validate
from heart.checks import cloud_validation as cloud


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "HEART_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        validate, "VALIDATION_REPORT_FILE", tmp_path / "validation_report.json"
    )
    monkeypatch.setattr(
        validate, "VERIFY_INSTALL_FILE", tmp_path / "verify_install.json"
    )
    monkeypatch.setattr(validate, "VALIDATION_HISTORY_DIR", tmp_path / "history")
    integration = dict(
        id=20,
        status="completed",
        conclusion="success",
        created_at="2026-10-01T12:00:00Z",
        html_url="https://example.test/runs/20",
    )
    producer = dict(
        id=10,
        status="completed",
        conclusion="success",
        created_at="2026-10-01T10:00:00Z",
        updated_at="2026-10-01T11:00:00Z",
        run_attempt=1,
        head_sha="abcdef123",
    )
    artifact = dict(
        version="2026.10.1.1.dev101",
        mode="rehearsal",
        index="testpypi",
        run_id="10",
        run_attempt="1",
        build_sha="abcdef123",
    )
    stage = dict(
        stage="integrate",
        status="pass",
        profile="release",
        version=artifact["version"],
        run_url=integration["html_url"],
        summary=dict(passed=719, failed=0, timeout=0, skipped=82),
        commit_shas={n: "abcdef123" for n in readiness._GATE_SHA_LIBS},
        verify_install=dict(
            ready=True,
            ts="2026-10-01T12:30:00Z",
            version=artifact["version"],
            index="testpypi",
            checks=[dict(check="A", status="PASS")],
        ),
    )
    return dict(
        integration=integration,
        producer=producer,
        artifact=artifact,
        stage=stage,
        tmp=tmp_path,
    )


def run(e):
    def fetch(path):
        return {
            "workflow_runs": (
                [e["integration"]]
                if "release-integrate.yml" in path
                else [e["producer"]]
            )
        }

    def download(repo, record, name, filename, dest):
        return e["stage"] if name == "release-stage-report" else e["artifact"]

    return cloud.collect(
        {"release_evidence": {"rehearsal_repo": "example/build"}}, fetch, download
    )


def report(e):
    return json.loads((e["tmp"] / "validation_report.json").read_text())


def test_complete_evidence_does_not_rejuvenate(evidence):
    e = evidence
    assert run(e)["validation_outcome"] == "pass"
    first = report(e)
    assert first["totals"]["passed"] == 719
    assert first["ts"].startswith("2026-10-01T10:00:00")
    run(e)
    assert report(e) == first
    vi = json.loads((e["tmp"] / "verify_install.json").read_text())
    assert vi["ts"] == e["stage"]["verify_install"]["ts"]
    assert vi["index"] == "testpypi"


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "wrong"),
        ("run_id", "99"),
        ("run_attempt", "2"),
        ("build_sha", "wrong"),
        ("index", "pypi"),
        ("mode", "live"),
    ],
)
def test_mismatched_provenance_is_incomplete(evidence, field, value):
    evidence["artifact"][field] = value
    assert run(evidence)["validation_outcome"] == "incomplete"
    assert "rehearse" not in report(evidence)["stages"]


@pytest.mark.parametrize("status", ["in_progress", "queued"])
def test_pending_rehearsal_is_not_proof(evidence, status):
    evidence["producer"]["status"] = status
    assert run(evidence)["validation_outcome"] == "incomplete"


def test_future_rehearsal_is_not_proof(evidence):
    evidence["producer"]["updated_at"] = "2026-10-02T00:00:00Z"
    assert run(evidence)["validation_outcome"] == "incomplete"


@pytest.mark.parametrize("which", ["integration", "producer"])
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_failed_producers_override_pass(evidence, which, conclusion):
    evidence[which]["conclusion"] = conclusion
    assert run(evidence)["validation_outcome"] == "fail"


def test_missing_rehearsal_is_incomplete(evidence):
    evidence["artifact"] = None
    assert run(evidence)["validation_outcome"] == "incomplete"


def test_failed_integration_without_artifact_is_adverse(evidence):
    evidence["stage"] = None
    evidence["integration"]["conclusion"] = "failure"
    assert run(evidence)["validation_outcome"] == "fail"


def test_missing_success_artifact_does_not_invent_report(evidence):
    evidence["stage"] = None
    assert run(evidence)["action"] == "artifact-unavailable"
    assert not (evidence["tmp"] / "validation_report.json").exists()


def test_pending_integration_does_not_use_old_run(evidence):
    evidence["integration"]["status"] = "in_progress"
    assert run(evidence)["action"] == "in-progress"
    assert not (evidence["tmp"] / "validation_report.json").exists()


def test_wrong_integration_identity_is_not_ingested(evidence):
    evidence["stage"]["run_url"] = "https://example.test/runs/19"
    assert run(evidence)["action"] == "artifact-unavailable"


def test_missing_timestamp_cannot_create_fresh_success(evidence):
    evidence["integration"]["created_at"] = None
    assert run(evidence)["action"] == "missing-producer-time"


def test_bounded_search(evidence):
    calls = []

    def fetch(path):
        if "release-integrate" in path:
            return {
                "workflow_runs": [
                    evidence["integration"],
                    dict(evidence["integration"], id=19),
                ]
            }
        return {"workflow_runs": [dict(evidence["producer"], id=n) for n in range(100)]}

    def download(repo, record, name, filename, dest):
        calls.append((record["id"], name))
        return evidence["stage"] if name == "release-stage-report" else None

    result = cloud.collect(
        {"release_evidence": {"rehearsal_repo": "example/build"}}, fetch, download
    )
    assert result["validation_outcome"] == "incomplete"
    assert len(calls) == 21
    assert (19, "release-stage-report") not in calls


def cloud_verdict(e, report_override=None):
    from tests.test_readiness import make_snapshot, LIBS

    value = report_override or report(e)
    snap = make_snapshot(
        ts="2026-10-01T13:00:00Z",
        validation_report=value,
        verify_install=json.loads((e["tmp"] / "verify_install.json").read_text()),
    )
    for lib in LIBS:
        snap["repos"][lib]["ci_status"]["head_sha"] = "abcdef123"
    return readiness.compute(snap, libraries=LIBS)


def test_collected_proof_is_green_only_for_current_source(evidence):
    run(evidence)
    assert cloud_verdict(evidence)["verdict"] == "green", cloud_verdict(evidence)
    value = report(evidence)
    value["commit_shas"][next(iter(readiness._GATE_SHA_LIBS))] = "moved"
    verdict = cloud_verdict(evidence, value)
    assert verdict["verdict"] == "stale"
    assert any("source moved" in s for s in verdict["stale_reasons"])


def test_missing_sha_and_old_evidence_stay_stale(evidence):
    run(evidence)
    value = report(evidence)
    value["commit_shas"] = {}
    assert cloud_verdict(evidence, value)["verdict"] == "stale"
    value = report(evidence)
    value["ts"] = "2026-01-01T00:00:00Z"
    assert cloud_verdict(evidence, value)["verdict"] == "stale"


def test_missing_attempt_cannot_match(evidence):
    evidence["artifact"].pop("run_attempt")
    evidence["producer"].pop("run_attempt")
    assert run(evidence)["validation_outcome"] == "incomplete"


def test_non_release_profile_is_not_release_evidence(evidence):
    evidence["stage"]["profile"] = "smoke"
    run(evidence)
    assert cloud_verdict(evidence)["verdict"] == "stale"
