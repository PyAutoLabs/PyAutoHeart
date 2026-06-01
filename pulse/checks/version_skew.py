"""pulse/checks/version_skew.py — workspace pin vs installed library version.

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
BEHIND / BAD. The result lands at ``$PULSE_STATE_DIR/version_skew.json``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

PULSE_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = PULSE_HOME / "config" / "repos.yaml"
_p3 = Path(__file__).resolve().parents[3]
PYAUTO_ROOT = _p3 if _p3.name == "PyAutoLabs" else Path.home() / "Code" / "PyAutoLabs"
PULSE_STATE_DIR = Path.home() / ".pyauto-pulse"

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
}


def read_library_version(repo: str, pkg: str, root: Path = PYAUTO_ROOT) -> str | None:
    """Regex-read ``__version__`` from ``<repo>/<pkg>/__init__.py`` (no import)."""
    init = root / repo / pkg / "__init__.py"
    try:
        m = _VERSION_RE.search(init.read_text())
    except OSError:
        return None
    return m.group(1) if m else None


def read_workspace_pin(workspace: str, root: Path = PYAUTO_ROOT) -> str | None:
    """Pinned version: config/general.yaml:version.workspace_version, then version.txt."""
    ws = root / workspace
    general = ws / "config" / "general.yaml"
    if general.is_file():
        try:
            data = yaml.safe_load(general.read_text()) or {}
            pin = (data.get("version") or {}).get("workspace_version")
            if pin:
                return str(pin)
        except yaml.YAMLError:
            pass
    vtxt = ws / "version.txt"
    if vtxt.is_file():
        t = vtxt.read_text().strip()
        if t:
            return t
    return None


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
        pinned = read_workspace_pin(workspace, root)
        if pinned is None:
            continue  # no pin (e.g. *_test workspaces) → not a skew candidate
        installed = read_library_version(repo, pkg, root)
        workspaces.append(
            {
                "workspace": workspace,
                "library": repo,
                "pinned": pinned,
                "installed": installed,
                "status": compare(pinned, installed),
            }
        )
    result = {"workspaces": workspaces}
    PULSE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    (PULSE_STATE_DIR / "version_skew.json").write_text(json.dumps(result, indent=2))
    return result


def main(argv: list[str]) -> int:
    result = run()
    sys.path.insert(0, str(PULSE_HOME))
    from pulse.pulse_color import c_ok, c_warn, c_fail, c_info, c_meta, glyph_ok, glyph_warn, glyph_fail

    workspaces = result["workspaces"]
    ahead = [w for w in workspaces if w["status"] == "AHEAD"]
    behind = [w for w in workspaces if w["status"] == "BEHIND"]
    bad = [w for w in workspaces if w["status"] == "BAD"]
    if ahead or bad:
        glyph = glyph_fail()
        label = c_fail(f"{len(ahead)} ahead") + (f" {c_warn(str(len(bad)) + ' bad')}" if bad else "")
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
