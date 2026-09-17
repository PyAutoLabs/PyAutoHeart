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


def classify_unpinned(output, package="autolens"):
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; pip_output=$(cat); '
            'verify_install_unpinned_refusal "$pip_output" "$2"',
            "classifier",
            str(HELPERS),
            package,
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


def test_help_lists_all_checks_including_the_colab_gate():
    result = run("--help")
    assert result.returncode == 0
    for letter in "ABCDEF":
        assert f"\n  {letter}   " in result.stdout, f"check {letter} missing from help"
    assert "Colab gate" in result.stdout
    assert "googlecolab/backend-info" in result.stdout


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


def test_help_describes_supported_success_and_below_floor_rejection():
    result = run("--help")

    assert result.returncode == 0
    assert "installs on python3.12 and python3.13" in result.stdout
    assert "pinned (Requires-Python >=3.12)" in result.stdout
    # The unpinned leg has to be advertised too, or the help understates what
    # Check B now guarantees.
    assert "unpinned" in result.stdout
    assert "2026.7.29.1.post1 tombstone" in result.stdout
    assert "applies to A, B, C, D" in result.stdout


def test_unpinned_classifier_accepts_the_tombstone_and_retraction_forms():
    """The two shapes that mean "refused below the floor"."""
    tombstone_output = (
        "        autolens requires Python 3.12 or later — you are running "
        "Python 3.11.\n"
        "ERROR: Failed to build 'autolens' when getting requirements to "
        "build wheel\n"
    )
    retraction_output = (
        "ERROR: Ignored the following versions that require a different "
        "python version: 2026.8.17.1 Requires-Python >=3.12\n"
        "ERROR: No matching distribution found for autolens\n"
    )

    assert classify_unpinned(tombstone_output).returncode == 0
    assert classify_unpinned(retraction_output).returncode == 0


def test_unpinned_classifier_rejects_unrelated_pip_failures():
    """A refusal for any other reason is not floor evidence."""
    network_failure = (
        "WARNING: Retrying after connection broken by NewConnectionError\n"
        "ERROR: No matching distribution found for autolens\n"
    )
    dependency_conflict = (
        "ERROR: Cannot install autolens because these package versions have "
        "conflicting dependencies.\n"
    )
    other_package_tombstone = (
        "        autofit requires Python 3.12 or later — you are running "
        "Python 3.11.\n"
    )

    for output in (network_failure, dependency_conflict, other_package_tombstone):
        assert classify_unpinned(output).returncode != 0


def test_check_b_asserts_the_unpinned_install_is_refused():
    """The gap Check B documented but never tested: until 2026-08-19 an
    unpinned 3.11 install silently resolved to the stale 2026.7.29.1 stack."""
    body = SCRIPT.read_text()

    assert "check_b_unpinned_refused" in body
    # Wired into the runner, not merely defined.
    assert body.count("check_b_unpinned_refused") >= 2
    assert "verify_install_unpinned_refusal" in body
    # A successful unpinned install below the floor is the bug returning.
    assert "the sub-floor backtrack is back" in body


# ----- check F: the Colab package-set gate -----------------------------------


def check_f_body():
    text = SCRIPT.read_text()
    return text[text.index("check_f() {") : text.index("# ----- runner -----")]


def test_check_f_uses_python_312_because_colab_does():
    body = check_f_body()

    assert 'if ! command -v python3.12 > /dev/null 2>&1; then' in body
    assert 'RESULTS+=("F|FAIL|python3.12 not found")' in body
    assert 'if ! make_venv "$venv" python3.12; then' in body
    # The old default-python venv is gone: seeding Colab's pins against a
    # different interpreter would resolve the wrong wheels.
    assert 'make_venv "$venv" python3;' not in body


def test_check_f_no_longer_installs_the_stack_with_dependencies():
    """The bug this check existed to hide.

    `pip install <stack> jax` WITH deps left corner/optax/xxhash/blackjax in
    the venv before the setup cell ran, so the cell's real `--no-deps` install
    could never be seen to miss one.
    """
    body = check_f_body()

    assert 'pip install "${PIP_INDEX_ARGS[@]}" "${f_targets[@]}" jax' not in body
    assert '"${f_targets[@]}" jax' not in body
    assert "emulating Colab's preinstalled env" not in body


def test_check_f_seeds_and_verifies_through_colab_gate():
    body = check_f_body()

    assert '"$VERIFY_INSTALL_DIR/colab_gate.py" seed' in body
    assert '"$VERIFY_INSTALL_DIR/colab_gate.py" verify' in body
    # Run with the simulated venv's interpreter, or importlib.metadata and the
    # import probe would see the host environment instead. Three invocations:
    # seed, the advisory released audit (rehearsal only), and the gated audit
    # the continuous and candidate paths share.
    assert body.count('"$venv/bin/python" "$VERIFY_INSTALL_DIR/colab_gate.py"') == 3
    assert '--manifest-cache "$COLAB_MANIFEST_CACHE"' in body
    assert (ROOT / "heart/checks/colab_gate.py").is_file()
    assert (ROOT / "heart/checks/colab_pip_freeze.snapshot.txt").is_file()


def test_check_f_runs_the_gate_between_the_setup_cell_and_the_notebook_cell():
    body = check_f_body()

    setup_cell = body.index("F_driver_setup.py")
    gate = body.index('colab_gate.py" verify')
    notebook_cell = body.index("F_driver_cell.py")

    assert setup_cell < gate < notebook_cell
    # The setup cell is still the injected cell verbatim, and the notebook cell
    # still loads the bundled dataset.
    assert "import google.colab" in body
    assert 'pip", "install", "autonerves", "--no-deps"' in body
    assert "al.Imaging.from_fits" in body
    assert "dataset/imaging/cosmos_web_ring/data.fits" in body


def test_check_f_keeps_the_fake_google_colab_stub():
    body = check_f_body()

    assert '"$site/google/colab/__init__.py"' in body
    assert '"$site/google/colab/output.py"' in body


def test_check_f_preserves_the_skip_exit_code():
    body = check_f_body()

    assert "sys.exit(3)" in body
    assert 'if [ "$setup_rc" -eq 3 ]; then' in body
    assert 'RESULTS+=("F|SKIP|installed autonerves predates setup_colab registry' in body


def test_autonerves_source_override_is_wired_and_documented():
    text = SCRIPT.read_text()
    body = check_f_body()
    help_result = run("--help")

    # Read on both paths: inside the re-pin driver (rehearsal — it has to sit
    # between the candidate pins and the setup_colab reload) and in the shared
    # f_overlay_autonerves_src step the continuous path runs in front of its
    # single audit.
    assert 'os.environ.get("COLAB_GATE_AUTONERVES_SRC")' in body
    assert "f_overlay_autonerves_src() {" in text
    assert 'os.environ["COLAB_GATE_AUTONERVES_SRC"]' in text
    assert text.count('"--no-deps", _autonerves_src') == 2
    assert "    f_overlay_autonerves_src\n" in body
    # It is no longer laid over the verbatim setup cell's own bootstrap, so
    # what that cell installs stays measurable on its own.
    setup_driver = body[body.index("F_driver_setup.py") : body.index("setup_rc=0")]
    assert '"--no-deps", _autonerves_src' not in setup_driver
    assert 'os.environ.get("COLAB_GATE_AUTONERVES_SRC")' not in setup_driver
    assert "COLAB_GATE_AUTONERVES_SRC" in help_result.stdout
    assert "COLAB_GATE_AUTONERVES_SRC" in (
        ROOT / "skills/verify_install/verify_install.md"
    ).read_text()


def test_help_documents_the_rehearsal_re_pin_and_warn_row():
    """A reader of --help must learn that a rehearsal gates on the candidate."""
    result = run("--help")

    assert result.returncode == 0
    assert "WARN" in result.stdout
    assert "re-pin" in result.stdout


def test_check_f_rehearsal_re_pins_to_the_candidate_and_audits_both_facets():
    """The chicken-and-egg fix (2026-09-17).

    The injected setup cell is verbatim and therefore unpinned, so in a
    rehearsal it bootstraps the RELEASED stack. Check F audits that advisorily,
    re-pins to the candidate, and gates on the candidate.
    """
    body = check_f_body()

    # The advisory facet exists, writes its own report, and is rehearsal-only.
    driver = body.index("F_driver_setup.py")
    guard = body.index('if [ -n "$TARGET_VERSION" ]; then', driver)
    released = body.index("$verify_released_json", guard)
    assert driver < guard < released
    assert 'local verify_released_json="/tmp/F_gate_verify_released_$TS.json"' in body
    assert 'F_GATE_VERIFY_RELEASED_JSON="$verify_released_json"' in body
    assert '--report-json "$verify_released_json"' in body

    # The re-pin: all five PyAuto packages at the candidate, then the
    # candidate's own bootstrap package list, mirroring _colab_setup.
    for package in ("autonerves", "autofit", "autoarray", "autogalaxy", "autolens"):
        assert f'f"{package}=={{version}}"' in body
    assert '"pip", "install", "--no-deps", *index_args, *pins' in body
    assert "importlib.reload(sc)" in body
    assert 'sc._PROJECTS["autolens"]["packages"]' in body
    assert '"pip", "install", *packages, "--no-deps"' in body
    assert "re-pin OK: candidate " in body
    # setup() is NOT called again — the workspace is cloned and cwd has moved.
    assert body.count("_setup_colab.setup(") == 1
    assert 'RESULTS+=("F|FAIL|candidate re-pin rc=$repin_rc")' in body

    # The released facet reports a WARN row, never a FAIL.
    assert 'RESULTS+=("F|WARN|released Colab bootstrap ' in body
    assert "F|FAIL|released" not in body


def test_check_f_continuous_path_runs_one_verify():
    """No --version: the released bootstrap IS the candidate — one audit."""
    body = check_f_body()

    # One gated audit, shared by the continuous and candidate paths, plus the
    # rehearsal-only advisory one.
    assert body.count('colab_gate.py" verify') == 2
    assert body.count('--report-json "$verify_json"') == 1
    assert body.count('--report-json "$verify_released_json"') == 1
    # The continuous branch is the `else` of the rehearsal guard, and applies
    # the dev/witness overlay in front of its single audit.
    guard = body.index('if [ -n "$TARGET_VERSION" ]; then', body.index("F_driver_setup.py"))
    otherwise = body.index("    else", guard)
    gate = body.index('--report-json "$verify_json"', otherwise)
    assert guard < body.index("f_overlay_autonerves_src\n", otherwise) < gate
    # Today's FAIL semantics are untouched.
    assert 'RESULTS+=("F|FAIL|colab gate: verify could not run (rc=$gate_rc)")' in body
    assert 'RESULTS+=("F|FAIL|$gate_detail")' in body
    assert 'RESULTS+=("F|PASS|$gate_detail")' in body


def test_warn_rows_are_counted_but_never_fail_the_run():
    text = SCRIPT.read_text()

    assert 'n_warn=$((n_warn + 1))' in text
    assert '[ "$status" = "WARN" ] && n_warn=$((n_warn + 1))' in text
    assert 'echo "Overall: PASS ($n_skip skipped, $n_warn warning(s))"' in text
    assert (
        'echo "Overall: FAIL ($n_fail failure(s), $n_skip skipped, '
        '$n_warn warning(s))"' in text
    )
    # ready is n_fail-driven, so a WARN row leaves it true.
    assert 'if [ "$n_fail" -eq 0 ]; then ready_bool=true; else ready_bool=false; fi' in text


def test_sidecar_nests_the_released_gate_report():
    text = SCRIPT.read_text()

    assert 'VI_F_GATE_VERIFY_RELEASED="$F_GATE_VERIFY_RELEASED_JSON"' in text
    assert '"verify_released"' in text
    assert '_read("VI_F_GATE_VERIFY_RELEASED")' in text
    # Declared globally, so the writer is safe when check F never ran.
    assert 'F_GATE_VERIFY_RELEASED_JSON=""' in text


def test_sidecar_nests_the_gate_report_under_check_f_without_changing_its_shape():
    text = SCRIPT.read_text()

    assert 'VI_F_GATE_SEED="$F_GATE_SEED_JSON"' in text
    assert 'VI_F_GATE_VERIFY="$F_GATE_VERIFY_JSON"' in text
    assert 'entry["colab_gate"] = gate' in text
    # The keys readiness.py and validate.py parse are untouched.
    for key in ('"check": parts[0]', '"status": parts[1]', '"detail": parts[2]'):
        assert key in text


def sidecar_writer_source():
    """The `python3 -c '...'` sidecar writer, lifted out of the script.

    Running the real writer (rather than hand-building a fixture) is the only
    way to prove the shape readiness.py and validate.py consume is unchanged.
    """
    text = SCRIPT.read_text()
    start = text.index("      python3 -c '") + len("      python3 -c '")
    end = text.index("\n'\n", start)
    return text[start:end]


def test_sidecar_still_parses_through_readiness_with_the_gate_report(tmp_path):
    import json
    import os

    from heart import readiness

    seed = tmp_path / "seed.json"
    verify = tmp_path / "verify.json"
    seed.write_text(json.dumps({"phase": "seed", "manifest_source": "live"}))
    verify.write_text(json.dumps({
        "phase": "verify",
        "ok": True,
        "detail": "Colab manifest live 2026-09-15; 61 Colab-provided, 9 extras, 74 imports probed",
        "fails": [],
        "warns": ["xxhash 4.0.1 outside <=3.4.1 required by autofit"],
    }))
    out = tmp_path / "verify_install.json"

    env = dict(os.environ)
    env.update({
        "VI_READY": "true",
        "VI_VERSION": "2026.9.1.1",
        "VI_CHECK_B_VERSION": "2026.9.1.1",
        "VI_REPORT_JSON": str(out),
        "VI_INDEX": "testpypi",
        "VI_F_GATE_SEED": str(seed),
        "VI_F_GATE_VERIFY": str(verify),
    })
    rows = "A|PASS|pip install\nF|PASS|Colab manifest live 2026-09-15\n"
    result = subprocess.run(
        ["python3", "-c", sidecar_writer_source()],
        input=rows, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr

    sidecar = json.loads(out.read_text())

    # Every key the consumers read is still there, in the same shape.
    assert set(sidecar) >= {"ts", "ready", "version", "check_b_version", "index", "checks"}
    assert sidecar["ready"] is True
    assert sidecar["index"] == "testpypi"
    assert [c["check"] for c in sidecar["checks"]] == ["A", "F"]
    assert sidecar["checks"][0] == {"check": "A", "status": "PASS", "detail": "pip install"}

    # The gate report is nested on F's row only, and is additive.
    f_row = sidecar["checks"][1]
    assert f_row["check"] == "F" and f_row["status"] == "PASS"
    assert f_row["colab_gate"]["seed"]["manifest_source"] == "live"
    assert f_row["colab_gate"]["verify"]["ok"] is True
    assert "colab_gate" not in sidecar["checks"][0]

    # readiness's own FAIL-extraction expression, run against the new shape.
    failed = [
        str(c.get("check"))
        for c in sidecar["checks"]
        if isinstance(c, dict) and str(c.get("status")).upper() == "FAIL"
    ]
    assert failed == []

    # And a FAILing F row still reaches readiness as a RED reason naming F.
    env["VI_READY"] = "false"
    rows_fail = "A|PASS|ok\nF|FAIL|colab gate: corner (autofit/plot.py:95)\n"
    subprocess.run(
        ["python3", "-c", sidecar_writer_source()],
        input=rows_fail, capture_output=True, text=True, env=env, check=True,
    )
    red_sidecar = json.loads(out.read_text())
    snapshot = {
        "ts": "2026-09-15T00:00:00+00:00",
        "verify_install": red_sidecar,
    }
    result = readiness.compute(snapshot)
    assert result["verdict"] == "red"
    assert any(
        "install verification FAILED" in reason and "F" in reason
        for reason in result["red_reasons"]
    )


def test_sidecar_warn_row_keeps_ready_true_and_readiness_not_red(tmp_path):
    """A rehearsal's two F rows: the advisory WARN, then the gated PASS.

    The WARN row must travel end to end — writer, sidecar, readiness — without
    moving `ready` or the verdict (decision 2026-09-17).
    """
    import json
    import os

    from heart import readiness

    seed = tmp_path / "seed.json"
    verify = tmp_path / "verify.json"
    released = tmp_path / "verify_released.json"
    seed.write_text(json.dumps({"phase": "seed", "manifest_source": "live"}))
    verify.write_text(json.dumps({
        "phase": "verify",
        "ok": True,
        "detail": "Colab manifest live 2026-09-17; 61 Colab-provided, 9 extras",
        "packages": {"autonerves": "2026.9.17.1.dev1"},
    }))
    released.write_text(json.dumps({
        "phase": "verify",
        "ok": False,
        "detail": "colab gate: corner (autofit/plot.py:95)",
        "packages": {"autonerves": "2026.9.15.1"},
    }))
    out = tmp_path / "verify_install.json"

    env = dict(os.environ)
    env.update({
        "VI_READY": "true",
        "VI_VERSION": "2026.9.17.1.dev1",
        "VI_CHECK_B_VERSION": "2026.9.17.1.dev1",
        "VI_REPORT_JSON": str(out),
        "VI_INDEX": "testpypi",
        "VI_F_GATE_SEED": str(seed),
        "VI_F_GATE_VERIFY": str(verify),
        "VI_F_GATE_VERIFY_RELEASED": str(released),
    })
    rows = (
        "A|PASS|ok\n"
        "F|WARN|released Colab bootstrap (autonerves=2026.9.15.1) broken for "
        "readers: colab gate: corner (autofit/plot.py:95); candidate "
        "2026.9.17.1.dev1 passes\n"
        "F|PASS|Colab manifest live 2026-09-17\n"
    )
    result = subprocess.run(
        ["python3", "-c", sidecar_writer_source()],
        input=rows, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr

    sidecar = json.loads(out.read_text())
    assert [c["status"] for c in sidecar["checks"]] == ["PASS", "WARN", "PASS"]
    assert sidecar["ready"] is True

    # The gate report rides on both F rows, both facets present.
    for row in sidecar["checks"][1:]:
        assert row["check"] == "F"
        assert row["colab_gate"]["verify"]["ok"] is True
        assert row["colab_gate"]["verify_released"]["ok"] is False
        assert row["colab_gate"]["verify_released"]["packages"]["autonerves"] == (
            "2026.9.15.1"
        )
    assert "colab_gate" not in sidecar["checks"][0]

    # And the verdict is untouched by the WARN row.
    verdict = readiness.compute({"ts": sidecar["ts"], "verify_install": sidecar})
    assert not any("install" in reason for reason in verdict["red_reasons"])
    assert not any("install" in reason for reason in verdict["yellow_reasons"])
