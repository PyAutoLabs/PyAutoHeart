"""tests/test_manifest_drift.py — body-map identity drift check."""

from __future__ import annotations

import json

from heart.checks import manifest_drift as md


OK_OUTPUT = """\
check PyAutoHeart/config/repos.yaml: OK
check PyAutoHands/pre_build.sh: OK
check ensure_workspace_labels.sh: OK
check local checkout origins: OK
"""

DRIFT_OUTPUT = """\
check PyAutoHeart/config/repos.yaml: OK
check ensure_workspace_labels.sh: 2 mismatch(es)
  ✗ ensure_workspace_labels targets 'rhayes777/PyAutoFit', manifest says 'PyAutoLabs/PyAutoFit'
  ✗ ensure_workspace_labels targets 'rhayes777/PyAutoConf', manifest says 'PyAutoLabs/PyAutoConf'
check local checkout origins: 1 mismatch(es)
  ✗ 'PyAutoFit': origin is 'rhayes777/PyAutoFit', manifest says 'PyAutoLabs/PyAutoFit'
"""


def test_parse_all_ok():
    checks = md.parse_check_output(OK_OUTPUT)
    assert len(checks) == 4
    assert all(c["ok"] for c in checks.values())
    assert all(c["problems"] == [] for c in checks.values())


def test_parse_drift_attributes_problems_to_surfaces():
    checks = md.parse_check_output(DRIFT_OUTPUT)
    assert checks["PyAutoHeart/config/repos.yaml"]["ok"] is True
    labels = checks["ensure_workspace_labels.sh"]
    assert labels["ok"] is False
    assert len(labels["problems"]) == 2
    assert "rhayes777/PyAutoFit" in labels["problems"][0]
    origins = checks["local checkout origins"]
    assert origins["ok"] is False
    assert len(origins["problems"]) == 1


def test_parse_garbage_yields_nothing():
    assert md.parse_check_output("Traceback (most recent call last):\n  boom\n") == {}


def _run_with_fake_script(tmp_path, monkeypatch, script_body: str | None):
    """Run md.run() against a fake workspace root; None = no script on disk."""
    if script_body is not None:
        scripts = tmp_path / "PyAutoMind" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "repos_sync.py").write_text(script_body)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(md, "PYAUTO_ROOT", tmp_path)
    monkeypatch.setattr(md, "HEART_STATE_DIR", state_dir)
    result = md.run()
    on_disk = json.loads((state_dir / "manifest_drift.json").read_text())
    assert on_disk == result
    return result


def test_run_missing_script_is_unavailable_not_green(tmp_path, monkeypatch):
    result = _run_with_fake_script(tmp_path, monkeypatch, None)
    assert result["available"] is False
    assert "missing" in result["reason"]


def test_run_parses_fake_script_report(tmp_path, monkeypatch):
    body = "import sys\nsys.stdout.write('''" + DRIFT_OUTPUT + "''')\nsys.exit(1)\n"
    result = _run_with_fake_script(tmp_path, monkeypatch, body)
    assert result["available"] is True
    assert result["problem_count"] == 3
    assert result["checks"]["ensure_workspace_labels.sh"]["ok"] is False


def test_run_unparseable_output_is_unavailable(tmp_path, monkeypatch):
    body = "import sys\nsys.stderr.write('boom')\nsys.exit(1)\n"
    result = _run_with_fake_script(tmp_path, monkeypatch, body)
    assert result["available"] is False
    assert "unparseable" in result["reason"]
