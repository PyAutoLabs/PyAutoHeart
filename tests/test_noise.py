"""tests/test_noise.py — dirty-file real-vs-noise classification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heart import noise

# The default workspace noise globs (kept in sync with config/repos.yaml).
GLOBS = [
    "*.fits",
    "*tracer.json",
    "*point_dataset*.json",
    "*positions.json",
    "*data.json",
    "*model.json",
    "*model_*.json",
    "*max_log_likelihood.json",
    "*galaxies.json",
    "*dataset.json",
    "*tracer_*.json",
    "*point_dataset*.csv",
    "*.npy",
    "*summary.json",
    "*summary_v*.json",
    "*comparison.json",
    "*hpc_*.json",
    "*.png",
    "*README.md",
    "*test_report.md",
]


# A representative porcelain sample drawn from the real pyauto-status output
# the user pasted: workspace_test (all generated), workspace_developer (real
# .py + an untracked results dir), euclid (mixed).
SAMPLE = [
    " M README.md",
    " M dataset/build/imaging/no_lens_light/data.fits",
    " M dataset/build/imaging/no_lens_light/noise_map.fits",
    " M dataset/build/imaging/no_lens_light/tracer.json",
    " M dataset/multi/lens_sersic/g_tracer.json",
    " M dataset/point_source/simple/point_dataset_positions_only.json",
    " M source_science/results/1_with_lens_light/fit_compare.py",
    " M CLAUDE.md",
    " M util.py",
    "?? test_report.md",
    "?? jax_profiling/results/jit/interferometer/mge/sma/",
    "?? new_module.py",
]


def test_generated_artifacts_are_noise():
    real, noise_files = noise.classify_dirty(SAMPLE, GLOBS)
    for expected in [
        "README.md",
        "dataset/build/imaging/no_lens_light/data.fits",
        "dataset/build/imaging/no_lens_light/noise_map.fits",
        "dataset/build/imaging/no_lens_light/tracer.json",
        "dataset/multi/lens_sersic/g_tracer.json",
        "dataset/point_source/simple/point_dataset_positions_only.json",
        "test_report.md",
    ]:
        assert expected in noise_files, expected
        assert expected not in real, expected


def test_untracked_directory_is_noise():
    _, noise_files = noise.classify_dirty(SAMPLE, GLOBS)
    assert "jax_profiling/results/jit/interferometer/mge/sma/" in noise_files


def test_source_changes_are_real():
    real, noise_files = noise.classify_dirty(SAMPLE, GLOBS)
    # A .py under results/ is real source despite the path — no broad
    # */results/* glob is used.
    for expected in [
        "source_science/results/1_with_lens_light/fit_compare.py",
        "CLAUDE.md",
        "util.py",
    ]:
        assert expected in real, expected
        assert expected not in noise_files, expected


def test_untracked_source_file_is_real():
    # An untracked *file* (not a directory) could be new source — keep it real.
    real, noise_files = noise.classify_dirty(SAMPLE, GLOBS)
    assert "new_module.py" in real
    assert "new_module.py" not in noise_files


# --- v1.4: generated json/png artifacts (the HowToFit dirty=315 fix) ---

GENERATED_SAMPLE = [
    " M dataset/example_1d/gaussian_x5/data.json",
    " M dataset/example_1d/gaussian_x5/model.json",
    " M output/imaging/model_0.json",
    " M output/imaging/model_1.json",
    " M output/x/max_log_likelihood.json",
    " M dataset/example_1d/gaussian_x5/image.png",
    " M docs/figure.png",
    # genuine source that MUST stay real even though it sits under results/:
    " M source_science/results/1_with_lens_light/fit_compare.py",
    " M source_science/results/make_cross_experiment_plot.py",
    " M config/general.yaml",
]


def test_generated_json_and_png_are_noise():
    real, noise_files = noise.classify_dirty(GENERATED_SAMPLE, GLOBS)
    for expected in [
        "dataset/example_1d/gaussian_x5/data.json",
        "dataset/example_1d/gaussian_x5/model.json",
        "output/imaging/model_0.json",
        "output/imaging/model_1.json",
        "output/x/max_log_likelihood.json",
        "dataset/example_1d/gaussian_x5/image.png",
        "docs/figure.png",
    ]:
        assert expected in noise_files, expected
        assert expected not in real, expected


def test_py_under_results_stays_real_despite_generated_neighbours():
    # The load-bearing guard: file-type globs must NOT swallow hand-edited .py
    # that happens to live under a results/ path. A directory rule would.
    real, noise_files = noise.classify_dirty(GENERATED_SAMPLE, GLOBS)
    for expected in [
        "source_science/results/1_with_lens_light/fit_compare.py",
        "source_science/results/make_cross_experiment_plot.py",
        "config/general.yaml",
    ]:
        assert expected in real, expected
        assert expected not in noise_files, expected


def test_generated_leftover_json_is_noise():
    # v1.5: galaxies / profiling-summary / jax-result jsons that escaped v1.4.
    sample = [
        " M dataset/imaging/simple/galaxies.json",
        "?? ep/profiles/postfix_N3_summary.json",
        " M jax_profiling/results/jit/imaging/delaunay/ao/comparison.json",
        " M jax_profiling/results/jit/imaging/delaunay/ao/hpc_a100_fp64.json",
        " M jax_profiling/results/jit/imaging/delaunay/ao/hpc_a100_mp.json",
    ]
    real, noise_files = noise.classify_dirty(sample, GLOBS)
    assert real == []
    assert len(noise_files) == len(sample)


def test_summary_glob_does_not_hide_py_module():
    # `*summary.json` must not be confused with a real summary.py source file.
    real, noise_files = noise.classify_dirty(
        [" M graphical/summary.py", " M x/postfix_summary.json"], GLOBS
    )
    assert "graphical/summary.py" in real
    assert "x/postfix_summary.json" in noise_files


def test_config_json_not_matched_by_data_or_model_globs():
    # A config-ish json that isn't a generated artifact stays real.
    real, noise_files = noise.classify_dirty([" M dataset/example_1d/info.json"], GLOBS)
    assert real == ["dataset/example_1d/info.json"]
    assert noise_files == []


def test_globs_in_sync_with_repo_config():
    # The test GLOBS constant must match what's actually shipped in repos.yaml.
    from pathlib import Path
    here = Path(__file__).resolve().parents[1]
    assert set(noise.load_noise_globs(here / "config" / "repos.yaml")) == set(GLOBS)


def test_all_test_workspace_dirty_is_noise():
    # The 19-file autolens_workspace_test sample is 100% generated → 0 real.
    sample = [
        " M README.md",
        " M dataset/build/imaging/no_lens_light/data.fits",
        " M dataset/build/imaging/no_lens_light/noise_map.fits",
        " M dataset/build/imaging/no_lens_light/tracer.json",
        " M dataset/build/point_source/point_dataset.json",
        " M dataset/multi/lens_sersic/r_tracer.json",
    ]
    real, noise_files = noise.classify_dirty(sample, GLOBS)
    assert real == []
    assert len(noise_files) == len(sample)


def test_rename_keeps_destination_path():
    real, _ = noise.classify_dirty(["R  old_name.py -> new_name.py"], GLOBS)
    assert real == ["new_name.py"]


def test_blank_lines_ignored():
    real, noise_files = noise.classify_dirty(["", "   ", " M a.py"], GLOBS)
    assert real == ["a.py"]
    assert noise_files == []


def test_load_noise_globs_from_repo_config():
    here = Path(__file__).resolve().parents[1]
    globs = noise.load_noise_globs(here / "config" / "repos.yaml")
    assert "*.fits" in globs
    assert "*tracer.json" in globs


def test_load_noise_globs_missing_file_returns_empty(tmp_path):
    assert noise.load_noise_globs(tmp_path / "nope.yaml") == []


# --- v1.6: line-level split + batch mode (the health_sync dashboard path) ---


def test_split_lines_preserves_codes():
    real_lines, noise_lines = noise.split_lines(SAMPLE, GLOBS)
    assert " M util.py" in real_lines
    assert "?? new_module.py" in real_lines
    assert " M dataset/build/imaging/no_lens_light/data.fits" in noise_lines
    assert "?? test_report.md" in noise_lines


def test_split_lines_rename_classified_by_destination():
    real_lines, noise_lines = noise.split_lines(
        ["R  old.py -> new.py", "R  raw.txt -> plot.png"], GLOBS
    )
    assert real_lines == ["R  old.py -> new.py"]
    assert noise_lines == ["R  raw.txt -> plot.png"]


def test_classify_dirty_matches_split_lines():
    # classify_dirty is a path-projection of split_lines — same partition.
    real, noise_files = noise.classify_dirty(SAMPLE, GLOBS)
    real_lines, noise_lines = noise.split_lines(SAMPLE, GLOBS)
    assert len(real) == len(real_lines)
    assert len(noise_files) == len(noise_lines)


def test_sweep_glob_gaps_are_noise():
    # The 2026-07-09 repo_cleanup sweep found these slipping through.
    sample = [
        " M dataset/imaging/los_halos/los_halo_list.npy",
        " M dataset/point_source/deblending/tracer_imaging.json",
        " M dataset/point_source/deblending/tracer_point.json",
        " M dataset/point_source/simple/point_dataset_positions_only.csv",
        " M dataset/weak/simple/dataset.json",
        "?? results/simulators/imaging_summary_v2026.5.14.2.json",
        " M scripts/scaling_relation.csv.py",  # .py stays real
    ]
    real, noise_files = noise.classify_dirty(sample, GLOBS)
    assert real == ["scripts/scaling_relation.csv.py"]
    assert len(noise_files) == 6


def test_batch_mode_roundtrip(tmp_path, capsys):
    in_dir = tmp_path / "p"
    out_dir = tmp_path / "real"
    in_dir.mkdir()
    (in_dir / "ws_mixed").write_text(
        " M util.py\n M data.fits\n?? new_module.py\n?? plot.png\n"
    )
    (in_dir / "ws_noise_only").write_text(" M a.fits\n M b.png\n")

    config = tmp_path / "repos.yaml"
    config.write_text("noise_globs:\n  - '*.fits'\n  - '*.png'\n")

    rc = noise.run_batch(in_dir, out_dir, str(config))
    assert rc == 0

    lines = capsys.readouterr().out.splitlines()
    assert "ws_mixed\t1\t1\t2" in lines  # 1 real mod, 1 real untracked, 2 noise
    assert "ws_noise_only\t0\t0\t2" in lines
    assert (out_dir / "ws_mixed").read_text() == " M util.py\n?? new_module.py\n"
    assert (out_dir / "ws_noise_only").read_text() == ""


def test_batch_mode_via_cli(tmp_path, capsys):
    in_dir = tmp_path / "p"
    in_dir.mkdir()
    (in_dir / "repo").write_text(" M x.fits\n M y.py\n")
    config = tmp_path / "repos.yaml"
    config.write_text("noise_globs:\n  - '*.fits'\n")

    rc = noise.main(
        ["--batch", str(in_dir), "--real-out", str(tmp_path / "real"), "--config", str(config)]
    )
    assert rc == 0
    assert "repo\t1\t0\t1" in capsys.readouterr().out
    assert (tmp_path / "real" / "repo").read_text() == " M y.py\n"


def test_build_sidecar_shape():
    sidecar = noise.build_sidecar(
        name="autolens_workspace_test",
        group="workspaces_test",
        branch="main",
        ahead=0,
        behind=0,
        upstream="origin/main",
        ts="2026-05-30T00:00:00+00:00",
        porcelain_lines=SAMPLE,
        noise_globs=GLOBS,
    )
    assert sidecar["dirty_files"] == sidecar["dirty_real"] + sidecar["dirty_noise"]
    assert sidecar["dirty_real"] == len(sidecar["real_files"])
    assert sidecar["dirty_noise"] == len(sidecar["noise_files"])
    assert sidecar["present"] is True
    # round-trips as JSON
    json.dumps(sidecar)
