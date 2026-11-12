"""The JAX compatibility workflow tests the range the library stack admits.

Its endpoints moved to jax 0.12 with the release floor (jax/jaxlib >=0.12.0,<0.13);
its replay manifest must be one that can resolve them. The previous manifest's
library pins capped jax below 0.12, so pairing it with a 0.12 endpoint fails at
install for reasons unrelated to jax.
"""

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/jax-compatibility.yml"
sys.path.insert(0, str(ROOT / ".github/scripts"))

import release_diagnostic as diag  # noqa: E402


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _manifest_path():
    match = re.search(r"Path\('(diagnostics/[^']+\.json)'\)", WORKFLOW.read_text())
    assert match, "workflow must read a manifest under diagnostics/"
    return ROOT / match.group(1)


def test_endpoints_are_inside_the_jax_floor():
    endpoints = _workflow()["jobs"]["compatibility"]["strategy"]["matrix"]["jax"]
    assert endpoints
    for version in endpoints:
        major, minor = (int(part) for part in version.split(".")[:2])
        assert (major, minor) == (0, 12), version


def test_workflow_manifest_loads_and_is_a_jax_0_12_candidate():
    manifest = diag.load_manifest(_manifest_path())
    assert manifest["packages"]["jax"].startswith("0.12.")
    assert manifest["packages"]["jaxlib"].startswith("0.12.")


def test_pull_request_paths_track_the_manifest_the_workflow_reads():
    on = _workflow()[True]  # YAML 1.1 reads the bare key `on` as True
    rel = _manifest_path().relative_to(ROOT).as_posix()
    assert rel in on["pull_request"]["paths"]


def test_the_incident_manifest_is_kept_for_its_own_diagnostic():
    old = ROOT / "diagnostics/release-37199991757.json"
    assert json.loads(old.read_text())["packages"]["jax"] == "0.10.2"
