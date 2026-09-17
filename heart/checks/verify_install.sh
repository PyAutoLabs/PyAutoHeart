#!/usr/bin/env bash
# heart/checks/verify_install.sh — deep install-readiness check for PyAutoLens.
#
# Owned by PyAutoHeart (moved from PyAutoHands): all release-readiness checking
# is Heart's job. This is a *deep, on-demand* check — it creates throwaway venvs
# / conda envs and installs from PyPI, so it takes minutes and must NEVER run in
# the <30s `tick` loop. Invoke it via `pyauto-heart verify_install`, which passes
# `--report-json $HEART_STATE_DIR/verify_install.json`; readiness then consumes
# that sidecar (fail -> RED, stale/missing -> YELLOW).
#
# Runs a suite of independent install-path checks:
#
#   A  pip install autolens (default Python) + start_here.py + welcome.py
#   B  one exact autolens release installs on 3.12/3.13, and 3.11 refuses it
#      both pinned (Requires-Python) and unpinned (the tombstone release)
#   C  conda install flow (python=3.12) + start_here.py + welcome.py
#   D  pip install "autolens[optional]" resolves
#   E  pip install autolens==2026.2.26.4 on Python 3.12 by explicit pin
#   F  Colab gate: a venv holding Google's Colab package set, the injected
#      setup cell verbatim (--no-deps bootstrap + workspace clone), an audit
#      of every import/declared dependency the bootstrap left unmet, then a cell
#
# Each check creates its own throwaway venv / conda env and reports
# PASS / FAIL / SKIP. A failed check never aborts the suite. Cleanup runs at
# the end (use --keep to retain artefacts for inspection).
#
# Usage:
#   verify_install                       # run all checks
#   verify_install A                     # run a single check
#   verify_install A C E                 # run a subset
#   verify_install --version 2026.4.5.2  # pin a specific PyPI version (A/B/C/D)
#   verify_install --testpypi            # install from test.pypi.org (pre-release rehearsal)
#   verify_install --find-links dist/    # prefer exact locally-built wheels
#   verify_install --keep                # don't clean up at the end
#   verify_install -h | --help

set -uo pipefail   # NB: no -e — checks must continue past expected failures.

VERIFY_INSTALL_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=verify_install_helpers.sh
source "$VERIFY_INSTALL_DIR/verify_install_helpers.sh"

# Unset PYTHONPATH so checks aren't shadowed by user-side editable installs.
# When the calling shell exports PYTHONPATH=/path/to/PyAutoNerves:/path/to/...
# (a common setup when developing the libs), throwaway venvs created here
# will still resolve `import autolens` etc. from those local source dirs
# rather than from the just-installed PyPI artefacts. That defeats the
# purpose of testing the install path. Clearing PYTHONPATH for the script
# (and every subshell it spawns) keeps each venv truly isolated.
unset PYTHONPATH

# ----- usage -----

usage() {
    cat <<'USAGE'
verify_install — release-readiness gate for PyAutoLens.

Usage:
  verify_install [CHECKS...] [--version VERSION] [--testpypi]
                 [--find-links DIR] [--keep] [-h]

Checks:
  A   pip install autolens (default python3) + start_here.py + welcome.py
  B   one exact autolens release installs on python3.12 and python3.13, while
      python3.11 refuses it both pinned (Requires-Python >=3.12) and unpinned
      (the 2026.7.29.1.post1 tombstone, so pip cannot backtrack to a stale one)
  C   conda install flow (python=3.12) + start_here.py + welcome.py
  D   pip install "autolens[optional]" resolves and imports
  E   pip install autolens==2026.2.26.4 (yanked) installs on python3.12 by explicit pin
  F   Colab gate: build a python3.12 venv holding the package set Google's
      Colab ships (googlecolab/backend-info), run the injected setup cell
      verbatim (--no-deps bootstrap + workspace clone), then audit every
      third-party import and declared dependency the bootstrap left unmet
      before running a real notebook cell

Default: run all checks.

Options:
  --version VERSION   Pin a specific PyPI version (applies to A, B, C, D).
  --testpypi          Install from test.pypi.org with PyPI as a fallback for
                      non-PyAuto deps (applies to A, B, C, D, E). Use this for a
                      pre-release rehearsal against a TestPyPI dry-run upload.
  --find-links DIR    Prefer wheel artifacts in DIR while retaining the selected
                      index for third-party dependencies. Evidence is labelled
                      find-links, never PyPI/TestPyPI. Useful before publication.
  --keep              Don't clean up venvs / conda envs / clones at the end.
  --report-json PATH  Also write a machine-readable {ts,ready,version,
                      check_b_version,index,checks} JSON sidecar to PATH
                      (consumed by pyauto-heart readiness; `index` is pypi,
                      testpypi, or find-links).
  -h, --help          Show this help.

Check F in a rehearsal (--version):
  An unpinned `pip install autonerves` can never select a dev pre-release, so
  the injected setup cell necessarily bootstraps the RELEASED stack. Check F
  therefore audits the released bootstrap first and reports it as an advisory
  WARN row, which never changes `ready` and never fails the run; it then
  re-pins the venv to the candidate (all five PyAuto packages at VERSION from
  the rehearsal index, --no-deps, setup_colab reloaded and its own package list
  reinstalled) and gates on that. A continuous run (no --version) is unchanged:
  one audit, one verdict.

Environment:
  COLAB_GATE_AUTONERVES_SRC
                      Check F only, dev/witness use. A path or requirement
                      installed --no-deps over the released `autonerves`, so an
                      UNRELEASED autonerves/setup_colab.py package list can be
                      rehearsed against the gate. It is applied after the
                      candidate re-pin in a --version rehearsal, and before the
                      single audit in a continuous run. Never set this in CI: a
                      release gate must read the wheels about to ship.
USAGE
}

# ----- argument parsing -----

TARGET_VERSION=""
KEEP=0
USE_TESTPYPI=0
FIND_LINKS=""
REPORT_JSON=""
REQUESTED_CHECKS=()

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --version)
            if [ $# -lt 2 ]; then
                echo "verify_install: --version requires a value" >&2
                exit 2
            fi
            TARGET_VERSION="$2"
            shift 2
            ;;
        --testpypi)
            USE_TESTPYPI=1
            shift
            ;;
        --find-links)
            if [ $# -lt 2 ]; then
                echo "verify_install: --find-links requires a directory" >&2
                exit 2
            fi
            FIND_LINKS="$2"
            shift 2
            ;;
        --keep)
            KEEP=1
            shift
            ;;
        --report-json)
            if [ $# -lt 2 ]; then
                echo "verify_install: --report-json requires a path" >&2
                exit 2
            fi
            REPORT_JSON="$2"
            shift 2
            ;;
        A|B|C|D|E|F|all)
            REQUESTED_CHECKS+=("$1")
            shift
            ;;
        *)
            echo "verify_install: unknown argument '$1'" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -n "$FIND_LINKS" ]; then
    if [ ! -d "$FIND_LINKS" ]; then
        echo "verify_install: --find-links is not a directory: $FIND_LINKS" >&2
        exit 2
    fi
    FIND_LINKS=$(cd "$FIND_LINKS" && pwd)
fi

# When --testpypi is set, route every PyAuto-package pip install through
# TestPyPI as the primary index and fall back to PyPI for transitive deps not
# mirrored there (matplotlib, scipy, nufftax, jax, etc.). Cleared otherwise —
# keeps PyPI as the sole source for the default release-gate path.
#
# Stored as an array so the two flags are passed cleanly without word-split
# surprises at every `pip install` site.
#
# `--pre` is required, not cosmetic. A rehearsal build is a pre-release
# (2026.8.3.1.devNNNNN). pip accepts a pre-release for a package that is pinned
# to one explicitly — which `autolens[optional]==$TARGET_VERSION` is — but
# refuses to consider pre-releases for its *dependencies* unless --pre is set.
# So without it, only `autolens` came from the candidate build and every
# intra-family dependency resolved to the last STABLE PyPI release instead.
#
# That is not a weaker test, it is a test of the wrong artifact: the resolution
# then runs against already-published metadata, which is frozen and cannot
# contain any dependency floor the candidate adds. Check D failed for exactly
# this reason on 2026-08-03 — autolens 2026.8.3.1.dev70001 pulled the stable
# autogalaxy 2026.8.2.1, whose bare `autofit` requirement let pip backtrack to
# autofit 2026.4.30.582 (April) and `import autolens` died on
# `module 'autofit' has no attribute 'Latent'`. The floors added in
# PyAutoLens#687 were present and correct in source, but no rehearsal could
# ever exercise them — a fix that only takes effect once published, gated on a
# check that can only pass once it has taken effect.
#
# With --pre the whole family is eligible at the candidate version, so the
# intra-family chain resolves against the metadata about to ship. Scoped to the
# TestPyPI path only: the default release-gate path against PyPI stays
# stable-only, which is what a user actually gets.
PIP_INDEX_ARGS=()
if [ "$USE_TESTPYPI" -eq 1 ]; then
    PIP_INDEX_ARGS=(--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ --pre)
fi
if [ -n "$FIND_LINKS" ]; then
    PIP_INDEX_ARGS+=(--find-links "$FIND_LINKS")
fi

if [ ${#REQUESTED_CHECKS[@]} -eq 0 ]; then
    REQUESTED_CHECKS=(all)
fi

# Expand "all" into A B C D E.
SELECTED=()
for c in "${REQUESTED_CHECKS[@]}"; do
    if [ "$c" = "all" ]; then
        SELECTED=(A B C D E F)
        break
    fi
    SELECTED+=("$c")
done

# ----- shared state -----

TS=$(date +%Y%m%d_%H%M%S)
RESULTS=()       # each row "LETTER|STATUS|DETAIL"
RESULTS_LOG=""   # captured tail appended below the table on FAIL
ARTEFACTS=()     # paths to rm -rf at end
CONDA_ENVS=()    # conda env names to remove at end

# Check F caches Google's Colab pip-freeze manifest next to Heart's other state
# (same default as heart/state.py) so an offline/rate-limited run degrades to
# the last real manifest rather than to the snapshot vendored in this repo.
HEART_STATE_DIR="${HEART_STATE_DIR:-$HOME/.pyauto-heart}"
COLAB_MANIFEST_CACHE="$HEART_STATE_DIR/colab_pip_freeze.txt"
F_GATE_SEED_JSON=""      # colab_gate seed report, folded into the sidecar
F_GATE_VERIFY_JSON=""    # colab_gate verify report (the gated facet)
# The advisory RELEASED-bootstrap report, written only on the rehearsal path.
# Declared here so the sidecar writer can reference it even when check F never
# ran (the writer skips a path that does not exist).
F_GATE_VERIFY_RELEASED_JSON=""

PIP_INSTALL_TARGET="autolens"
PIP_INSTALL_OPTIONAL="autolens[optional]"
if [ -n "$TARGET_VERSION" ]; then
    PIP_INSTALL_TARGET="autolens==$TARGET_VERSION"
    PIP_INSTALL_OPTIONAL="autolens[optional]==$TARGET_VERSION"
fi

# ----- helpers -----

# step <message>: print an indented sub-step header with elapsed time.
step() {
    local now
    now=$(date +%H:%M:%S)
    printf '  [%s] -> %s\n' "$now" "$1"
}

# tail_log <header> <text>: append a labelled output tail to RESULTS_LOG.
tail_log() {
    local header="$1"
    local text="$2"
    RESULTS_LOG+=$'\n--- '"$header"$' ---\n'"$(printf '%s' "$text" | tail -40)"$'\n'
}

# make_venv <venv-path> <python-bin>: create a venv. Returns 0 on success.
make_venv() {
    local venv_path="$1"
    local pybin="$2"
    "$pybin" -m venv "$venv_path"
}

# ----- check A: pip install on default python -----

check_a() {
    echo
    echo "=== Check A: pip install + start_here.py + welcome.py ==="

    local venv="/tmp/autolens_verify_A_$TS"
    local workspace="/tmp/autolens_workspace_verify_A_$TS"
    ARTEFACTS+=("$venv" "$workspace")

    step "creating venv with python3 at $venv"
    if ! make_venv "$venv" python3; then
        RESULTS+=("A|FAIL|could not create venv with python3")
        return
    fi

    # shellcheck source=/dev/null
    source "$venv/bin/activate"

    step "upgrading pip"
    pip install --upgrade pip

    step "pip install $PIP_INSTALL_TARGET"
    if ! pip install "${PIP_INDEX_ARGS[@]}" "$PIP_INSTALL_TARGET" |& tee /tmp/A_pip.log; then
        RESULTS+=("A|FAIL|pip install $PIP_INSTALL_TARGET failed")
        tail_log "Check A pip output" "$(cat /tmp/A_pip.log 2>/dev/null)"
        deactivate
        return
    fi

    step "pip install numba"
    pip install "${PIP_INDEX_ARGS[@]}" numba |& tee /tmp/A_numba.log

    step "showing installed versions"
    python -c "
import autolens, autogalaxy, autoarray, autofit, autonerves
print(f'autolens:   {autolens.__version__}')
print(f'autogalaxy: {autogalaxy.__version__}')
print(f'autoarray:  {autoarray.__version__}')
print(f'autofit:    {autofit.__version__}')
print(f'autonerves: {autonerves.__version__}')
"

    step "cloning autolens_workspace"
    if ! git clone --depth 1 \
            https://github.com/PyAutoLabs/autolens_workspace.git "$workspace"; then
        RESULTS+=("A|FAIL|workspace clone failed")
        deactivate
        return
    fi

    local sh_rc=0 wc_rc=0
    step "running start_here.py (PYAUTO_TEST_MODE=1)"
    (cd "$workspace" && PYAUTO_TEST_MODE=1 JAX_ENABLE_X64=True \
        python start_here.py) |& tee /tmp/A_sh.log
    sh_rc=${PIPESTATUS[0]}

    step "running welcome.py (PYAUTO_TEST_MODE=1)"
    (cd "$workspace" && PYAUTO_TEST_MODE=1 JAX_ENABLE_X64=True \
        python welcome.py) |& tee /tmp/A_wc.log
    wc_rc=${PIPESTATUS[0]}

    deactivate

    if [ "$sh_rc" -eq 0 ] && [ "$wc_rc" -eq 0 ]; then
        RESULTS+=("A|PASS|pip install + start_here.py + welcome.py")
    else
        RESULTS+=("A|FAIL|start_here rc=$sh_rc welcome rc=$wc_rc")
        [ "$sh_rc" -ne 0 ] && tail_log "Check A start_here.py output" "$(cat /tmp/A_sh.log 2>/dev/null)"
        [ "$wc_rc" -ne 0 ] && tail_log "Check A welcome.py output"    "$(cat /tmp/A_wc.log 2>/dev/null)"
    fi
}

# ----- check B: exact-release support floor -----
#
# The 3.12 run resolves the current release when --version is absent; every
# later cell reuses that exact version. This is essential on 3.11: an unpinned
# `pip install autolens` may legitimately select an older compatible wheel,
# which is migration fallback rather than evidence that the new release accepts
# 3.11. A rejected install passes only when pip explicitly reports the
# Requires-Python constraint — index/network/resolver failures stay failures.

CHECK_B_VERSION="$TARGET_VERSION"

check_b_supported() {
    local pybin="$1"
    local label="${pybin#python}"

    if ! command -v "$pybin" > /dev/null 2>&1; then
        step "$pybin not installed — FAIL (required Check B interpreter)"
        RESULTS+=("B|FAIL|$pybin not installed")
        return
    fi

    local venv="/tmp/autolens_verify_B_${label}_$TS"
    ARTEFACTS+=("$venv")

    step "$pybin: creating venv at $venv"
    if ! make_venv "$venv" "$pybin"; then
        RESULTS+=("B|FAIL|$pybin could not create venv")
        return
    fi

    # shellcheck source=/dev/null
    source "$venv/bin/activate"
    pip install --upgrade pip > /dev/null 2>&1 || true

    local install_target="autolens"
    if [ -n "$CHECK_B_VERSION" ]; then
        install_target="autolens==$CHECK_B_VERSION"
    fi

    step "$pybin: pip install $install_target"
    local pip_out pip_rc=0
    pip_out=$(pip install "${PIP_INDEX_ARGS[@]}" "$install_target" 2>&1) || pip_rc=$?

    if [ "$pip_rc" -ne 0 ]; then
        RESULTS+=("B|FAIL|$pybin pip install $install_target failed (rc=$pip_rc)")
        tail_log "Check B ($pybin) pip output" "$pip_out"
        deactivate
        return
    fi

    step "$pybin: importing autolens, autogalaxy, autoarray, autofit, autonerves"
    local import_out import_rc=0 installed_version="" versions_equivalent=1
    import_out=$(python -c "
import autolens, autogalaxy, autoarray, autofit, autonerves
print(f'autolens={autolens.__version__}')
" 2>&1) || import_rc=$?
    if [ "$import_rc" -eq 0 ]; then
        installed_version=$(python -c \
            'from importlib.metadata import version; print(version("autolens"))')
        if [ -n "$installed_version" ] && [ -n "$CHECK_B_VERSION" ] \
                && ! verify_install_versions_equivalent \
                    python "$installed_version" "$CHECK_B_VERSION"; then
            versions_equivalent=0
        fi
    fi
    deactivate

    printf '%s\n' "$import_out" | sed 's/^/      /'

    if [ "$import_rc" -ne 0 ]; then
        RESULTS+=("B|FAIL|$pybin import failed (rc=$import_rc)")
        tail_log "Check B ($pybin) import output" "$import_out"
        return
    fi
    if [ -z "$installed_version" ]; then
        RESULTS+=("B|FAIL|$pybin could not read installed autolens metadata version")
        return
    fi

    if [ "$versions_equivalent" -ne 1 ]; then
        RESULTS+=("B|FAIL|$pybin installed $installed_version, expected $CHECK_B_VERSION")
        return
    fi
    # pip accepted the exact specifier above. Retain its canonical metadata
    # spelling so every later interpreter reuses one byte-identical pin even
    # when the user supplied a PEP 440-equivalent spelling such as 2026.07.
    CHECK_B_VERSION="$installed_version"
    RESULTS+=("B|PASS|$pybin installed + imported autolens==$installed_version")
}

check_b_rejected() {
    local pybin="python3.11"

    if ! command -v "$pybin" > /dev/null 2>&1; then
        step "$pybin not installed — FAIL (required floor-rejection interpreter)"
        RESULTS+=("B|FAIL|$pybin not installed")
        return
    fi
    if [ -z "$CHECK_B_VERSION" ]; then
        RESULTS+=("B|FAIL|no exact autolens version resolved for $pybin rejection")
        return
    fi

    local venv="/tmp/autolens_verify_B_reject_3.11_$TS"
    local install_target="autolens==$CHECK_B_VERSION"
    ARTEFACTS+=("$venv")

    step "$pybin: creating rejection venv at $venv"
    if ! make_venv "$venv" "$pybin"; then
        RESULTS+=("B|FAIL|$pybin could not create venv")
        return
    fi

    # shellcheck source=/dev/null
    source "$venv/bin/activate"
    pip install --upgrade pip > /dev/null 2>&1 || true

    step "$pybin: expecting Requires-Python rejection for $install_target"
    local pip_out pip_rc=0
    pip_out=$(pip install "${PIP_INDEX_ARGS[@]}" "$install_target" 2>&1) || pip_rc=$?
    deactivate

    if [ "$pip_rc" -eq 0 ]; then
        RESULTS+=("B|FAIL|$pybin unexpectedly installed $install_target")
        return
    fi
    if verify_install_requires_python_rejection "$pip_out" "$CHECK_B_VERSION"; then
        RESULTS+=("B|PASS|$pybin rejected $install_target (Requires-Python >=3.12)")
    else
        RESULTS+=("B|FAIL|$pybin install failed without a Requires-Python rejection (rc=$pip_rc)")
        tail_log "Check B ($pybin) unexpected pip output" "$pip_out"
    fi
}

check_b_unpinned_refused() {
    local pybin="python3.11"

    if ! command -v "$pybin" > /dev/null 2>&1; then
        step "$pybin not installed — FAIL (required floor-rejection interpreter)"
        RESULTS+=("B|FAIL|$pybin not installed")
        return
    fi

    local venv="/tmp/autolens_verify_B_unpinned_3.11_$TS"
    ARTEFACTS+=("$venv")

    step "$pybin: creating unpinned-refusal venv at $venv"
    if ! make_venv "$venv" "$pybin"; then
        RESULTS+=("B|FAIL|$pybin could not create venv")
        return
    fi

    # shellcheck source=/dev/null
    source "$venv/bin/activate"
    pip install --upgrade pip > /dev/null 2>&1 || true

    step "$pybin: expecting an UNPINNED install to be refused"
    local pip_out pip_rc=0
    pip_out=$(pip install "${PIP_INDEX_ARGS[@]}" autolens 2>&1) || pip_rc=$?
    deactivate

    if [ "$pip_rc" -eq 0 ]; then
        RESULTS+=("B|FAIL|$pybin silently installed an unpinned autolens — the sub-floor backtrack is back")
        tail_log "Check B ($pybin) unpinned install unexpectedly succeeded" "$pip_out"
        return
    fi
    if verify_install_unpinned_refusal "$pip_out" autolens; then
        RESULTS+=("B|PASS|$pybin refused the unpinned install")
    else
        RESULTS+=("B|FAIL|$pybin unpinned install failed without a floor refusal (rc=$pip_rc)")
        tail_log "Check B ($pybin) unexpected unpinned pip output" "$pip_out"
    fi
}

check_b() {
    echo
    echo "=== Check B: exact release on 3.12/3.13; pinned and unpinned refused on 3.11 ==="
    check_b_supported python3.12
    check_b_supported python3.13
    check_b_rejected
    check_b_unpinned_refused
}

# ----- check C: conda flow -----

check_c() {
    echo
    echo "=== Check C: conda install flow ==="

    if ! command -v conda > /dev/null 2>&1; then
        step "conda not on PATH — SKIP"
        RESULTS+=("C|SKIP|conda not on PATH")
        return
    fi

    local env_name="autolens_verify_$TS"
    local workspace="/tmp/autolens_workspace_verify_C_$TS"
    CONDA_ENVS+=("$env_name")
    ARTEFACTS+=("$workspace")

    step "conda create -n $env_name python=3.12"
    if ! conda create -y -n "$env_name" python=3.12 |& tee /tmp/C_create.log; then
        RESULTS+=("C|FAIL|conda create failed")
        tail_log "Check C conda create output" "$(cat /tmp/C_create.log 2>/dev/null)"
        return
    fi

    step "upgrading pip in $env_name"
    conda run -n "$env_name" pip install --upgrade pip

    step "conda pip install $PIP_INSTALL_TARGET --no-cache-dir"
    if ! conda run -n "$env_name" pip install "${PIP_INDEX_ARGS[@]}" \
            "$PIP_INSTALL_TARGET" --no-cache-dir |& tee /tmp/C_pip.log; then
        RESULTS+=("C|FAIL|conda pip install $PIP_INSTALL_TARGET failed")
        tail_log "Check C pip output" "$(cat /tmp/C_pip.log 2>/dev/null)"
        return
    fi

    step "conda pip install numba --no-cache-dir"
    conda run -n "$env_name" pip install "${PIP_INDEX_ARGS[@]}" \
        numba --no-cache-dir |& tee /tmp/C_numba.log

    step "cloning autolens_workspace"
    if ! git clone --depth 1 \
            https://github.com/PyAutoLabs/autolens_workspace.git "$workspace"; then
        RESULTS+=("C|FAIL|workspace clone failed")
        return
    fi

    local sh_rc=0 wc_rc=0
    step "conda run welcome.py (PYAUTO_TEST_MODE=1)"
    (cd "$workspace" && conda run -n "$env_name" \
        env PYAUTO_TEST_MODE=1 JAX_ENABLE_X64=True python welcome.py) |& tee /tmp/C_wc.log
    wc_rc=${PIPESTATUS[0]}

    step "conda run start_here.py (PYAUTO_TEST_MODE=1)"
    (cd "$workspace" && conda run -n "$env_name" \
        env PYAUTO_TEST_MODE=1 JAX_ENABLE_X64=True python start_here.py) |& tee /tmp/C_sh.log
    sh_rc=${PIPESTATUS[0]}

    if [ "$sh_rc" -eq 0 ] && [ "$wc_rc" -eq 0 ]; then
        RESULTS+=("C|PASS|conda(python=3.12) + start_here + welcome")
    else
        RESULTS+=("C|FAIL|start_here rc=$sh_rc welcome rc=$wc_rc")
        [ "$sh_rc" -ne 0 ] && tail_log "Check C start_here.py output" "$(cat /tmp/C_sh.log 2>/dev/null)"
        [ "$wc_rc" -ne 0 ] && tail_log "Check C welcome.py output"    "$(cat /tmp/C_wc.log 2>/dev/null)"
    fi
}

# ----- check D: optional extra resolves -----

check_d() {
    echo
    echo "=== Check D: pip install \"$PIP_INSTALL_OPTIONAL\" ==="

    local venv="/tmp/autolens_verify_D_$TS"
    ARTEFACTS+=("$venv")

    step "creating venv with python3 at $venv"
    if ! make_venv "$venv" python3; then
        RESULTS+=("D|FAIL|could not create venv with python3")
        return
    fi

    # shellcheck source=/dev/null
    source "$venv/bin/activate"
    pip install --upgrade pip > /dev/null 2>&1

    # `python3` is whatever the host defaults to — 3.12 on a dev laptop, 3.13 in
    # the release job (setup-python runs 3.11/3.12/3.13 and the last one wins).
    # The resolution differs between them, so the evidence has to name the
    # interpreter it actually exercised (PyAutoLens#687).
    local pyver
    pyver=$(python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
    step "venv interpreter is python $pyver"

    step "pip install $PIP_INSTALL_OPTIONAL"
    pip install "${PIP_INDEX_ARGS[@]}" "$PIP_INSTALL_OPTIONAL" |& tee /tmp/D_pip.log
    local pip_rc=${PIPESTATUS[0]}

    step "import autolens"
    local import_rc=0
    python -c "import autolens; print(autolens.__version__)" || import_rc=$?
    deactivate

    if [ "$pip_rc" -eq 0 ] && [ "$import_rc" -eq 0 ]; then
        RESULTS+=("D|PASS|$PIP_INSTALL_OPTIONAL resolved + imports (python $pyver)")
    else
        RESULTS+=("D|FAIL|pip rc=$pip_rc import rc=$import_rc (python $pyver)")
        tail_log "Check D output" "$(cat /tmp/D_pip.log 2>/dev/null)"
    fi
}

# ----- check E: yanked-pin -----

check_e() {
    echo
    echo "=== Check E: pip install autolens==2026.2.26.4 (yanked) ==="

    local venv="/tmp/autolens_verify_E_$TS"

    if ! command -v python3.12 > /dev/null 2>&1; then
        step "python3.12 not installed — FAIL (required Check E interpreter)"
        RESULTS+=("E|FAIL|python3.12 not installed")
        return
    fi

    ARTEFACTS+=("$venv")

    # This pinned 2026.2.26.4 stack predates Python 3.13 dependency wheels
    # (notably SciPy 1.14.0). Check E verifies yanked-wheel reachability, not
    # forward interpreter support, so keep its historical environment on 3.12.
    step "creating venv with python3.12 at $venv"
    if ! make_venv "$venv" python3.12; then
        RESULTS+=("E|FAIL|could not create venv with python3.12")
        return
    fi

    # shellcheck source=/dev/null
    source "$venv/bin/activate"
    pip install --upgrade pip > /dev/null 2>&1

    # Pin all 5 PyAuto libs together — pinning only autolens lets pip resolve
    # autogalaxy/autofit/autoarray to latest, which can break the autolens
    # 2026.2.26.4 import path (e.g. ModuleNotFoundError on a renamed/removed
    # symbol in autogalaxy 2026.5.1.4). Same multi-pin pattern that release.yml
    # uses (lines 130-138).
    step "pip install autoconf/autoarray/autofit/autogalaxy/autolens==2026.2.26.4"
    pip install "${PIP_INDEX_ARGS[@]}" \
      autoconf==2026.2.26.4 \
      autoarray==2026.2.26.4 \
      autofit==2026.2.26.4 \
      autogalaxy==2026.2.26.4 \
      autolens==2026.2.26.4 |& tee /tmp/E_pip.log
    local pip_rc=${PIPESTATUS[0]}

    # Verify pip install resolved + downloaded all 5 wheels (i.e. yanked
    # version is still reachable via explicit pin). We deliberately do NOT
    # exercise `import autolens` here: yanked versions are typically yanked
    # because of bugs, and 2026.2.26.4 specifically has an import-time
    # autoconf config-key lookup that fails standalone — exactly the kind
    # of issue that justified yanking it. Check E's purpose is to verify
    # the install path (resolve + download), not runtime correctness.
    local installed_pkgs=""
    if [ "$pip_rc" -eq 0 ]; then
        step "verifying all 5 libs installed at 2026.2.26.4"
        installed_pkgs=$(pip list --format=freeze 2>/dev/null | grep -E "^(autoconf|autoarray|autofit|autogalaxy|autolens)==" | sort | tr '\n' ' ')
        echo "      $installed_pkgs"
    fi
    deactivate

    # PASS if pip succeeded AND all 5 libs report version 2026.2.26.4 from `pip list`.
    local expected_count=5
    local actual_count
    actual_count=$(printf '%s' "$installed_pkgs" | grep -oE "==2026.2.26.4" | wc -l)
    if [ "$pip_rc" -eq 0 ] && [ "$actual_count" -eq "$expected_count" ]; then
        RESULTS+=("E|PASS|all 5 libs installed at 2026.2.26.4 via explicit pin (python3.12)")
    else
        RESULTS+=("E|FAIL|pip rc=$pip_rc, $actual_count/$expected_count libs at 2026.2.26.4")
        tail_log "Check E output" "$(cat /tmp/E_pip.log 2>/dev/null)"
    fi
}

# ----- check F: Colab simulation — the Colab package set, then the setup cell -----
#
# This check used to "emulate Colab" with `pip install autolens jax` WITH
# dependencies. That install is what made the check blind: the venv already
# held corner, optax, xxhash, blackjax and every other declared dependency
# before the setup cell ran, so the cell's real `pip install ... --no-deps`
# could never be observed to miss one. Notebooks that die on Colab at the first
# post-fit plot passed check F for months.
#
# The venv is now built from the package set Google actually ships (the
# `googlecolab/backend-info` pip-freeze manifest) via colab_gate.py:
#
#   seed    resolve the with-deps closure WITHOUT installing it, install only
#           the part of it Colab also ships, at Colab's pinned versions
#   <cell>  the injected setup cell verbatim: `pip install ... --no-deps` +
#           workspace clone + autonerves config
#   verify  walk the installed libraries' declared requirements, AST-scan every
#           import in their source and probe each one for real, construct the
#           headline searches
#   <cell>  one real notebook cell (al.Imaging.from_fits)
#
# The interpreter is python3.12 because Colab is python3.12 — check B's
# "missing required interpreter is FAIL" rule applies here too.
#
# --- two facets in a rehearsal (--version), one continuously ---------------
#
# The injected setup cell is verbatim, and verbatim means UNPINNED: it runs
# `pip install autonerves --no-deps`, and the released `setup_colab.setup()` it
# then imports installs `autolens autogalaxy autofit autoarray autonerves ...
# --no-deps` unpinned too. pip cannot select a dev pre-release for either, so in
# a TestPyPI rehearsal the cell pulls the whole stack back down to the current
# PyPI release — whatever the seed step pinned. Audited as-is, the gate would
# therefore measure the RELEASED bootstrap and never the candidate, and a
# released bootstrap that is broken would hold Heart RED over the very release
# that carries its fix (chicken-and-egg, 2026-09-17).
#
# So with --version check F runs the audit twice:
#
#   RELEASED   advisory. What a reader who opens the notebook today actually
#              gets. Reported as a WARN row: it never fails the run, because
#              the release IS the remedy and grading it YELLOW/RED would block
#              it.
#   <re-pin>   all five PyAuto packages pinned to the candidate from the
#              rehearsal index (--no-deps), then `setup_colab` reloaded and its
#              own package list reinstalled exactly as `_colab_setup` does — the
#              candidate's bootstrap, run for real.
#   CANDIDATE  the gate. FAIL here is FAIL as it has always been.
#
# Without --version (the continuous run against PyPI) the released bootstrap IS
# the candidate, so there is one audit and nothing changes.
#
# COLAB_GATE_AUTONERVES_SRC (dev/witness only): a path or requirement installed
# `--no-deps` over the released `autonerves` — after the candidate re-pin in a
# rehearsal, before the single audit in a continuous run. It exists to rehearse
# an UNRELEASED setup_colab.py — the bootstrap package list is the thing this
# check gates, and until it is on PyPI there is no other way to run the gate
# against a fix. Never set in CI.

# f_report_str <json-path> <key...>: print a string field out of a colab_gate
# report (nested keys walk down), or "" when it is missing or unreadable.
f_report_str() {
    python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("")
else:
    for key in sys.argv[2:]:
        data = data.get(key) if isinstance(data, dict) else None
    print(data if isinstance(data, str) else "")
' "$@" 2>/dev/null
}

# f_overlay_autonerves_src: the COLAB_GATE_AUTONERVES_SRC overlay for the
# CONTINUOUS path (the rehearsal path does the same install inside the re-pin
# driver, where it has to sit between the candidate pins and the setup_colab
# reload). No-op when the variable is unset, which is every CI run.
f_overlay_autonerves_src() {
    if [ -z "${COLAB_GATE_AUTONERVES_SRC:-}" ]; then
        return 0
    fi
    COLAB_GATE_AUTONERVES_SRC="$COLAB_GATE_AUTONERVES_SRC" python -c '
import os, subprocess, sys

_autonerves_src = os.environ["COLAB_GATE_AUTONERVES_SRC"]
print(f"COLAB_GATE_AUTONERVES_SRC set — overlaying {_autonerves_src}")
subprocess.check_call(
    [sys.executable, "-m", "pip", "install", "--no-deps", _autonerves_src]
)
'
}

check_f() {
    echo
    echo "=== Check F: Colab gate (Colab package set + setup cell + package audit + notebook cell) ==="

    local venv="/tmp/autolens_verify_F_$TS"
    local ws_dir="/tmp/colab_sim_workspace_F_$TS"
    local seed_json="/tmp/F_gate_seed_$TS.json"
    local verify_json="/tmp/F_gate_verify_$TS.json"
    local verify_released_json="/tmp/F_gate_verify_released_$TS.json"
    # The gate reports are read by the sidecar writer, which runs before
    # cleanup — so they can be swept with everything else (and kept by --keep).
    # The released report only exists on the rehearsal path; the writer skips a
    # path that is not a file.
    ARTEFACTS+=("$venv" "$ws_dir" "$seed_json" "$verify_json" "$verify_released_json")
    F_GATE_SEED_JSON="$seed_json"
    F_GATE_VERIFY_JSON="$verify_json"
    F_GATE_VERIFY_RELEASED_JSON="$verify_released_json"

    # Colab runs Python 3.12. A different interpreter would seed Colab's pins
    # against the wrong wheels, so a missing python3.12 is FAIL, not SKIP —
    # the same rule Checks B and E apply to their required interpreters.
    if ! command -v python3.12 > /dev/null 2>&1; then
        RESULTS+=("F|FAIL|python3.12 not found")
        return
    fi

    step "creating venv with python3.12 at $venv"
    if ! make_venv "$venv" python3.12; then
        RESULTS+=("F|FAIL|could not create venv with python3.12")
        return
    fi

    # shellcheck source=/dev/null
    source "$venv/bin/activate"
    # `packaging` is the gate's only non-stdlib dependency (it evaluates the
    # environment markers and version specifiers in the requirement walk).
    pip install --upgrade pip packaging > /dev/null 2>&1

    # When a version is pinned (a TestPyPI rehearsal), pin ALL five PyAuto
    # packages to it — otherwise the closure resolves `autolens` at the pinned
    # dev version but autoarray/autonerves/autofit/autogalaxy to the latest
    # *final* release on PyPI (dev versions are pre-releases pip won't pick for
    # a floor-only dependency), so the gate would audit an incoherent
    # dev/released mix instead of the same wheels as Checks A/C/D.
    local f_targets=("$PIP_INSTALL_TARGET")
    if [ -n "$TARGET_VERSION" ]; then
        f_targets=(
            "autolens==$TARGET_VERSION"
            "autoarray==$TARGET_VERSION"
            "autonerves==$TARGET_VERSION"
            "autofit==$TARGET_VERSION"
            "autogalaxy==$TARGET_VERSION"
        )
    fi

    step "seeding the venv with Colab's package set (closure of ${f_targets[*]} + jax)"
    local seed_rc=0
    "$venv/bin/python" "$VERIFY_INSTALL_DIR/colab_gate.py" seed \
        --manifest-cache "$COLAB_MANIFEST_CACHE" \
        --report-json "$seed_json" \
        --targets "${f_targets[@]}" \
        --index-args "${PIP_INDEX_ARGS[@]}" |& tee /tmp/F_seed.log
    seed_rc=${PIPESTATUS[0]}
    if [ "$seed_rc" -ne 0 ]; then
        RESULTS+=("F|FAIL|colab gate: seeding Colab's package set failed (rc=$seed_rc)")
        tail_log "Check F colab_gate seed output" "$(cat /tmp/F_seed.log 2>/dev/null)"
        deactivate
        return
    fi

    # A fake google.colab package makes `import google.colab` — the probe both
    # the injected cell and setup_colab use — succeed, activating the real
    # on-Colab code path.
    step "installing fake google.colab stub into the venv"
    local site
    site=$(python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    mkdir -p "$site/google/colab"
    touch "$site/google/__init__.py" "$site/google/colab/__init__.py"
    # JAX detects Colab too: importing jax with google.colab present triggers
    # `from google.colab import output` (jax._src.debugger.colab_lib), so the
    # stub needs the submodule real Colab provides.
    touch "$site/google/colab/output.py"

    # --- driver part 1: the injected notebook cell, verbatim, then setup ---
    # Exit 3 = SKIP (installed autonerves predates the setup_colab registry).
    step "running the Colab bootstrap driver (setup cell + workspace clone)"
    cat > /tmp/F_driver_setup.py <<'PYEOF'
import os
import subprocess
import sys

WS_DIR = os.environ["COLAB_SIM_WORKSPACE_DIR"]

# --- the injected setup cell, verbatim ---
try:
    import google.colab
except ImportError:
    from autolens import setup_colab as _setup_colab
else:
    import importlib
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "autonerves", "--no-deps"]
    )
    _setup_colab = importlib.import_module("autonerves.setup_colab")

# NB: nothing is laid over the bootstrap here — the cell is the cell. The
# COLAB_GATE_AUTONERVES_SRC overlay and, in a rehearsal, the candidate re-pin
# happen after this driver, so what the cell installs is measurable on its own.

if not hasattr(_setup_colab, "setup"):
    print(
        "SKIP: installed autonerves predates the setup_colab registry "
        "(ships with the next release)"
    )
    sys.exit(3)

_setup_colab.setup("autolens", raise_error_if_not_gpu=False, workspace_dir=WS_DIR)

# --- assertions: clone happened, cwd moved into the workspace ---
assert os.getcwd() == WS_DIR, f"cwd is {os.getcwd()}, expected {WS_DIR}"
assert os.path.isdir(os.path.join(WS_DIR, "config")), "workspace config/ missing"
assert os.path.isdir(os.path.join(WS_DIR, "dataset")), "workspace dataset/ missing"
print("setup cell OK: bootstrap installed, workspace cloned, cwd moved")
PYEOF
    local setup_rc=0
    COLAB_SIM_WORKSPACE_DIR="$ws_dir" python /tmp/F_driver_setup.py |& tee /tmp/F_driver.log
    setup_rc=${PIPESTATUS[0]}

    if [ "$setup_rc" -eq 3 ]; then
        RESULTS+=("F|SKIP|installed autonerves predates setup_colab registry (next release)")
        deactivate
        return
    fi
    if [ "$setup_rc" -ne 0 ]; then
        RESULTS+=("F|FAIL|setup cell rc=$setup_rc")
        tail_log "Check F driver output" "$(cat /tmp/F_driver.log 2>/dev/null)"
        deactivate
        return
    fi

    # --- the gate: what did the --no-deps bootstrap actually leave behind? ---
    # Every audit runs from inside the cloned workspace so autonerves resolves
    # its config the way a notebook cell does (conf reads the cwd).
    local gate_rc=0
    local gate_detail=""
    local released_rc=0
    local released_detail=""
    local released_ver="?"

    if [ -n "$TARGET_VERSION" ]; then
        # --- facet 1: the RELEASED bootstrap, advisory ---
        # This is what the verbatim cell just installed, and what a reader who
        # opens the notebook today gets. It is never a FAIL: see the header.
        step "auditing the RELEASED bootstrap (advisory — what a reader gets today)"
        (cd "$ws_dir" && "$venv/bin/python" "$VERIFY_INSTALL_DIR/colab_gate.py" verify \
            --manifest-cache "$COLAB_MANIFEST_CACHE" \
            --seed-report "$seed_json" \
            --report-json "$verify_released_json" \
            --index-args "${PIP_INDEX_ARGS[@]}") |& tee /tmp/F_gate_released.log
        released_rc=${PIPESTATUS[0]}
        released_detail=$(f_report_str "$verify_released_json" detail)
        released_ver=$(f_report_str "$verify_released_json" packages autonerves)
        [ -n "$released_ver" ] || released_ver="?"
        if [ -z "$released_detail" ]; then
            released_detail="verify could not run (rc=$released_rc)"
        fi

        # --- re-pin the venv to the candidate, then re-run its bootstrap ---
        step "re-pinning the venv to the candidate $TARGET_VERSION"
        cat > /tmp/F_driver_repin.py <<'PYEOF'
"""Re-pin the simulated Colab venv from the release to the candidate.

The verbatim setup cell has already run: the workspace is cloned and the cwd is
inside it, so `setup()` is NOT called again. What is redone is the part a
rehearsal needs pinned — the five PyAuto wheels, and then the candidate's own
bootstrap package list, installed exactly as `autonerves.setup_colab._colab_setup`
installs it.
"""

import importlib
import os
import subprocess
import sys

index_args = sys.argv[1:]          # the rehearsal's pip index args, if any
version = os.environ["COLAB_GATE_TARGET_VERSION"]

pins = [
    f"autonerves=={version}",
    f"autofit=={version}",
    f"autoarray=={version}",
    f"autogalaxy=={version}",
    f"autolens=={version}",
]
print(f"re-pinning the PyAuto stack to the candidate {version}")
subprocess.check_call(
    [sys.executable, "-m", "pip", "install", "--no-deps", *index_args, *pins]
)

# --- dev/witness override: rehearse an unreleased setup_colab.py ---
#
# After the pins, so the local source wins over the candidate wheel; before the
# reload, so the package list read below is the one being rehearsed.
_autonerves_src = os.environ.get("COLAB_GATE_AUTONERVES_SRC")
if _autonerves_src:
    print(f"COLAB_GATE_AUTONERVES_SRC set — overlaying {_autonerves_src}")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--no-deps", _autonerves_src]
    )

# The wheels landed after this interpreter started, so the import system's
# directory caches predate them.
importlib.invalidate_caches()
import autonerves.setup_colab as sc

sc = importlib.reload(sc)
if not isinstance(getattr(sc, "_PROJECTS", None), dict) or "autolens" not in sc._PROJECTS:
    print(
        "ERROR: candidate autonerves.setup_colab exposes no _PROJECTS['autolens'] "
        "entry — its bootstrap package list cannot be mirrored"
    )
    sys.exit(4)

packages = sc._PROJECTS["autolens"]["packages"]
subprocess.check_call([sys.executable, "-m", "pip", "install", *packages, "--no-deps"])
print(
    f"re-pin OK: candidate {version} bootstrap package list installed "
    f"({len(packages)} packages)"
)
PYEOF
        local repin_rc=0
        COLAB_GATE_TARGET_VERSION="$TARGET_VERSION" \
            python /tmp/F_driver_repin.py "${PIP_INDEX_ARGS[@]}" |& tee /tmp/F_repin.log
        repin_rc=${PIPESTATUS[0]}
        if [ "$repin_rc" -ne 0 ]; then
            RESULTS+=("F|FAIL|candidate re-pin rc=$repin_rc")
            tail_log "Check F candidate re-pin output" "$(cat /tmp/F_repin.log 2>/dev/null)"
            deactivate
            return
        fi

        step "auditing the CANDIDATE bootstrap (the gate)"
    else
        # Continuous run: the released bootstrap IS the candidate, so there is
        # one audit and the dev/witness overlay (if any) goes in front of it.
        f_overlay_autonerves_src
        step "auditing the bootstrapped environment against Colab's package set"
    fi

    (cd "$ws_dir" && "$venv/bin/python" "$VERIFY_INSTALL_DIR/colab_gate.py" verify \
        --manifest-cache "$COLAB_MANIFEST_CACHE" \
        --seed-report "$seed_json" \
        --report-json "$verify_json" \
        --index-args "${PIP_INDEX_ARGS[@]}") |& tee /tmp/F_gate.log
    gate_rc=${PIPESTATUS[0]}
    gate_detail=$(f_report_str "$verify_json" detail)

    if [ "$gate_rc" -eq 2 ] || { [ "$gate_rc" -ne 0 ] && [ -z "$gate_detail" ]; }; then
        RESULTS+=("F|FAIL|colab gate: verify could not run (rc=$gate_rc)")
        tail_log "Check F colab_gate verify output" "$(cat /tmp/F_gate.log 2>/dev/null)"
        deactivate
        return
    fi
    if [ "$gate_rc" -ne 0 ]; then
        RESULTS+=("F|FAIL|$gate_detail")
        tail_log "Check F colab_gate verify output" "$(cat /tmp/F_gate.log 2>/dev/null)"
        deactivate
        return
    fi

    # The candidate passed. If the bootstrap a reader gets today did not, say
    # so — as a WARN row, which prints in the table and travels into the
    # sidecar but leaves `ready` (and therefore the Heart verdict) alone.
    if [ -n "$TARGET_VERSION" ] && [ "$released_rc" -ne 0 ]; then
        RESULTS+=("F|WARN|released Colab bootstrap (autonerves=$released_ver) broken for readers: $released_detail; candidate $TARGET_VERSION passes")
    fi

    # --- driver part 2: one real notebook cell (the top of imaging/start_here) ---
    #
    # Load the SAME dataset the current imaging/start_here.py loads: the bundled
    # `cosmos_web_ring` JWST example (one of the few datasets that ship committed
    # with the workspace, cloned above by setup_colab). The old `dataset/imaging/
    # simple/*.fits` path was never bundled and is no longer used by start_here —
    # after the release dataset `-f` leak fix (PyAutoBuild#150) it would only exist
    # if simulated at run time, so loading it here crashed FileNotFoundError while
    # Check A (which runs start_here.py) passed.
    step "running one real notebook cell (al.Imaging.from_fits)"
    cat > /tmp/F_driver_cell.py <<'PYEOF'
import autolens as al

dataset = al.Imaging.from_fits(
    data_path="dataset/imaging/cosmos_web_ring/data.fits",
    noise_map_path="dataset/imaging/cosmos_web_ring/noise_map.fits",
    psf_path="dataset/imaging/cosmos_web_ring/psf.fits",
    pixel_scales=0.06,
)
print(f"cell OK: loaded imaging dataset, shape {dataset.data.shape_native}")
PYEOF
    local cell_rc=0
    (cd "$ws_dir" && python /tmp/F_driver_cell.py) |& tee /tmp/F_cell.log
    cell_rc=${PIPESTATUS[0]}

    deactivate

    if [ "$cell_rc" -eq 0 ]; then
        RESULTS+=("F|PASS|$gate_detail")
    else
        RESULTS+=("F|FAIL|notebook cell rc=$cell_rc")
        tail_log "Check F notebook cell output" "$(cat /tmp/F_cell.log 2>/dev/null)"
    fi
}

# ----- runner -----

START_TS=$(date +%H:%M:%S)
echo "verify_install starting at $START_TS — running checks: ${SELECTED[*]}"

for letter in "${SELECTED[@]}"; do
    case "$letter" in
        A) check_a ;;
        B) check_b ;;
        C) check_c ;;
        D) check_d ;;
        E) check_e ;;
        F) check_f ;;
        *) echo "verify_install: unknown check '$letter'" >&2 ;;
    esac
done

# ----- report -----

echo
echo "Install Verification Results"
echo "============================"
printf '%-5s  %-6s  %s\n' "Check" "Status" "Detail"
printf '%-5s  %-6s  %s\n' "-----" "------" "------"

n_fail=0
n_skip=0
# WARN is advisory and deliberately NOT counted in n_fail: check F's
# released-bootstrap facet reports what a reader gets today, and a broken
# release is not evidence against shipping the candidate that fixes it
# (human decision, 2026-09-17). Only FAIL moves `ready`.
n_warn=0
for row in "${RESULTS[@]}"; do
    IFS='|' read -r letter status detail <<< "$row"
    printf '%-5s  %-6s  %s\n' "$letter" "$status" "$detail"
    [ "$status" = "FAIL" ] && n_fail=$((n_fail + 1))
    [ "$status" = "SKIP" ] && n_skip=$((n_skip + 1))
    [ "$status" = "WARN" ] && n_warn=$((n_warn + 1))
done

echo
if [ "$n_fail" -eq 0 ]; then
    echo "Overall: PASS ($n_skip skipped, $n_warn warning(s))"
else
    echo "Overall: FAIL ($n_fail failure(s), $n_skip skipped, $n_warn warning(s))"
fi

if [ -n "$RESULTS_LOG" ]; then
    echo
    echo "----- Failure detail -----"
    printf '%s\n' "$RESULTS_LOG"
fi

# ----- machine-readable sidecar (consumed by pyauto-heart readiness) -----

if [ -n "$REPORT_JSON" ]; then
    if [ "$n_fail" -eq 0 ]; then ready_bool=true; else ready_bool=false; fi
    # Which index the wheels came from travels with the result. A --testpypi run
    # proves the about-to-ship wheels install; it says nothing about the current
    # PyPI release. Readiness reports the index rather than flattening the two,
    # so the verdict never claims more than was verified.
    if [ -n "$FIND_LINKS" ]; then
        vi_index=find-links
    elif [ "$USE_TESTPYPI" -eq 1 ]; then
        vi_index=testpypi
    else
        vi_index=pypi
    fi
    printf '%s\n' "${RESULTS[@]}" | \
      VI_READY="$ready_bool" VI_VERSION="$TARGET_VERSION" VI_CHECK_B_VERSION="$CHECK_B_VERSION" \
      VI_REPORT_JSON="$REPORT_JSON" \
      VI_INDEX="$vi_index" \
      VI_F_GATE_SEED="$F_GATE_SEED_JSON" VI_F_GATE_VERIFY="$F_GATE_VERIFY_JSON" \
      VI_F_GATE_VERIFY_RELEASED="$F_GATE_VERIFY_RELEASED_JSON" \
      python3 -c '
import datetime, json, os, sys
checks = []
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line:
        continue
    parts = (line.split("|", 2) + ["", "", ""])[:3]
    checks.append({"check": parts[0], "status": parts[1], "detail": parts[2]})

# Check F carries the Colab gate report as a nested "colab_gate" key on its own
# row: "seed", "verify" (the gated facet) and, on a rehearsal, "verify_released"
# (the advisory audit of the bootstrap a reader gets today). It is attached to
# every F row, the WARN one included. Additive only: every existing key and the
# shape of "checks" (a list of {check,status,detail}) are untouched, because
# heart/readiness.py and heart/validate.py parse this file and must keep
# working unchanged.
def _read(var):
    path = os.environ.get(var) or ""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None

gate = {k: v for k, v in (("seed", _read("VI_F_GATE_SEED")),
                          ("verify", _read("VI_F_GATE_VERIFY")),
                          ("verify_released", _read("VI_F_GATE_VERIFY_RELEASED")))
        if v is not None}
if gate:
    for entry in checks:
        if entry["check"] == "F":
            entry["colab_gate"] = gate

out = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "ready": os.environ["VI_READY"] == "true",
    "version": os.environ.get("VI_VERSION") or None,
    "check_b_version": os.environ.get("VI_CHECK_B_VERSION") or None,
    "index": os.environ.get("VI_INDEX") or "pypi",
    "checks": checks,
}
path = os.environ["VI_REPORT_JSON"]
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(out, f, indent=2)
os.replace(tmp, path)
'
    echo
    echo "Wrote JSON report: $REPORT_JSON"
fi

# ----- cleanup -----

if [ "$KEEP" -eq 1 ]; then
    echo
    echo "--keep: artefacts retained:"
    for p in "${ARTEFACTS[@]}"; do echo "  $p"; done
    for n in "${CONDA_ENVS[@]}"; do echo "  conda env: $n"; done
else
    echo
    echo "Cleaning up artefacts (use --keep next time to retain)..."
    for p in "${ARTEFACTS[@]}"; do
        rm -rf "$p"
    done
    for n in "${CONDA_ENVS[@]}"; do
        conda env remove -y -n "$n" > /dev/null 2>&1 || true
    done
fi

[ "$n_fail" -eq 0 ]
