#!/usr/bin/env bash
# Pure helpers shared by verify_install.sh and its fast classifier tests.

verify_install_requires_python_rejection() {
    local pip_output="$1"
    local version="$2"

    # Local-wheel path: pip has built a candidate and names the package in its
    # candidate-level Requires-Python diagnostic.
    if printf '%s\n' "$pip_output" | grep -qiE \
            "package ['\"]?autolens['\"]? requires a different python(:| version)[^[:cntrl:]]*>= ?3\\.12"; then
        return 0
    fi

    # Index path: PyPI/TestPyPI exposes data-requires-python, so pip filters the
    # link before candidacy. Its diagnostic splits the constraint and requested
    # package across lines; require the exact pinned version in both places so a
    # network/resolver/dependency failure cannot be mistaken for floor evidence.
    if printf '%s\n' "$pip_output" | grep -qi \
            "Ignored the following versions that require a different python version:" \
        && printf '%s\n' "$pip_output" | grep -Fqi \
            "$version Requires-Python >=3.12" \
        && printf '%s\n' "$pip_output" | grep -Fqi \
            "autolens==$version"; then
        return 0
    fi

    return 1
}

verify_install_versions_equivalent() {
    local pybin="$1"
    local left="$2"
    local right="$3"

    "$pybin" - "$left" "$right" <<'PY'
import sys
from pip._vendor.packaging.version import InvalidVersion, Version

try:
    equivalent = Version(sys.argv[1]) == Version(sys.argv[2])
except InvalidVersion:
    equivalent = False
raise SystemExit(0 if equivalent else 1)
PY
}
