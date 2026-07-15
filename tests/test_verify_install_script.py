"""tests/test_verify_install_script.py — fast argument/surface checks only.

The checks themselves create venvs and install from PyPI (minutes-class,
network-bound) and are exercised by running `pyauto-heart verify_install`,
never from the unit suite.
"""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "heart" / "checks" / "verify_install.sh"


def run(*args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True
    )


def test_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
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
    assert 'vi_index=testpypi; else vi_index=pypi' in text
    assert 'VI_INDEX="$vi_index"' in text
    assert '"index": os.environ.get("VI_INDEX") or "pypi",' in text


def test_help_documents_the_sidecar_index():
    result = run("--help")
    assert result.returncode == 0
    assert "index" in result.stdout
