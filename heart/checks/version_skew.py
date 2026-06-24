"""heart/checks/version_skew.py — workspace pin vs installed library version.

PyAutoBuild's ``verify_workspace_versions.sh`` blocks a release if any
workspace's pinned version is AHEAD of its installed library — but that only
runs at ``pre_build`` time, so day-to-day the skew is invisible. This check
makes it continuous.

For each polled workspace that carries a pin, it resolves:

- the **pinned** version: ``config/general.yaml`` → ``version.workspace_version``,
  falling back to a ``version.txt`` at the workspace root (same precedence as
  ``verify_workspace_versions.sh``);
- the **installed** library version: by regex-reading ``__version__`` from the
  matching library's ``<pkg>/__init__.py`` source — never importing the heavy
  library (keeps the tick cheap).

Versions are ``YYYY.M.D.B`` tuples; the comparison yields MATCH / AHEAD /
BEHIND / BAD. Two further statuses mirror the hard-fail conditions of
``verify_workspace_versions.sh`` so Heart is the single authoritative readiness
gate:

- **MISMATCH** — ``config/general.yaml`` and ``version.txt`` both exist and
  disagree (a release-blocking inconsistency in ``verify_workspace_versions.sh``).
- **UNKNOWN** — the library ``__init__.py`` could not be read (the workspace is
  pinned but the library isn't checked out); surfaced as caution, never a hard
  block, matching the script's ``SKIP (cannot import …)`` behaviour.

The result lands at ``$HEART_STATE_DIR/version_skew.json``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"
_p3 = Path(__file__).resolve().parents[3]
PYAUTO_ROOT = _p3 if _p3.name == "PyAutoLabs" else Path.home() / "Code" / "PyAutoLabs"
HEART_STATE_DIR = Path(
    os.environ.get("HEART_STATE_DIR")
    or os.environ.get("PULSE_STATE_DIR")
    or Path.home() / ".pyauto-heart"
)

_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

# workspace name -> (library repo dir, package dir holding __init__.py)
WORKSPACE_LIBRARY = {
    "autofit_workspace": ("PyAutoFit", "autofit"),
    "autogalaxy_workspace": ("PyAutoGalaxy", "autogalaxy"),
    "autolens_workspace": ("PyAutoLens", "autolens"),
    "HowToFit": ("PyAutoFit", "autofit"),
    "HowToGalaxy": ("PyAutoGalaxy", "autogalaxy"),
    "HowToLens": ("PyAutoLens", "autolens"),
    "euclid_strong_lens_modeling_pipeline": ("PyAutoLens", "autolens"),
    "autolens_assistant": ("PyAutoLens", "autolens"),
}


def read_library_version(repo: str, pkg: str, root: Path = PYAUTO_ROOT) -> str | None:
    """Regex-read ``__version__`` from ``<repo>/<pkg>/__init__.py`` (no import)."""
    init = root / repo / pkg / "__init__.py"
    try:
        m = _VERSION_RE.search(init.read_text())
    except OSError:
        return None
    return m.group(1) if m else None


def read_workspace_pin_sources(
    workspace: str, root: Path = PYAUTO_ROOT
) -> tuple[str | None, str | None]:
    """Return ``(general_yaml_version, version_txt)`` — either may be None.

    Kept separate from :func:`read_workspace_pin` so callers that need to detect
    a general.yaml ↔ version.txt disagreement (MISMATCH) can see both sources.
    """
    ws = root / workspace
    yaml_v: str | None = None
    general = ws / "config" / "general.yaml"
    if general.is_file():
        try:
            data = yaml.safe_load(general.read_text()) or {}
            pin = (data.get("version") or {}).get("workspace_version")
            if pin:
                yaml_v = str(pin).strip()
        except yaml.YAMLError:
            pass
    txt_v: str | None = None
    vtxt = ws / "version.txt"
    if vtxt.is_file():
        t = vtxt.read_text().strip()
        if t:
            txt_v = t
    return yaml_v, txt_v


def read_workspace_pin(workspace: str, root: Path = PYAUTO_ROOT) -> str | None:
    """Pinned version: config/general.yaml:version.workspace_version, then version.txt."""
    yaml_v, txt_v = read_workspace_pin_sources(workspace, root)
    return yaml_v if yaml_v is not None else txt_v


def _tuple(v: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in v.split("."))
    except (ValueError, AttributeError):
        return None


def compare(pinned: str | None, installed: str | None) -> str:
    """MATCH / AHEAD (pinned > installed) / BEHIND (pinned < installed) / BAD."""
    pt, it = _tuple(pinned or ""), _tuple(installed or "")
    if pt is None or it is None:
        return "BAD"
    if pt == it:
        return "MATCH"
    return "AHEAD" if pt > it else "BEHIND"


def run(root: Path = PYAUTO_ROOT) -> dict[str, Any]:
    workspaces = []
    for workspace, (repo, pkg) in WORKSPACE_LIBRARY.items():
        yaml_v, txt_v = read_workspace_pin_sources(workspace, root)
        if yaml_v is None and txt_v is None:
            continue  # no pin (e.g. *_test workspaces) → not a skew candidate
        installed = read_library_version(repo, pkg, root)

        # general.yaml ↔ version.txt disagreement is the same release-blocking
        # condition verify_workspace_versions.sh fails on.
        if yaml_v is not None and txt_v is not None and yaml_v != txt_v:
            workspaces.append(
                {
                    "workspace": workspace,
                    "library": repo,
                    "pinned": yaml_v,
                    "version_txt": txt_v,
                    "installed": installed,
                    "status": "MISMATCH",
                }
            )
            continue

        pinned = yaml_v if yaml_v is not None else txt_v
        # Library not checked out → cannot compare. Caution, not a hard block
        # (mirrors the script's "SKIP (cannot import <pkg>)").
        status = "UNKNOWN" if installed is None else compare(pinned, installed)
        workspaces.append(
            {
                "workspace": workspace,
                "library": repo,
                "pinned": pinned,
                "installed": installed,
                "status": status,
            }
        )
    result = {"workspaces": workspaces}
    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(HEART_STATE_DIR / "version_skew.json", result)
    return result


def main(argv: list[str]) -> int:
    result = run()
    sys.path.insert(0, str(HEART_HOME))
    from heart.heart_color import c_ok, c_warn, c_fail, c_info, c_meta, glyph_ok, glyph_warn, glyph_fail

    workspaces = result["workspaces"]
    ahead = [w for w in workspaces if w["status"] == "AHEAD"]
    behind = [w for w in workspaces if w["status"] == "BEHIND"]
    mismatch = [w for w in workspaces if w["status"] == "MISMATCH"]
    bad = [w for w in workspaces if w["status"] == "BAD"]
    blocking = ahead + mismatch + bad  # release-blocking statuses
    if blocking:
        glyph = glyph_fail()
        parts = []
        if ahead:
            parts.append(c_fail(f"{len(ahead)} ahead"))
        if mismatch:
            parts.append(c_fail(f"{len(mismatch)} mismatch"))
        if bad:
            parts.append(c_warn(f"{len(bad)} bad"))
        label = " ".join(parts)
    elif behind:
        glyph = glyph_warn()
        label = c_warn(f"{len(behind)} behind")
    else:
        glyph = glyph_ok()
        label = c_ok(f"{len(workspaces)} in sync")
    print(f"{glyph} {c_info('version_skew')} {label} {c_meta(f'({len(workspaces)} pinned)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
