"""tests/test_verify_install_script.py — fast argument/surface checks only.

The checks themselves create venvs and install from PyPI (minutes-class,
network-bound) and are exercised by running `pyauto-heart verify_install`,
never from the unit suite.
"""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "heart" / "checks" / "verify_install.sh"
HELPERS = SCRIPT.with_name("verify_install_helpers.sh")
WORKFLOW = ROOT / ".github" / "workflows" / "workspace-validation.yml"


def run(*args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True
    )


def classify_rejection(output, version="2026.7.29.1"):
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; pip_output=$(cat); '
            'verify_install_requires_python_rejection "$pip_output" "$2"',
            "classifier",
            str(HELPERS),
            version,
        ],
        input=output,
        capture_output=True,
        text=True,
    )


def test_bash_syntax():
    for path in (SCRIPT, HELPERS):
        result = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr


def test_help_lists_all_checks_including_colab_simulation():
    result = run("--help")
    assert result.returncode == 0
    for letter in "ABCDEF":
        assert f"\n  {letter}   " in result.stdout, f"check {letter} missing from help"
    assert "Colab simulation" in result.stdout


def test_unknown_argument_rejected():
    result = run("--bogus")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_check_f_wired_into_selection_and_runner():
    text = SCRIPT.read_text()
    assert "SELECTED=(A B C D E F)" in text
    assert "F) check_f ;;" in text
    assert "A|B|C|D|E|F|all)" in text


def test_no_stale_workspace_owner():
    # The workspaces moved Jammy2211 -> PyAutoLabs; clones must use the new owner.
    assert "github.com/Jammy2211/autolens_workspace" not in SCRIPT.read_text()


def test_sidecar_records_the_package_index():
    # readiness reports this index, so a --testpypi run never reads as proof that
    # installing from PyPI works. Static check: running the writer for real means
    # a venv + a PyPI install, which this file deliberately never does.
    text = SCRIPT.read_text()
    assert "vi_index=find-links" in text
    assert "vi_index=testpypi" in text
    assert "vi_index=pypi" in text
    assert 'VI_INDEX="$vi_index"' in text
    assert 'VI_CHECK_B_VERSION="$CHECK_B_VERSION"' in text
    assert '"index": os.environ.get("VI_INDEX") or "pypi",' in text
    assert '"check_b_version": os.environ.get("VI_CHECK_B_VERSION") or None,' in text


def test_help_documents_the_sidecar_index():
    result = run("--help")
    assert result.returncode == 0
    assert "index" in result.stdout


def test_find_links_is_documented_and_missing_directory_is_rejected():
    help_result = run("--help")
    missing_result = run("B", "--find-links", "/definitely/missing/wheels")

    assert "--find-links DIR" in help_result.stdout
    assert "Evidence is labelled" in help_result.stdout
    assert missing_result.returncode == 2
    assert "is not a directory" in missing_result.stderr


def test_check_b_uses_the_new_floor_contract():
    text = SCRIPT.read_text()

    assert "check_b_supported python3.12" in text
    assert "check_b_supported python3.13" in text
    assert "check_b_rejected" in text
    assert "python3.9" not in text
    assert "python3.10" not in text
    assert "banner present" not in text
    assert "no banner detected" not in text


def test_check_b_reuses_one_exact_version_and_classifies_rejection():
    text = SCRIPT.read_text()

    assert 'CHECK_B_VERSION="$TARGET_VERSION"' in text
    assert 'install_target="autolens==$CHECK_B_VERSION"' in text
    assert "verify_install_requires_python_rejection" in text
    assert "verify_install_versions_equivalent" in text
    assert "failed without a Requires-Python rejection" in text


def test_check_e_uses_python_312_for_the_historical_stack():
    text = SCRIPT.read_text()
    body = text[text.index("check_e()") : text.index("# ----- check F:")]

    assert 'command -v python3.12 > /dev/null 2>&1' in body
    assert 'RESULTS+=("E|FAIL|python3.12 not installed")' in body
    assert 'if ! make_venv "$venv" python3.12; then' in body
    assert 'if ! make_venv "$venv" python3; then' not in body
    assert '(python3.12)' in body
    assert "installs on python3.12 by explicit pin" in text
    assert "Check E requires python3.12" in (ROOT / "bin/pyauto-heart").read_text()
    assert "still installs on `python3.12` by explicit pin" in (
        ROOT / "skills/verify_install/verify_install.md"
    ).read_text()


def test_version_comparison_runs_before_supported_venv_deactivation():
    text = SCRIPT.read_text()
    body = text[
        text.index("check_b_supported()") : text.index("check_b_rejected()")
    ]
    comparison = body.index("&& ! verify_install_versions_equivalent")
    deactivate_after_comparison = body.index("\n    deactivate\n", comparison)

    assert comparison < deactivate_after_comparison


def test_rejection_classifier_accepts_candidate_and_index_pip_forms():
    candidate_output = (
        "ERROR: Package 'autolens' requires a different Python: "
        "3.11.9 not in '>=3.12'\n"
    )
    index_output = (
        "ERROR: Ignored the following versions that require a different "
        "python version: 2026.7.29.1 Requires-Python >=3.12\n"
        "ERROR: Could not find a version that satisfies the requirement "
        "autolens==2026.7.29.1 (from versions: none)\n"
    )

    assert classify_rejection(candidate_output).returncode == 0
    assert classify_rejection(index_output).returncode == 0


def test_rejection_classifier_rejects_unrelated_pip_failures():
    network_failure = (
        "WARNING: Retrying after connection broken by NewConnectionError\n"
        "ERROR: No matching distribution found for autolens==2026.7.29.1\n"
    )
    dependency_conflict = (
        "ERROR: Cannot install autolens==2026.7.29.1 because these package "
        "versions have conflicting dependencies.\n"
    )
    wrong_floor = (
        "ERROR: Package 'autolens' requires a different Python: "
        "3.11.9 not in '>=3.13'\n"
    )

    for output in (network_failure, dependency_conflict, wrong_floor):
        assert classify_rejection(output).returncode != 0


def test_pep440_equivalent_versions_compare_equal():
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; verify_install_versions_equivalent python3 "$2" "$3"',
            "versions",
            str(HELPERS),
            "2026.07.028.1",
            "2026.7.28.1",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_release_workflow_exposes_every_required_interpreter():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    steps = jobs["verify_install_release"]["steps"]
    run_index = next(
        index for index, step in enumerate(steps)
        if step.get("name") == "Run verify_install A-F against the TestPyPI wheels"
    )
    setup_versions = [
        str(step.get("with", {}).get("python-version"))
        for index, step in enumerate(steps)
        if index < run_index and step.get("uses") == "actions/setup-python@v5"
    ]

    assert setup_versions == ["3.11", "3.12", "3.13"]
    workflow_text = WORKFLOW.read_text()
    assert "Checks B and E invoke explicit pythonX.Y binaries" in workflow_text
    assert "Expose Python 3.12 for Checks B and E" in workflow_text


def test_help_describes_supported_success_and_below_floor_rejection():
    result = run("--help")

    assert result.returncode == 0
    assert "installs on python3.12 and python3.13" in result.stdout
    assert "python3.11 rejects it because Requires-Python is >=3.12" in result.stdout
    assert "applies to A, B, C, D" in result.stdout
