#!/usr/bin/env bash
#
# install_arcticpy.sh — THE canonical arcticpy install for the whole organism.
#
# This file is the single home of the recipe and of the `arcticpy==2.6` pin.
# Two consumers execute these exact bytes:
#
#   * CI  — `.github/actions/install-arcticpy/action.yml` (the composite action
#           every CTI repo's workflow references) runs it via
#           `${{ github.action_path }}/install_arcticpy.sh`.
#   * Local — `heart/smoke.py` runs it directly out of the Heart checkout when a
#           workspace's `smoke:` entry sets `arcticpy: true`.
#
# It is a shell script rather than only composite-action steps precisely so the
# local runner can share it: a composite action cannot be invoked from Python,
# and a second copy of the recipe is the failure this whole arrangement exists
# to prevent (PyAutoHeart#170 replaced four divergent copies with one).
#
# WHY THIS IS FIDDLY
#
# arcticpy is deliberately NOT a pip dependency of autocti:
#
#   * its PyPI distribution is a source-only C++ sdist — it needs libgsl-dev
#     headers and a toolchain to build, and there is no wheel;
#   * its own requirements downgrade numpy below 2.0, which breaks a modern
#     PyAuto stack.
#
# So it is built with --no-build-isolation (reusing the numpy already present
# instead of resolving its own) and --no-deps (so it cannot drag numpy back
# down). Each flag costs something that has to be paid back explicitly:
#
#   --no-build-isolation  pip does NOT create an isolated build environment and
#                         does NOT read pyproject's build-system.requires, so
#                         every BUILD dependency must already be installed.
#                         arcticpy declares none of them.
#   --no-deps             pip installs no RUNTIME dependencies either, so the
#                         packages arcticpy imports at import time must also be
#                         installed by hand.
#
# Both sets are installed below, and the script ends by proving the result
# actually imports.
#
# Verified 2026-08-24 in a clean container against a bare venv: each missing
# build dependency failed the build naming the next one. Without setuptools the
# build dies at `BackendUnavailable: Cannot import 'setuptools.build_meta'` —
# and Python 3.12+ venvs no longer ship setuptools by default, which is why the
# recipe that omitted it was a real hazard rather than a style nit.
#
# This script is intentionally SELF-CONTAINED: it upgrades pip/setuptools/wheel
# itself rather than relying on a caller having done so in a preceding step.
#
# ENVIRONMENT CONTRACT (all optional; defaults match the previous action)
#
#   ARCTICPY_VERSION      arcticpy version to build.  Default 2.6.
#                         THE single pin for the organism — bump it HERE.
#   ARCTICPY_INSTALL_GSL  "true"/"false". Install libgsl-dev via apt.
#                         Default true. `false` means "GSL is already present",
#                         and the script PROVES that rather than assuming it.
#   ARCTICPY_SUDO         "true"/"false". Use sudo for the apt leg. Default
#                         true (GitHub-hosted runners); set false in a
#                         container already running as root.
#   ARCTICPY_GSL_PREFIXES Space-separated include prefixes searched when the
#                         apt leg is skipped. Default covers Debian/Ubuntu,
#                         /usr/local and Homebrew-on-Apple-Silicon. Override it
#                         for the no-root workaround where GSL headers are
#                         extracted somewhere unprivileged (PyAutoCTI/AGENTS.md
#                         section arcticpy).
#   PYTHON                Interpreter to install into. Default `python`, which
#                         is correct both on a runner and inside an activated
#                         venv; heart/smoke.py passes its isolated venv's
#                         interpreter explicitly.
#
# On success, prints the installed version and — when $GITHUB_OUTPUT is set —
# writes `version=<v>` to it, so the action's `version` output still works.

set -euo pipefail

ARCTICPY_VERSION="${ARCTICPY_VERSION:-2.6}"
ARCTICPY_INSTALL_GSL="${ARCTICPY_INSTALL_GSL:-true}"
ARCTICPY_SUDO="${ARCTICPY_SUDO:-true}"
PYTHON="${PYTHON:-python}"

if [ "$ARCTICPY_INSTALL_GSL" = "true" ]; then
    echo "==> Installing GSL headers"
    SUDO=""
    if [ "$ARCTICPY_SUDO" = "true" ]; then SUDO="sudo"; fi
    $SUDO apt-get update
    $SUDO apt-get install -y libgsl-dev
else
    # The caller says GSL is already there. Check it, because the alternative is
    # a compiler error hundreds of lines into the build naming a header, which
    # reads as "arcticpy is broken" rather than "you are missing a system
    # package". This is the path heart/smoke.py takes: a local dev command must
    # not mutate system packages (and apt-get does not exist on macOS at all),
    # so it declines to install GSL but must still fail legibly without it.
    echo "==> Skipping GSL install (ARCTICPY_INSTALL_GSL=false); checking headers"
    # Test each prefix independently. `ls a b c` is NOT the way to ask this:
    # it exits non-zero when ANY operand is missing, so on a machine with GSL
    # in exactly one of these three prefixes -- i.e. every machine that has it
    # -- the check would report it absent.
    GSL_PREFIXES="${ARCTICPY_GSL_PREFIXES:-/usr/include /usr/local/include /opt/homebrew/include}"
    GSL_FOUND="false"
    for prefix in $GSL_PREFIXES; do
        if [ -f "$prefix/gsl/gsl_version.h" ]; then
            echo "    found GSL headers in $prefix"
            GSL_FOUND="true"
            break
        fi
    done
    if [ "$GSL_FOUND" != "true" ]; then
        echo "ERROR: GSL headers not found, and this invocation was told not to install them." >&2
        echo "       arcticpy is a C++ sdist and cannot build without them. Install with:" >&2
        echo "         Debian/Ubuntu:  sudo apt-get install -y libgsl-dev" >&2
        echo "         macOS:          brew install gsl" >&2
        echo "       Then re-run. (Searched: $GSL_PREFIXES — override with ARCTICPY_GSL_PREFIXES.)" >&2
        exit 1
    fi
fi

echo "==> Installing arcticpy build dependencies"
# --no-build-isolation reads NOTHING from arcticpy's build-system requires, so
# these must be present before the build starts.
"$PYTHON" -m pip install --upgrade pip setuptools wheel
"$PYTHON" -m pip install numpy cython

echo "==> Installing arcticpy runtime dependencies"
# --no-deps suppresses these, but arcticpy/read_noise.py imports both at import
# time (`from scipy.optimize import curve_fit`, `import matplotlib as mpl`) and
# __init__.py imports read_noise. So `import arcticpy` fails without them —
# which is why the verify step below, and every downstream `import autocti`,
# needs them present.
#
# The CTI stack installs scipy and matplotlib anyway as ordinary dependencies;
# naming them here is what makes this script verifiable on its own rather than
# only inside an already-built stack.
"$PYTHON" -m pip install scipy matplotlib

echo "==> Building and installing arcticpy ${ARCTICPY_VERSION}"
"$PYTHON" -m pip install "arcticpy==${ARCTICPY_VERSION}" \
    --no-build-isolation --no-deps

echo "==> Verifying arcticpy imports"
# Assert here rather than letting a broken build surface much later as a
# confusing `import autocti` failure in an unrelated job.
#
# NOTE: arcticpy exposes no __version__ attribute — the obvious
# `import arcticpy; print(arcticpy.__version__)` raises AttributeError even on a
# perfectly good install. The distribution metadata is the supported way to ask.
VERSION="$("$PYTHON" -c 'import arcticpy; from importlib.metadata import version; print(version("arcticpy"))')"
echo "arcticpy $VERSION imported successfully"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "version=$VERSION" >> "$GITHUB_OUTPUT"
fi
