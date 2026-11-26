#!/usr/bin/env bash
#
# install_arcticpy.sh — THE canonical arcticpy install for the whole organism.
#
# This file is the single home of the recipe and of the organism's arcticpy pin.
#
# STOPGAP (2026-11-26, PyAutoHeart#372): the pin is currently a GitHub COMMIT,
# not a PyPI release. arcticpy 2.7 (numpy 2 / Cython 3 support, upstream PRs
# #73 + #75) is merged into jkeger/arctic main but cannot be published to PyPI
# for now, so the default builds it from source at a pinned commit
# (ARCTICPY_SOURCE below). That needs `git` on PATH — present on GitHub-hosted
# runners and on Colab; a local `pyauto-heart smoke` run now needs git and
# github.com access instead of PyPI. To REVERT once 2.7 is on PyPI: set the ARCTICPY_VERSION
# default back to a PyPI version (`2.7`), and the git source stops being used.
# See the Mind draft
# draft/maintenance/pyautoheart/arcticpy_pin_bump_after_upstream_release.md.
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
#   * its own requirements (up to 2.6) downgrade numpy below 2.0, which breaks
#     a modern PyAuto stack. 2.7's metadata now allows numpy<3, but this
#     stopgap keeps --no-deps anyway so the recipe stays the proven one.
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
#   ARCTICPY_VERSION      PyPI arcticpy version to build. Default EMPTY,
#                         which means "build ARCTICPY_SOURCE instead". A value
#                         (e.g. 2.6, or 2.7 once published) installs
#                         `arcticpy==<value>` from PyPI.
#   ARCTICPY_SOURCE       pip URL used when ARCTICPY_VERSION is empty. Default
#                         git+https://github.com/jkeger/arctic@<pinned commit>
#                         (2.7). Together with ARCTICPY_VERSION, THE single pin
#                         for the organism — bump it HERE.
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
#                         extracted somewhere unprivileged (see the CTI
#                         library's AGENTS.md, section arcticpy).
#   PYTHON                Interpreter to install into. Default `python`, which
#                         is correct both on a runner and inside an activated
#                         venv; heart/smoke.py passes its isolated venv's
#                         interpreter explicitly.
#
# On success, prints the installed version and — when $GITHUB_OUTPUT is set —
# writes `version=<v>` to it, so the action's `version` output still works.

set -euo pipefail

ARCTICPY_VERSION="${ARCTICPY_VERSION:-}"
ARCTICPY_SOURCE="${ARCTICPY_SOURCE:-git+https://github.com/jkeger/arctic@6f5153582aca00b7b0cc11ab04b2c58a7502400d}"
if [ -n "$ARCTICPY_VERSION" ]; then
    ARCTICPY_SPEC="arcticpy==${ARCTICPY_VERSION}"
else
    ARCTICPY_SPEC="arcticpy @ ${ARCTICPY_SOURCE}"
fi
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
# these must be present before the build starts. 2.7 builds against numpy>=2,
# Cython>=3 and setuptools>=64; the floors make an older Cython already in the
# environment get upgraded rather than silently reused.
"$PYTHON" -m pip install --upgrade pip setuptools wheel
"$PYTHON" -m pip install "numpy>=2" "cython>=3"

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

echo "==> Building and installing ${ARCTICPY_SPEC}"
# --no-cache-dir: the wheel built here links the MACHINE's libgsl by soname, so
# a copy cached in ~/.cache/pip is only valid on the image that built it. CI
# caches that directory across runs (smoke-tests.yml) with no image identity in
# the key; when GitHub moved ubuntu-24.04 (libgsl.so.27) -> ubuntu-26.04
# (libgsl.so.28) pip restored the old wheel and `import arcticpy` failed with
# "libgsl.so.27: cannot open shared object file" (PyAutoHeart#330). Always
# building fresh costs ~15 s and makes a system-library change impossible to
# mask. Only this install opts out: the others are image-independent wheels.
"$PYTHON" -m pip install "$ARCTICPY_SPEC" \
    --no-build-isolation --no-deps --no-cache-dir

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
