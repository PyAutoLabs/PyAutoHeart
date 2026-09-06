# Unit-test + import-time bottleneck census (2026-09-06)

Phase 9 of the `ci-timing-fast-tests` epic (PyAutoMind
`draft/feature/pyautoheart/ci_timing_fast_tests_epic.md`; PyAutoHeart#213), the last
one. It lives beside the legacy digest in `timings/`, and it is the **unit/import**
half the legacy round left as a placeholder — that document's §"Not yet in this round"
named exactly this gap.

This is a **research verdict, not a refactor**: no library source, test or workflow
was changed by the pass that produced it. Every option in §6 is a proposal a human
accepts or refuses, and each accepted one is filed as its own prompt.

**Epoch.** Every number here is inside the `fast-tests` epoch
(`timings/epochs.jsonl` → `fast-tests @ 2026-09-06`). Nothing here may be compared
against a `legacy @ 2026-09-05` row.

## Sources, and which side each number comes from

Two instruments, never mixed in one cell:

- **CI (the record).** `timings/unit/<repo>.jsonl`, ingested by the heart-health run
  of 2026-09-06 from the `unit-timings-<py>` artifacts `lib-tests.yml` now emits.
  Three repos have rows:

  | repo | run id | run URL | head | py legs |
  |---|---|---|---|---|
  | PyAutoNerves | 34038040174 | <https://github.com/PyAutoLabs/PyAutoNerves/actions/runs/34038040174> | `88c43eb` | 3.12, 3.13 |
  | PyAutoArray | 34039841741 | <https://github.com/PyAutoLabs/PyAutoArray/actions/runs/34039841741> | `499e087` | 3.12, 3.13 |
  | PyAutoFit | 34039848597 | <https://github.com/PyAutoLabs/PyAutoFit/actions/runs/34039848597> | `e27eb8c` | 3.12, 3.13 |

  All three heads are the phase-8c branch of this epic. A CI row's
  per-test seconds are the junit `time` (setup+call+teardown); `import_s` is a
  fresh-process `python -c "import <pkg>"` measured on the runner.

- **Local (labelled `local` everywhere it appears).** One 4-core container,
  Python 3.12.3, the same clones the phase-8c work used: PyAutoNerves / PyAutoArray /
  PyAutoFit are the **phase-8c branch of this epic, installed editable**;
  PyAutoGalaxy (`6d216c1`) and PyAutoLens (`1f5b1e9`) are **`main`**. Warm: every suite run at least twice, the
  second reported; the numba and JAX caches are whatever the container had already
  built. `python3 -m pytest -q --durations=0 -p no:cacheprovider`, serial unless the
  row says `-n auto`.

**PyAutoGalaxy and PyAutoLens have no CI rows yet** — their first `lib-tests.yml` run
carrying the artifact has not been ingested. Every PyAutoGalaxy / PyAutoLens number
below is local and says so. That is the one measured gap in this document; §8 lists it
with the others.

## 1. Where library unit-test time is, per repo

| repo | tests | CI wall py3.12 | CI wall py3.13 | local serial | local `-n auto` | CI/local |
|---|---:|---:|---:|---:|---:|---:|
| PyAutoFit | 2446 (CI) / 2421+25 skipped (local) | 157.5 s | 144.8 s | 71.5 s | **cannot run** (§2.5) | 2.2x |
| PyAutoArray | 1449 | 101.2 s | 94.1 s | 44.3 s | 22.7 s | 2.3x |
| PyAutoGalaxy | 1171 | — | — | 34.3 s *(local)* | 28.8 s *(local)* | — |
| PyAutoLens | 614 | — | — | 36.5 s *(local)* | 21–25 s *(local)* | — |
| PyAutoNerves | 182 | 1.9 s | 1.8 s | 1.2 s | 1.2 s | 1.6x |

**What the CI/local factor is made of.** It is not all "the runner is slower".
`lib-tests.yml` runs `pytest --cov <pkg> --cov-report xml --junitxml=…`, serially, with
no compile caches. Measured locally with the same flags and the same pass counts:
PyAutoArray 45 s → **60 s** (+33 %), PyAutoFit 71.5 s → **120 s** (+68 %). Add the
24.8 s of cold-numba compilation §2.1 isolates, and the 2.2–2.3x is largely accounted
for by two workflow settings rather than by the tests.

**Concentration.** The top 5 % of tests carry almost the whole suite everywhere except
PyAutoLens:

| repo | top-5 % share of measured test time (local, serial) |
|---|---:|
| PyAutoArray | 97.0 % (72 tests, 39.1 s of 40.3 s) |
| PyAutoGalaxy | 95.0 % (59 tests, 30.5 s of 32.1 s) |
| PyAutoFit | 94.1 % (121 tests, 61.1 s of 64.9 s) |
| PyAutoLens | 58.3 % (31 tests, 20.1 s of 34.4 s) |
| PyAutoNerves | 100 % (9 tests, 0.10 s of 0.10 s) |

PyAutoLens is the exception and the reason matters: its cost is not a few tests, it is
**614 tests each doing a little**, on top of the largest import in the stack. It is the
one suite an option in §6 must reach through the *floor*, not through a hot spot.

**Fixture cost is not a finding anywhere.** Summed `setup` phase over each whole suite:
PyAutoArray 0.36 s, PyAutoGalaxy 0.84 s, PyAutoLens 0.83 s, PyAutoNerves 0.01 s. Only
PyAutoFit has any at all — 11.8 s of setup against 53.1 s of call — and **4.72 s of
that 11.8 s is one fixture**, `test_autofit/mcp/test_mcp_tools.py::test_list_searches`,
which builds two complete search output trees on disk. The classification "fixture
cost" is used exactly twice in this document; it is not where the time is.

## 2. Slowest tests, classified

Classes: **compile** (a JAX/XLA or numba compilation inside the test), **search/fit** (a
real optimisation, sampler or fit run in a test), **numerics** (large-array or
scalar-loop arithmetic), **I/O** (files or figures written), **fixture**, **other**.

### 2.1 PyAutoArray — CI (run 34039841741), top 25

`local` is the same test in the local serial run. The column is diagnostic, not
decoration: **a row that costs seconds in CI and ~0.01 s locally is a compilation, not
work** — the container's numba cache is warm and `lib-tests.yml` restores nothing.

| CI 3.12 s | CI 3.13 s | local s | test | class | where the cost is |
|---:|---:|---:|---|---|---|
| 4.03 | 3.40 | 0.01 | `inversion/regularizations/test_curvature_mask.py::test__regularization_matrix_from__matches_hand_computed_operators` | compile (numba) | first call into the jitted regularization kernels |
| 3.62 | 3.86 | 0.01 | `operators/test_coarse_interp_util.py::test__coarse_interp_matrix_from__rows_are_partition_of_unity` | compile (numba) | `autoarray/operators/coarse_interp_util.py` — 5 `@numba_util.jit()` |
| 3.37 | 3.15 | 3.14 | `inversion/inversion/interferometer/test_interferometer.py::test__interferometer_sparse_operator__func_list_and_x2_mappers__identical_to_mapping` | compile (JAX) | **measured**: `jax/_src/compiler.py:330 backend_compile_and_load` = 3.51 s of the test, 105 compiles |
| 3.29 | 2.77 | 3.22 | `operators/test_transformer.py::test__nufft__chunk_size__visibilities_from_numpy_matches_unchunked` | compile (JAX) | **measured**: same frame = 3.49 s of 4.84 s, 148 compiles — reached through `autoarray/operators/transformer.py:345 _forward_native` (§5.1) |
| 3.27 | 3.21 | 0.01 | `inversion/inversion/test_nnls_memo.py::test__memoized_reconstruction__matches_unmemoized[0]` | compile (numba) | `autoarray/util/fnnls.py` / `cholesky_funcs.py` first call |
| 3.04 | 2.51 | 0.02 | `inversion/inversion/test_abstract.py::test__curvature_matrix__via_sparse_operator__identical_to_mapping` | compile (numba) | as above |
| 2.61 | 2.41 | 0.01 | `inversion/inversion/imaging/test_inversion_imaging_util.py::test__curvature_matrix_off_diags_via_mapper_and_blurred_curvature_weights_from__matches_dense_kernel[3x3]` | compile (numba) | `inversion_imaging_util` reaches jitted `mapper_util` |
| 1.86 | 1.57 | 1.71 | `operators/test_transformer.py::test__nufft__chunk_size__jax_jit_traces_with_scan` | compile (JAX) | deliberate: the test's subject *is* the traced `jax.lax.scan` |
| 1.83 | 1.60 | 1.87 | `inversion/regularizations/test_adapt_power_jax.py::test__split_builder__numpy_and_jax_agree` | compile (JAX) | numpy-vs-JAX parity test |
| 1.81 | 1.66 | 0.47 | `inversion/inversion/imaging/test_inversion_imaging_util.py::test__psf_weighted_noise_imaging_from` | compile (numba) + numerics | |
| 1.77 | 1.73 | 0.01 | `util/test_cholesky_inplace.py::test__interleaved_inserts_and_deletes__match` | compile (numba) | `autoarray/util/cholesky_funcs.py` — 5 `@numba_util.jit()` |
| 1.75 | 1.41 | 0.01 | `operators/test_derivative_util.py::test__derivative_1st_operators_from__exact_on_linear_function` | compile (numba) | `autoarray/operators/derivative_util.py` — 4 `@numba_util.jit()` |
| 1.69 | 1.83 | 0.86 | `inversion/pixelization/interpolator/test_delaunay_nn.py::test__smooth_source_mapping_is_numerically_close_to_delaunay` | numerics | natural-neighbour interpolation |
| 1.69 | 1.45 | 1.54 | `dataset/interferometer/test_dataset.py::test__dirty_image__shape_native_matches_real_space_mask` | compile (JAX) | reaches `_forward_native` (§5.1) |
| 1.69 | 1.79 | 0.01 | `operators/test_derivative_util.py::test__derivative_2nd_operators_from__exact_on_quadratic_function` | compile (numba) | |
| 1.66 | 1.51 | 1.51 | `inversion/inversion/interferometer/test_interferometer.py::test__interferometer_sparse_operator__func_list_and_mapper__identical_to_mapping` | compile (JAX) | |
| 1.65 | 1.45 | 1.55 | `inversion/pixelization/interpolator/test_delaunay.py::test__barycentric_dual_area__numpy_matches_jax_in_graph` | compile (JAX) | |
| 1.64 | 1.84 | 0.00 | `inversion/inversion/imaging/test_inversion_imaging_util.py::test__curvature_matrix_via_sparse_operator_from__matches_dense_psf_precision_operator[3x3]` | compile (numba) | |
| 1.61 | 1.44 | 1.80 | `inversion/pixelization/mappers/test_delaunay.py::test__areas_for_magnification__jax_matches_numpy` | compile (JAX) | |
| 1.57 | 1.38 | 1.33 | `operators/test_transformer.py::test__nufft__chunk_size__jax_paths_match_unchunked` | compile (JAX) | |
| 1.52 | 1.43 | 1.28 | `inversion/inversion/interferometer/test_interferometer.py::test__interferometer_sparse_operator__x2_mappers__identical_to_mapping` | compile (JAX) | |
| 1.47 | 1.29 | 1.51 | `operators/test_transformer.py::test__nufft__visibilities_from__all_ones_image__first_visibility_matches_expected` | compile (JAX) | §5.1 |
| 1.44 | 1.28 | 1.50 | `inversion/regularizations/test_kernel_jax_gradients.py::test__quadratic_form_via_cholesky__gradient_is_finite_difference_certified[gauss_cov_matrix_from]` | compile (JAX) | `jax.grad` certification |
| 1.44 | 1.41 | 0.01 | `operators/test_coarse_interp_util.py::test__binned_mask_from__coarse_pixel_unmasked_only_if_all_fine_unmasked` | compile (numba) | |
| 1.29 | 1.09 | 0.48 | `inversion/plot/test_inversion_plotters.py::test__inversion_subplot_of_mapper__is_output_for_all_inversions` | I/O | subplot written to disk |

**The arithmetic.** The 25 rows are 52.6 s of a 101.2 s leg. Ten of them cost ≤ 0.02 s
locally and **24.8 s in CI**: that is numba compilation, paid on every library CI run of
every library, and nothing in this suite's own timing says so — only the CI-vs-local
column does. A further ~20 s of the 25 is JAX/XLA compilation, which is paid in *both*
places.

### 2.2 PyAutoFit — CI (run 34039848597), top 25

| CI 3.12 s | CI 3.13 s | local s | test | class |
|---:|---:|---:|---|---|
| 10.11 | 8.52 | — | `non_linear/search/mcmc/test_blackjax_smc.py::test__cold_run_recovers_the_analytic_log_evidence` | search/fit + compile (JAX) |
| 7.21 | 6.33 | — | `…test_blackjax_smc.py::test__hmc_kernel_runs_and_holds_acceptance` | search/fit + compile (JAX) |
| 6.44 | 6.15 | 4.77 | `mcp/test_mcp_tools.py::test_list_searches` | **fixture** (4.72 s of it is `setup`) + I/O |
| 6.10 | 6.12 | 3.95 | `graphical/regression/test_linear_regression.py::test_laplace` | search/fit (EP/Laplace to convergence) |
| 5.93 | 6.49 | 3.10 | `non_linear/test_scaler.py::test__scaling_cannot_move_the_MAP` | numerics — **2 x 200 000 Python-loop gradient steps**, `test_scaler.py:333-340` |
| 4.62 | 3.87 | — | `…test_blackjax_smc.py::test__warm_run_recovers_the_analytic_log_evidence_through_the_bridge` | search/fit + compile (JAX) |
| 4.13 | 4.24 | — | `…test_blackjax_nuts.py::test__samples_via_internal_from_shape_contract` | search/fit + compile (JAX) |
| 4.13 | 3.62 | — | `…test_blackjax_smc.py::test__test_mode_runs_end_to_end` | search/fit + compile (JAX) |
| 3.98 | 3.72 | 2.01 | `graphical/stochastic/test_regression.py::test_stochastic_linear_regression` | search/fit |
| 3.36 | 3.20 | 3.06 | `tools/test_atomic_write.py::…::test__truncated_summary_lets_the_next_run_proceed` | I/O |
| 2.94 | 2.85 | 1.74 | `mapper/prior/test_prior_bounds_1489.py::TestEmceeContainment::test__unconstrained_parameter_stays_inside_declared_box` | search/fit (a real Emcee run) |
| 2.92 | 2.69 | 1.78 | `non_linear/search/test_sneaky_map.py::test_sneaky_map` | other (multiprocessing) |
| 2.92 | 2.95 | 2.36 | `non_linear/search/mle/test_multi_start_gradient.py::test__quick_update__fires_once_per_cadence_boundary_in_gradient_steps` | search/fit + compile (JAX) |
| 2.69 | 2.46 | 1.72 | `graphical/hierarchical/test_optimise.py::test_optimise` | search/fit |
| 2.54 | 2.60 | 1.53 | `graphical/gaussian/test_optimizer.py::TestDynesty::test_optimisation` | search/fit (a real Dynesty run) |
| 2.14 | 1.86 | 1.27 | `graphical/info/test_output.py::test_output` | I/O |
| 2.05 | 1.57 | 1.36 | `…test_multi_start_gradient.py::test__quick_update__terminal_boundary_is_skipped` | search/fit |
| 1.98 | 1.73 | 1.15 | `non_linear/search/nest/test_nautilus.py::test__single_core_builds_no_pool` | search/fit |
| 1.85 | 1.80 | 1.58 | `graphical/info/test_output.py::test_default_output` | I/O |
| 1.84 | 2.23 | 1.23 | `graphical/info/test_output.py::test_path_prefix` | I/O |
| 1.75 | 1.61 | — | `…test_blackjax_nuts.py::test__times_from_positions_clamps_low_ess` | search/fit + compile (JAX) |
| 1.74 | 1.60 | 1.80 | `…test_multi_start_gradient.py::test__quick_update__default_never_sentinel_stays_silent` | search/fit |
| 1.67 | 1.65 | — | `…test_blackjax_nuts.py::test__times_from_positions_multi_chain_pools_samples` | search/fit + compile (JAX) |
| 1.39 | 1.26 | 0.82 | `graphical/functionality/test_messages.py::test_normal_simplex` | numerics |
| 1.25 | — | 1.09 | `graphical/regression/test_logistic_regression.py::test_laplace` | search/fit |

`—` in the `local` column means the test **did not run locally**: `blackjax` is not
installed in this container, so its 7 tests are among the 25 the local run skips. They
are **33.6 s of the 157.5 s CI leg (21 %)** and are invisible to any local measurement
— the single largest thing a local `--durations` pass would have missed, and the reason
the CI record had to exist before this census could be written.

The shape of PyAutoFit is different from PyAutoArray's: **17 of the 25 rows are a real
search, fit or EP optimisation running inside a unit test**, not a compilation. Its
suite is long because it is broad (2446 tests, 71.5 s local serial with the top row at
4.8 s), not because a handful of tests are pathological.

### 2.3 PyAutoGalaxy — local, serial (no CI rows yet), top 25

| local s | test | class | where the cost is |
|---:|---|---|---|
| **17.84** | `profiles/mass/dark/test_kaplinghat.py::test__lensing_quantities_are_finite_and_positive` | numerics | **cProfile'd**: `autogalaxy/profiles/mass/dark/kaplinghat.py:217 _density_3d_from_radius` called **2 186 457** times (10.8 s self, 29.6 s cumulative) from the scalar `quad` lambda at `kaplinghat.py:258-263`, each call doing two scalar `np.interp` at `kaplinghat.py:46-51`. Three grid points. |
| 2.58 | `…test_kaplinghat.py::test__vmapped_deflections_match_instance_path_for_zero_interaction` | compile (JAX) | `vmapped_deflections_from` |
| 1.85 | `operate/test_deflections.py::test__tangential_critical_curve_list_from__small_datasets_env__evaluation_grid_keeps_extent` | compile (JAX) + numerics | |
| 1.02 | `…test_kaplinghat.py::test__vmapped_deflections_match_instance_path_for_sidm_core` | compile (JAX) | |
| 0.57 | `analysis/test_plotter.py::test__galaxies` | I/O | |
| 0.54 | `profiles/mass/dark/test_yang24.py::test__lensing_quantities_are_finite_and_positive` | numerics | same `quad`-over-a-Python-callable shape, smaller |
| 0.46 | `plot/test_plot_array_mask.py::…::test__explicit_mask_changes_the_rendered_figure` | I/O | |
| 0.44 | `analysis/test_plotter.py::test__inversion` | I/O | |
| 0.42 | `analysis/test_model_util.py::test__mge_model_from__single_basis_elliptical` | other | |
| 0.41 | `profiles/mass/input/test_gaussian_random_field.py::test__profile_potential_matches_realization_on_unmasked_pixels` | numerics | |
| 0.30 | `analysis/test_result.py::test__max_log_likelihood_galaxies_available_as_result` | search/fit | |
| 0.26 | `imaging/plot/test_fit_imaging_plotters.py::test__subplot_of_galaxy` | I/O | |
| 0.23 | `analysis/analysis/test_analysis.py::test__save_attributes__dataset_fits_output_for_aggregator` | I/O | |
| 0.23 | `profiles/mass/dark/test_yang24.py::test__vmapped_deflections_match_instance_path_for_zero_tau` | compile (JAX) | |
| 0.14 | `ellipse/model/test_analysis_ellipse.py::test__make_result__result_imaging_is_returned` | search/fit | |
| 0.14 | `imaging/model/test_analysis_imaging.py::test__make_result__result_imaging_is_returned` | search/fit | |
| 0.14 | `interferometer/plot/test_fit_interferometer_plotters.py::test__fit_sub_plot_real_space` | I/O | |
| 0.13 | `interferometer/model/test_analysis_interferometer.py::test__make_result__result_interferometer_is_returned` | search/fit | |
| 0.12 | `plot/test_plot_array_mask.py::…::test__mask_is_optional` | I/O | |
| 0.11 | `imaging/model/test_plotter_imaging.py::test__imaging` | I/O | |
| 0.11 | `imaging/model/test_plotter_imaging.py::test__fit_imaging_combined` | I/O | |
| 0.11 | `galaxy/plot/test_galaxies_plotter.py::test__galaxies_sub_plot_output` | I/O | |
| 0.11 | `operate/test_deflections.py::test__radial_critical_curve_list_from__compare_via_magnification` | numerics | |
| 0.10 | `ellipse/test_fit_ellipse.py::test__points_from_major_axis__zero_masked` | numerics | |
| 0.10 | `imaging/test_simulate_and_fit_imaging.py::test__simulate_imaging_data_and_fit__basis_galaxies__same_figure_of_merit_as_standard` | search/fit | |

**One test is 52 % of PyAutoGalaxy's whole suite.** `test_kaplinghat.py` accounts for
20.8 s of 32.1 s of measured local test time. This is the clearest single hot spot on
the board and it is not a test problem — it is the profile's numpy implementation
(§5.2). Note the second-order consequence: under `-n auto` the same suite still takes
28.8 s, because one 17.8 s test cannot be parallelised away.

### 2.4 PyAutoLens — local, serial (no CI rows yet), top 25

| local s | test | class |
|---:|---|---|
| 1.63 | `point/triangles/test_shape_solver.py::test_shapes_of_equal_area_give_the_same_images` | numerics (triangle solver) |
| 1.10 | `interferometer/model/test_analysis_interferometer.py::test__shared_state_from__preloads_curvature_reused__figure_of_merit_unchanged` | compile (JAX) + search/fit |
| 0.93 | `lens/test_mappings.py::test__multiple_image_positions__centroid_and_brightest_pixel_agree_roughly` | numerics |
| 0.90 | `point/triangles/test_shape_solver.py::test_intermediate_plane_differs_from_the_last_plane` | numerics |
| 0.88 | `point/triangles/test_solver_multi_plane.py::test__intermediate_plane__filter_is_what_used_to_reject_the_images` | numerics |
| 0.85 | `analysis/test_latent.py::test_delaunay_pixelized_source_magnification_is_finite` | numerics (inversion) |
| 0.83 | `point/triangles/test_solver_multi_plane.py::test__plane_redshift_none_matches_last_plane_redshift` | numerics |
| 0.82 | `lens/test_mappings.py::test__magnifications__pixelized_are_flux_shares_summing_to_one` | numerics (inversion) |
| 0.82 | `potential_correction/test_iterative_interferometer.py::test__solve_joint_optimization__identity_damping_finite` | search/fit |
| 0.81 | `lens/test_mappings.py::test__multiple_image_pixel_coordinates__are_inside_the_data` | numerics |
| 0.79 | `imaging/model/test_plotter_imaging.py::test__fit_imaging` | I/O |
| 0.69 | `point/triangles/test_shape_solver.py::test_total_magnification_converges_as_the_source_shrinks` | numerics |
| 0.61 | `potential_correction/test_iterative_interferometer.py::test__cost_identity_matches_direct_visibility_chi2` | numerics |
| 0.56 | `interferometer/model/test_plotter_interferometer.py::test__fit_interferometer` | I/O |
| 0.56 | `potential_correction/test_iterative_interferometer.py::test__log_evidence__finite_at_optimum` | search/fit |
| 0.55 | `lens/test_mappings.py::test__magnifications__parametric_matches_the_analytic_isothermal_sphere` | numerics |
| 0.54 | `imaging/plot/test_fit_imaging_plots.py::test__subplot_tracer_from_fit__two_plane_tracer__output_file_created` | I/O |
| 0.52 | `interferometer/plot/test_fit_interferometer_plots.py::test__subplot_fit` | I/O |
| 0.52 | `analysis/analysis/test_analysis_dataset.py::test__modify_before_fit__inversion_no_positions_likelihood__raises_exception` | other |
| 0.51 | `imaging/model/test_plotter_imaging.py::test__fit_imaging__quick_update__writes_normal_fit_subplot_only` | I/O |
| 0.48 | `potential_correction/test_iterative_interferometer.py::test__solve_joint_optimization__finite_state_and_decreasing_cost` | search/fit |
| 0.46 | `point/triangles/test_solver_multi_plane.py::test__intermediate_plane_source__images_are_found` | numerics |
| 0.44 | `lens/test_los.py::test__negative_kappa_from__loose_quad_matches_reference_and_is_negative` | numerics (`quad`) |
| 0.44 | `point/triangles/test_solver_multi_plane.py::test__solver_and_fit_measure_magnification_at_the_same_plane` | numerics |
| 0.43 | `point/triangles/test_solver_multi_plane.py::test__magnification_matches_ray_traced_jacobian` | numerics |

PyAutoLens has **no hot spot**. Its slowest test is 1.63 s and its top 5 % is only 58 %
of the suite. What it has instead is the stack's largest import (§3) and the widest
spread: the triangle point solver (5 rows) and the pixelized-magnification mapping path
(4 rows) are the only recognisable clusters, and neither is worth a refactor on these
numbers.

### 2.5 A finding that is not a timing: PyAutoFit cannot run under `-n auto`

`python3 -m pytest -n auto` in PyAutoFit fails collection outright:

```
ERROR gw1 - Different tests were collected between gw0 and gw1.
```

The cause is `test_autofit/mapper/prior/test_prior_properties.py:63-64`:

```python
def prior_id(prior):
    return f"{type(prior).__name__}({prior.message})"
```

used as `ids=prior_id` at lines 98, 109 and 127. For a `TransformedMessage` the
interpolated `{prior.message}` falls back to the default `object.__repr__`, so the test
id embeds a **memory address** — `UniformPrior(<autofit.messages.composed_transform.
TransformedMessage object at 0x7f5ed2bd2db0>)`. Every worker process gets different
addresses, xdist compares the collected id lists and refuses.

This is why the largest unit-test leg on the board (2446 tests, 157.5 s) is also the
only one that cannot be parallelised, and it is a four-line fix in a test file. It is
option **O5**.

## 3. Import-time composition, per package

`python3 -X importtime -c "import <pkg>"`, warm, three repeats, median of the
cumulative. Local; the CI column is the record's `import_s`, which is a whole fresh
process (interpreter start included) rather than the import alone.

| package | local `import` (cum) | local fresh process | CI `import_s` 3.12 / 3.13 | CI/local process |
|---|---:|---:|---:|---:|
| autonerves | 0.085 s | 0.178 s | 0.169 / 0.169 | 0.95x |
| autoarray | 0.134 s | 0.255 s | 0.442 / 0.418 | 1.7x |
| autofit | 0.329 s | 0.461 s | 0.892 / 1.249 | 1.9–2.7x |
| autogalaxy | 0.734 s | 0.882 s | *(no rows)* | — |
| autolens | 1.150 s | 1.393 s | *(no rows)* | — |

`import autolens` from a workspace root measures 1.28–1.31 s, matching the ~1.3 s the
issue quotes. For scale: `import autolens, jax, numba` in one process is 2.15–2.25 s,
and the phase-8 diagnosis measured the *whole* warm per-process script floor at
4.3–4.8 s locally and 5–7 s in CI. The import tree below is therefore roughly **a third
of the floor**, and it is the third that can be moved from a library.

**Where the 1.150 s of `import autolens` is**, by cumulative subtree (median of three):

| subtree | cum | note |
|---|---:|---|
| `autogalaxy` | 0.712 s | of which `autofit` 0.353 s, `autoarray` 0.052 s |
| `matplotlib.pyplot` | 0.239 s | **not** imported by `import autogalaxy` — §4.1 |
| `scipy.special` | 0.235 s | §4.2 |
| `scipy.integrate` | 0.178 s | pulled by `autogalaxy.profiles.mass.dark.kaplinghat` — §4.3 |
| `autoarray.plot` | 0.104 s | matplotlib core (not pyplot) |
| `scipy.optimize` | 0.093 s | inside `scipy.integrate` |
| `matplotlib` (core) | 0.069 s | |

Self-time top rows are the same story from the other side: in `import autolens` the
largest single self-times are `scipy.special._support_alternative_backends` 0.127 s,
`fontTools.agl` 0.045 s, `autoarray.plot` 0.032 s, `scipy.ndimage.
_support_alternative_backends` 0.026 s, `matplotlib.patches` 0.020 s — i.e. **the
plotting stack and scipy's array-API shims, not PyAuto code**. `import autoarray`
(0.134 s) pulls no matplotlib, no scipy, no numba and no jax at all; it is already lean
and is not a candidate for anything here.

## 4. Pulled in at import, not needed at import

Three, each with its trigger line found by an import hook, not by reading.

### 4.1 `matplotlib.pyplot` — 0.239 s per `import autolens` process

```
autolens/__init__.py:143        from . import potential_correction as pc
autolens/potential_correction/__init__.py:16   from autolens.potential_correction import visualize
autolens/potential_correction/visualize.py:19  import matplotlib.pyplot as plt
```

Measured 0.204 / 0.239 / 0.268 s across the three repeats. It is genuinely not needed
at import: **`import autogalaxy` and even `import autogalaxy.plot` leave
`matplotlib.pyplot` out of `sys.modules`** — the plot wrappers import it lazily, when a
figure is actually made. `autolens` is the only package in the stack that pays it
unconditionally, and it pays it for one visualization module inside
`potential_correction`.

*Honest bound:* the saving is only realised in a process that never renders a figure.
Every script that plots imports pyplot later anyway. It is realised in full by every
`jax_likelihood` / `jax_grad` pin script, every `should_simulate` simulator subprocess,
and every library unit-test worker.

### 4.2 `scipy.special` — 0.150 s per `import autofit`, 0.235 s per `import autolens`

There is **no top-level `import scipy` anywhere in autofit**. The trigger is a
module-level object construction that evaluates a transform:

```
autofit/messages/normal.py:678   UniformNormalMessage = TransformedMessage(NormalMessage(0, 1), phi_transform)
  -> autofit/messages/composed_transform.py:138   z0 = self._inverse_transform(np.array(x0))
  -> autofit/messages/transform.py:231            return self.inv_func(x, *self.args)
  -> autofit/messages/transform.py:354            from scipy.special import ndtr
```

Every deliberate deferral in that file is intact — `normal.py:140,160,435` all import
scipy inside functions. The import survives because a **module-level literal runs the
numerics at import time**. Roughly 0.13 s of the 0.15 s is scipy's own
`_support_alternative_backends` shim, i.e. the cost is scipy's, but the trigger is ours
and it is paid by every process that imports autofit — which is every process on the
board.

### 4.3 `scipy.integrate` (+`scipy.optimize`) — 0.178 s per `import autolens`

Pulled by `autogalaxy/profiles/mass/dark/kaplinghat.py:5`
(`from scipy.integrate import quad, solve_ivp`) at module import, i.e. by the *same
file* that owns the slowest test on the board (§2.3, §5.2). `scipy.integrate` drags
`scipy.optimize` (0.093 s) behind it through `scipy.integrate._bvp`.

### The multiplier: how many processes pay it

From `timings/scripts/*.jsonl`, entries per Python leg, in the `fast-tests` epoch:

| repo | entries / leg | imports |
|---|---:|---|
| HowToLens | 50 | autolens |
| autolens_workspace | 39 | autolens |
| autolens_workspace_test | 27 | autolens |
| autogalaxy_workspace_test | 39 | autogalaxy |
| HowToGalaxy | 32 | autogalaxy |
| autogalaxy_workspace | 16 | autogalaxy |
| autofit_workspace_test | 14 | autofit |
| HowToFit | 14 | autofit |
| autofit_workspace | 10 | autofit |
| autocti_workspace / _test | 3 + 3 | autoarray + autofit |
| **total** | **247** | |

A full board round runs both Python legs: **494 script processes**, of which **232**
import autolens and **174** import autogalaxy. Every one of the 494 imports autofit.
Each `should_simulate` subprocess is a further whole process on top (phase 8: ~4.5 s of
its ~5.2 s is its own import floor).

Per board round, at the measured local per-process savings and the ~1.8x CI/local
process ratio from the table in §3:

| deferral | per process | processes | local-equivalent | CI-scaled |
|---|---:|---:|---:|---:|
| pyplot out of `autolens` (§4.1) | 0.239 s | 232 | 55 s | ~100 s |
| `scipy.special` out of `import autofit` (§4.2) | 0.150 s | 494 | 74 s | ~133 s |
| `scipy.integrate` out of `kaplinghat` import (§4.3) | 0.178 s | 406 | 72 s | ~130 s |

These are **upper bounds under the stated caveat** in each subsection, and they do not
compose additively where the same process would import the module later anyway.

## 5. Shared hot spots the three sides agree on

The interesting rows are the ones the per-script CI timings, the phase-5/6/8 gate
diagnoses **and** this unit census all point at.

### 5.1 The interferometer NUFFT path is JAX even when the caller asked for NumPy

| side | evidence |
|---|---|
| unit census | the two slowest non-numba PyAutoArray tests are both this path: `test__nufft__chunk_size__visibilities_from_numpy_matches_unchunked` (CI 3.29 s; **3.49 s of it is `backend_compile_and_load`, 148 compiles**) and `test__interferometer_sparse_operator__func_list_and_x2_mappers__…` (CI 3.37 s; 3.51 s of it, 105 compiles) |
| phase 8 diagnosis | `compiler.py:backend_compile_and_load` 2.34–3.19 s on three interferometer smoke scripts (al_ws `interferometer/features/pixelization/delaunay.py`, `interferometer/modeling.py`, ag_ws `interferometer/start_here.py`) |
| phase 8c | `PYAUTO_DISABLE_JAX=1` now forces `use_jax=False` in `apply_sparse_operator`; the imaging sibling was backed off with reasons |
| source | `autoarray/operators/transformer.py:345 _forward_native` — the `xp=np` branch still calls `_nufftax.nufft2d2(...)`, and `nufftax` is a JAX library. There is no NumPy NUFFT. `PYAUTO_DISABLE_JAX` cannot help here, because there is nothing to route to |

This is the single path the three sides agree on most strongly, and phase 8c's
`disable_jax()` predicate explicitly did **not** reach it.

### 5.2 `KaplinghatCoredNFWSph` — a scalar `quad` over a Python callable

| side | evidence |
|---|---|
| unit census | 17.84 s local, **52 % of PyAutoGalaxy's whole measured test time**, from three grid points |
| profile | `kaplinghat.py:217 _density_3d_from_radius` × 2 186 457 calls; `numpy.interp` × 4 372 939; `scipy.integrate._qagse` × 4124 (nested: the potential's `quad` at `:378` integrates the mass `quad` at `:295` which integrates the density) |
| import census | the same file's `from scipy.integrate import quad, solve_ivp` costs 0.178 s at import in every autogalaxy/autolens process (§4.3) |
| CI scripts | no smoke entry fits this profile, so the CI script record is *silent* — this hot spot exists only in the unit suite and in any user run that uses the profile |

The Lane-Emden table is already `lru_cache`d (`kaplinghat.py:16`); the cost is that the
integrand is a scalar Python lambda, so the table is re-`np.interp`ed twice per
quadrature node.

### 5.3 Compilation is the largest single term in library CI, and only the smoke gate caches it

| side | evidence |
|---|---|
| unit census (CI) | ten PyAutoArray tests cost 24.8 s in CI and ≤ 0.02 s locally: numba compilation with no cache |
| unit census (local A/B) | with a persistent JAX compile cache the PyAutoArray suite goes **43 s cold → 21 s warm** (−51 %); the cache is 4.4 MB. PyAutoGalaxy 37 → 36 s, PyAutoLens 37 → 34 s, PyAutoFit 73 → 70 s |
| phase 7 / `smoke-tests.yml` | the smoke workflow **restores and saves** a JAX compile cache keyed on `(runner OS, python leg, jaxlib, epoch)` and a dataset cache, and `mkdir -p /tmp/numba_cache` so the many script subprocesses in one job share numba output |
| `lib-tests.yml` | has **no** JAX cache, **no** numba cache, and no `NUMBA_CACHE_DIR`. It sets `JAX_ENABLE_X64=True` and runs `pytest --cov <pkg> --cov-report xml --junitxml=…` serially |
| phase 8 diagnosis | cold numba was "the whole of the 86 s → 18 s step" on a pixelization script; the same class, on the other workflow |

### 5.4 `model.info` / `replace_promise` — agreed, and already fixed

Recorded here because it is the model case of the three sides agreeing. Phase 8 measured
`print(model.info)` at 0.5–1.3 s on nearly every modeling script and 6.0 s of 10.2 s on
one; the fix landed in phase 8c (`autofit/mapper/prior_model/recursion.py`, PyAutoFit
`e27eb8cb`), measured 10.58 → 6.73 s with byte-identical output. **The unit census now
confirms it from the third side**: `replace_promise` and `path_instances_of_class`
appear nowhere in PyAutoFit's CI top 25 on that head. Nothing further is proposed.

### 5.5 The `should_simulate` subprocess — agreed, and already fixed

Same pattern. Phase 8 measured 5.2–6.5 s per affected script and ~26 s / ~28 s per CI
run for al_ws / ag_ws; phase 8c's cap card fixed the three families
(`True → False`, 12.40 → 6.69 s, 10.48 → 6.32 s, 10.93 → 6.52 s). It is in the unit
census only as `test_autoarray/util/test_dataset_util.py` (0.46 s local, not in the top
25). Nothing further is proposed — except that the follow-up phase 8c already named
stands: PyAutoArray's `autonerves>=2026.8.23.1` floor predates `SMALLSHP`, so CI
resolving autonerves from PyPI silently un-fixes it until the release lands.

## 6. Ranked options

Every row is a proposal. **Impact** is per full board round (both Python legs) unless
stated; **validation** is the independent check beyond the unit suites; **guard** is the
standing rule the option must not cross.

| # | option | impact | risk | validation | guard |
|---|---|---|---|---|---|
| **O1** | **Give `lib-tests.yml` the caches `smoke-tests.yml` already has**: restore/save a JAX compile cache keyed `(OS, py, jaxlib, epoch)` and a numba cache keyed on the library commit. CI/workflow only. | **measured**: −22 s of 43 s on the PyAutoArray suite from the JAX cache alone (local A/B), and the ten cold-numba rows in §2.1 are **24.8 s of a 101 s CI leg**. Across 6 library repos × 2 legs this is the largest single number in this document. | **low** — the mechanism is already in production in `smoke-tests.yml`, including the key discipline (a jaxlib bump must miss). A stale cache costs a recompile, never a wrong answer. | the library suites themselves; compare `timings/unit/<repo>.jsonl` wall_s across the boundary | none crossed: this is *caches*, exactly what the compile-time verdict permits |
| **O2** | **Defer `scipy.special` out of `import autofit`**: make the module-level `TransformedMessage` literals at `autofit/messages/normal.py:678` lazy (a module `__getattr__` or a cached factory) so no transform is evaluated at import. | 0.150 s × 494 script processes ≈ 74 s local-equivalent (~133 s CI-scaled) per round, plus every unit-test process and every `should_simulate` subprocess | **medium** — module-level names in a public namespace; anything doing `from autofit.messages.normal import UniformNormalMessage` must keep working, and pickling/identifier hashing of those objects must be checked | PyAutoFit suite (2446), then `autolens_workspace_test` + `autogalaxy_workspace_test` smoke gates for the identifier/serialization surface | none |
| **O3** | **Defer `matplotlib.pyplot` out of `import autolens`**: import `autolens.potential_correction.visualize`'s pyplot inside its functions (or make `potential_correction/__init__.py:16` lazy), matching what `autogalaxy.plot` already does. | 0.239 s × 232 autolens processes ≈ 55 s local-equivalent (~100 s CI-scaled) per round, realised only in processes that never draw | **low** — one module, and the stack already demonstrates the pattern | PyAutoLens suite; `autolens_workspace` + HowToLens smoke gates (they *do* draw, so they prove nothing is broken) | none |
| **O4** | **Vectorise `KaplinghatCoredNFWSph`'s convergence/potential quadrature**: replace the scalar `quad`-over-a-lambda at `kaplinghat.py:255-264` / `:295` / `:378` with a fixed vectorised rule (or `quad_vec`) evaluating `_density_3d_from_radius` on an array. | **17.8 s of PyAutoGalaxy's 34.3 s local suite** is this one test; a vectorised integrand removes ~2.2 M scalar Python calls. Zero CI-script impact (no smoke entry uses the profile) — this is a unit-suite and user-runtime win | **medium** — it is a *numerical* change to a physics profile. The existing test asserts finiteness and positivity only, so it will not catch an accuracy regression on its own | the profile's own tests plus a new tolerance test against the current implementation's values; `autolens_profiling` for the runtime claim | numpy-mode numerics, not a likelihood or sampler restructure — clear of the compile-time verdict |
| **O5** | **Make PyAutoFit's parametrized ids deterministic** (`test_autofit/mapper/prior/test_prior_properties.py:63`), unblocking `pytest -n auto` for the largest suite on the board. | PyAutoFit CI 157.5 s serial; the other suites gain ~2x under `-n auto` locally (PyAutoArray 44.3 → 22.7 s). Needs O1 as well, or each worker re-pays every compile | **low** for the id fix (a test file); **medium** for turning `-n auto` on in CI, which changes the isolation the suite currently has | run the suite serially and under `-n auto` and diff the collected id list and the pass/fail set | none |
| **O6** | **Reconsider `--cov` on the library legs** — run coverage on one leg (3.12) rather than both, or drop it where Codecov is not read. | **measured locally, same pass counts both ways**: PyAutoArray 45 s → **60 s** (+33 %), PyAutoFit 71.5 s → **120 s** (+68 %) with `--cov <pkg> --cov-report xml`. On the CI legs that is roughly 25 s and 60 s per leg; dropping the duplicate leg across 6 repos is minutes per board round | **low** technically, but it is a **coverage-policy** decision, not a timing one — flagged for the human rather than recommended | none needed; it is a workflow flag | none |
| **O7** | **Give the interferometer NUFFT a NumPy path, or state that it has none.** §5.1: `transformer.py:345`'s `xp=np` branch calls a JAX library, so `PYAUTO_DISABLE_JAX=1` is honoured everywhere except the one place that compiles most. Either implement a NumPy NUFFT for small inputs, or document the asymmetry the way phase 8c documented the imaging one. | ~7 s of the PyAutoArray CI leg in two tests, 2.3–3.2 s per interferometer smoke script (phase 8) | **high** if a NumPy NUFFT is written (a new numerical implementation to certify against the JAX one); **zero** if the outcome is the documentation half | `misc/jax_assertions/fit_interferometer_sparse_operator.py` in both `_test` repos, plus the PyAutoArray parity tests that already exist | **borderline — read the guard first.** Adding a backend to a transform is not "restructuring a likelihood for compile time", but if the motivation is only the compile, the verdict says leave it. Recommended shape: the **documentation half only**, unless a NumPy path is wanted for its own sake |
| **O8** | **Reduce `test_scaler.py::test__scaling_cannot_move_the_MAP`'s 2 × 200 000 Python gradient steps** (`test_scaler.py:333-340`) to the smallest count that still separates the scaled and unscaled answers. | 5.9 s of the PyAutoFit CI leg, one test | **low**, but it weakens a deliberately end-to-end assertion; needs the author's judgement on how few steps still prove the claim | the test itself, run at the reduced count against the current tolerances | none |
| **O9** | **Ask whether the 7 `blackjax` sampler tests belong in the unit suite** (§2.2): they are **33.6 s of a 157.5 s leg**, they are real SMC/NUTS runs, and they are invisible to every local run because `blackjax` is not installed. Options are a shared session-scoped compiled kernel, `test_mode` settings, or a marked slow lane. | 33.6 s per PyAutoFit CI leg (21 %) | **medium** — these tests guard a sampler's correctness; moving them out of the default lane trades CI time for coverage-at-PR-time | the tests themselves at the reduced settings; the sampler benchmark record | **settings and caches only.** Do **not** restructure the SMC/NUTS kernels or their likelihoods for compile time — that is exactly what the closed arc forbids. A session fixture that compiles once, or a smaller step count, is a setting |

**Ordering.** O1 first: it is the largest measured number, it is the lowest risk, the
mechanism already exists one workflow over, and it changes no library source. O2 and O3
next: both are import deferrals with a measured per-process number and a large
multiplier. O4 is the largest *single-test* win on the board but is the first one that
touches numerics. O5–O9 are smaller or need a policy answer first.

## 7. Explicitly excluded

Named so nobody re-derives them.

- **Anything that restructures a likelihood or a sampler to reduce JAX compile time.**
  The jax-compile-time arc is closed: settings and caches only. That rules out
  re-shaping `Analysis.log_likelihood_function`, the `FactorGraphModel` vmap structure,
  the blackjax kernels, or any "make it trace faster" rewrite. O1 and O9 stay inside the
  verdict because a cache and a step count are settings.
- **Anything whose cost is upstream JAX/XLA.** `backend_compile_and_load` is jaxlib's;
  we may cache its output (O1) and we may stop calling it (O7's documentation half), but
  we do not chase it.
- **Adding JAX to unit tests, anywhere.** The standing `no JAX in unit tests` rule
  holds. Note what the census actually found: the rule is **already breached in
  PyAutoArray by the library, not by the tests** — `test__nufft__chunk_size__…_numpy_…`
  is a NumPy test that compiles JAX because `transformer.py:345` has no NumPy path
  (§5.1). The breach is a source fact, and O7 is the only place it is even discussed.
- **Restructuring `autoarray`'s import tree.** `import autoarray` is 0.134 s and pulls
  no matplotlib, scipy, numba or jax. There is nothing there.
- **`model.info` / `replace_promise`, and the `should_simulate` subprocess.** Both were
  the epic's largest systemic findings and both shipped in phase 8c. Confirmed fixed
  from the third side in §5.4 / §5.5. Not re-proposed.
- **The per-process import floor as a whole.** 4.3–4.8 s warm locally, 5–7 s in CI, of
  which `import autolens` is ~1.3 s. Options O2/O3 move a third of the import; the rest
  is the interpreter, the config layer, numba and jax, and no workspace or library
  change reaches it. Phase 8's verdict — five of its fourteen slow scripts are import
  floor and "cannot be improved from a workspace repo" — stands.

## 8. Gaps in this round

Stated rather than papered over.

1. **PyAutoGalaxy and PyAutoLens have no CI unit rows.** Their tables (§2.3, §2.4) are
   local. On the PyAutoArray evidence the CI numbers will be ~2.3x larger *and*
   differently ordered — cold numba promotes rows that read as free locally. The
   kaplinghat finding is robust to that (it is pure Python/numpy, not a compile); the
   PyAutoLens ordering is not, and should be re-read once its first
   `unit-timings-<py>` artifact is ingested.
2. **`blackjax` is not installed in this container**, so 25 PyAutoFit tests never ran
   locally and their local column is `—`. The CI record covers them.
3. **`--durations` under `-n auto` is not comparable to serial durations.** Every
   per-test number in §2 is from a serial run for exactly this reason: under 4-way
   xdist the same PyAutoLens `potential_correction` tests read 6.8 s (first-on-worker,
   paying that worker's import and compile) against ~0.5 s serial. The suite *wall*
   times under `-n auto` in §1 are real; the per-test rows would not have been.
4. **The CI/local ratio is measured only for autoarray, autofit and autonerves**
   (1.7x, 1.9–2.7x, 0.95x). The ~1.8x used to scale the §4 savings is that range, not a
   measurement of the autolens legs.
5. **No option here has been implemented or trialled end-to-end.** The JAX-cache number
   (O1) is a local A/B on this container, not a CI run; the import-deferral numbers are
   per-process measurements multiplied by a process count from `timings/scripts/*.jsonl`,
   not an observed board round.
