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

verify_install_unpinned_refusal() {
    local pip_output="$1"
    local package="$2"

    # Tombstone path (the live one): releases at or below 2026.7.29.1 declare
    # Requires-Python >=3.9 and stay valid candidates forever, so an unpinned
    # install below the floor is refused by 2026.7.29.1.post1 raising during
    # metadata preparation. That text arrives from the build subprocess, not as
    # a pip resolver diagnostic, so it needs its own classifier.
    if printf '%s\n' "$pip_output" | grep -qE \
            "$package requires Python 3\.[0-9]+ or later" \
        && printf '%s\n' "$pip_output" | grep -qE \
            "you are running Python 3\.[0-9]+"; then
        return 0
    fi

    # Retraction path: if the sub-floor catalogue is ever withdrawn, pip runs
    # out of candidates instead and says so. Both shapes mean refused; anything
    # else (network, resolver, dependency failure) is not floor evidence.
    if printf '%s\n' "$pip_output" | grep -qi \
            "Ignored the following versions that require a different python version:" \
        && printf '%s\n' "$pip_output" | grep -qiE \
            "No matching distribution found for $package"; then
        return 0
    fi

    return 1
}
