"""tests/test_url_check.py — offline forbidden-URL regex guard (moved from PyAutoBuild)."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "heart" / "checks" / "url_check.sh"


def run(directory):
    return subprocess.run(
        ["bash", str(SCRIPT), str(directory)],
        capture_output=True,
        text=True,
    )


def test_clean_directory_passes(tmp_path):
    (tmp_path / "README.rst").write_text(
        "See `Try on Colab "
        "<https://colab.research.google.com/github/PyAutoLabs/autolens_workspace/blob/2026.4.13.6/start_here.ipynb>`_."
    )
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_mybinder_url_fails(tmp_path):
    (tmp_path / "README.rst").write_text(
        "See `Binder <https://mybinder.org/v2/gh/PyAutoLabs/autofit_workspace/main?filepath=foo.ipynb>`_."
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "mybinder.org" in result.stdout


def test_jammy2211_colab_owner_fails(tmp_path):
    (tmp_path / "docs.md").write_text(
        "https://colab.research.google.com/github/Jammy2211/autolens_workspace/blob/2026.4.13.6/start_here.ipynb"
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "Jammy2211" in result.stdout


def test_release_branch_ref_fails(tmp_path):
    (tmp_path / "intro.rst").write_text(
        "https://colab.research.google.com/github/PyAutoLabs/autolens_workspace/blob/release/start_here.ipynb"
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "/blob/release/" in result.stdout


def test_unscanned_extensions_ignored(tmp_path):
    (tmp_path / "data.txt").write_text("https://mybinder.org/v2/gh/foo/bar/main")
    result = run(tmp_path)
    assert result.returncode == 0


def test_nested_directories_scanned(tmp_path):
    nested = tmp_path / "docs" / "tutorials"
    nested.mkdir(parents=True)
    (nested / "intro.rst").write_text(
        "https://colab.research.google.com/github/Jammy2211/autofit_workspace/blob/release/foo.ipynb"
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "intro.rst" in result.stdout


def test_missing_directory_errors(tmp_path):
    result = run(tmp_path / "does-not-exist")
    assert result.returncode == 2


def test_ipynb_files_scanned(tmp_path):
    (tmp_path / "demo.ipynb").write_text(
        '{"cells": [{"source": ["[Binder](https://mybinder.org/v2/gh/foo/bar/main)"]}]}'
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "demo.ipynb" in result.stdout


def test_blob_main_colab_url_fails(tmp_path):
    (tmp_path / "README.md").write_text(
        "https://colab.research.google.com/github/PyAutoLabs/HowToLens/blob/main/notebooks/chapter_1_introduction/tutorial_1_grids_and_galaxies.ipynb"
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "release bumper skips main" in result.stdout


def test_blob_main_non_notebook_repo_passes(tmp_path):
    # /blob/main/ is only forbidden for the six notebook repos — other repos'
    # Colab links (none exist today) are not the bumper's problem.
    (tmp_path / "README.md").write_text(
        "https://colab.research.google.com/github/PyAutoLabs/PyAutoFit/blob/main/example.ipynb"
    )
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_workspace_chapter_colab_url_fails(tmp_path):
    (tmp_path / "README.md").write_text(
        "https://colab.research.google.com/github/PyAutoLabs/autogalaxy_workspace/blob/2026.7.6.649/notebooks/chapter_1_introduction/tutorial_0_visualization.ipynb"
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "chapters live in HowToFit/HowToGalaxy/HowToLens" in result.stdout


def test_howto_chapter_colab_url_passes(tmp_path):
    (tmp_path / "README.md").write_text(
        "https://colab.research.google.com/github/PyAutoLabs/HowToGalaxy/blob/2026.7.6.649/notebooks/chapter_1_introduction/tutorial_0_visualization.ipynb"
    )
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_workspace_non_chapter_notebook_passes(tmp_path):
    (tmp_path / "README.md").write_text(
        "https://colab.research.google.com/github/PyAutoLabs/autolens_workspace/blob/2026.7.6.649/notebooks/imaging/start_here.ipynb"
    )
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
