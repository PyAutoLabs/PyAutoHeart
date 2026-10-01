"""Read completed validation artifacts for the ephemeral cloud board.

No dispatch, build or publication. This bounded daily collector is deliberately
outside tick: a fresh runner needs both stages, not the local integrate cache.
The validator remains the only fold, and readiness remains the only verdict.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from heart import state, validate
from heart.checks import release_run


def api(path: str) -> dict:
    try:
        result = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def download(repo: str, run: dict, name: str, filename: str, dest: Path) -> dict | None:
    try:
        result = subprocess.run(
            [
                "gh",
                "run",
                "download",
                str(run["id"]),
                "--repo",
                repo,
                "--name",
                name,
                "--dir",
                str(dest),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            return None
        data = json.loads((dest / filename).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None


def matching_rehearsal(
    stage: dict, integration: dict, artifact: dict, run: dict
) -> bool:
    """Require actual producer identity and an exact wheel version, not proximity."""
    start = release_run._parse_ts(integration.get("created_at"))
    produced = release_run._parse_ts(run.get("updated_at"))
    return bool(
        stage.get("version")
        and artifact.get("version") == stage["version"]
        and artifact.get("mode") == "rehearsal"
        and artifact.get("index") == "testpypi"
        and artifact.get("run_id")
        and artifact.get("run_attempt")
        and run.get("run_attempt")
        and str(artifact.get("run_id")) == str(run.get("id"))
        and str(artifact.get("run_attempt")) == str(run.get("run_attempt"))
        and artifact.get("build_sha")
        and artifact["build_sha"] == run.get("head_sha")
        and run.get("status") == "completed"
        and start
        and produced
        and produced <= start
    )


def collect(config: dict, fetch=api, get_artifact=download) -> dict[str, Any]:
    """Observe the newest integration only; never fall back to an older pass.

    Callables are injectable for offline provenance and freshness tests.
    All temporary and persisted files live inside HEART_STATE_DIR.
    """
    source = config["release_evidence"]
    repo = release_run.RELEASE_REPO
    runs = fetch(
        f"repos/{repo}/actions/workflows/release-integrate.yml/runs?branch=main&per_page=1"
    ).get("workflow_runs", [])
    if not runs:
        return {"action": "unavailable"}
    integration = runs[0]
    if integration.get("status") != "completed":
        return {"action": "in-progress", "run_url": integration.get("html_url")}
    observed = release_run._parse_ts(integration.get("created_at"))
    if observed is None:
        return {"action": "missing-producer-time"}
    adverse = integration.get("conclusion") != "success"
    state.HEART_STATE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=state.HEART_STATE_DIR) as tmp:
        root = Path(tmp)
        stage = get_artifact(
            repo,
            integration,
            "release-stage-report",
            "stage_report.json",
            root / "integration",
        )
        if (
            not stage
            or stage.get("stage") != "integrate"
            or stage.get("run_url") != integration.get("html_url")
        ):
            if not adverse:
                return {"action": "artifact-unavailable"}
            # A failed producer without an artifact is still adverse evidence.
            stage = {
                "stage": "integrate",
                "status": "fail",
                "run_url": integration.get("html_url"),
            }
        sources = []
        stage_path = root / "stage_report.json"
        state.atomic_write_json(stage_path, stage)
        sources.append(stage_path)
        matched = False
        if stage.get("version"):
            producer_repo = source["rehearsal_repo"]
            candidates = fetch(
                f"repos/{producer_repo}/actions/workflows/release.yml/runs?branch=main&per_page=20"
            ).get("workflow_runs", [])
            for candidate in candidates[:20]:
                created = release_run._parse_ts(candidate.get("created_at"))
                if (
                    not created
                    or created > observed
                    or candidate.get("status") != "completed"
                ):
                    continue
                artifact = get_artifact(
                    producer_repo,
                    candidate,
                    "testpypi-rehearsal-version",
                    "rehearsal.json",
                    root / str(candidate.get("id")),
                )
                if not artifact or not matching_rehearsal(
                    stage, integration, artifact, candidate
                ):
                    continue
                # A matching unsuccessful producer cannot be laundered by its
                # artifact or replaced by another, older producer.
                adverse = adverse or candidate.get("conclusion") != "success"
                observed = min(observed, created)
                path = root / "rehearsal.json"
                state.atomic_write_json(path, artifact)
                sources.append(path)
                matched = True
                break
        report = validate.run(sources, now=observed, force_fail=adverse)
        return {
            "action": "ingested",
            "rehearsal_matched": matched,
            "run_url": integration.get("html_url"),
            "validation_outcome": report["validation_outcome"],
            "ts": report["ts"],
        }


def main() -> int:
    config = yaml.safe_load((release_run.HEART_HOME / "config/repos.yaml").read_text())
    result = collect(config)
    state.atomic_write_json(state.HEART_STATE_DIR / "cloud_validation.json", result)
    from heart.heart_color import c_info

    print(c_info("cloud_validation: " + json.dumps(result, sort_keys=True)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
