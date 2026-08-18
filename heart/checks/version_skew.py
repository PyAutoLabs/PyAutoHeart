"""heart/checks/version_skew.py — workspace compatibility floor vs newest release.

Under the pre-2026-07 model this check compared a workspace's recorded pin
(``config/general.yaml`` → ``version.workspace_version`` / ``version.txt``)
against the library ``__init__.py`` ``__version__`` stamp. That model is gone:
releases no longer commit the stamp or the pin back to ``main`` (PyAutoConf#119 /
PyAutoBuild#121 — the daily "Update version to X" commits were the CI-storm /
cron-pause engine), so *both* artifacts are frozen and the old check was inert —
permanently MATCH on stale values, unfailable by any release.

The live invariant now is the **compatibility floor**: each workspace records
``config/general.yaml`` → ``version.minimum_library_version`` — the oldest
library release whose API its scripts require — and users must be able to install
a release that satisfies it. So this check compares:

- the **floor**: ``version.minimum_library_version`` for the workspace;
- the **newest release**: the highest ``YYYY.M.D.B`` git tag on the mapped
  library checkout (read with ``git tag`` — no library import, no network, cheap
  enough for the <30s tick).

Statuses:

- **UNSATISFIABLE** — floor > newest release: no released version satisfies the
  floor, so a fresh ``pip install`` cannot pair with the workspace. Release-
  blocking (this is the invariant nothing guarded before: the old leg was
  unfailable by releases).
- **OK** — floor <= newest release.
- **BAD** — floor or newest release is unparseable.
- **UNKNOWN** — the library isn't checked out / carries no release tags, so the
  newest release can't be resolved; surfaced as caution, never a hard block.

The yank gap is owned by the **deep PyPI leg** (``--pypi``): git tags cannot see
a release being *yanked* on PyPI after the fact, so ``--pypi`` asks the PyPI
JSON API whether each floor still names an installable (non-yanked) release and
whether *any* installable release satisfies it. Network — never part of the
tick; run it on demand or from a nightly. Its statuses:

- **UNSATISFIABLE** — no installable release >= floor exists on PyPI (every
  candidate yanked): same defect class as the tag-based UNSATISFIABLE.
- **FLOOR_YANKED** — the floor version itself is yanked/absent but a newer
  installable release satisfies it; floors are ``>=`` so installs still
  resolve — warn, fix by bumping the floor.
- **OK** / **BAD** / **UNKNOWN** — as above; UNKNOWN covers PyPI unreachable
  (offline is caution, never a false hard block).

An informational "floor lags far behind newest" signal is a possible future add.

The tick result lands at ``$HEART_STATE_DIR/version_skew.json``; the ``--pypi``
leg at ``version_skew_pypi.json`` (a sibling file, so the tick never clobbers
on-demand evidence).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
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
    or Path.home() / ".pyauto-heart"
)

_TAG_RE = re.compile(r"^\d{4}\.\d+\.\d+\.\d+$")


def workspace_library(config_path: Path | str = CONFIG_PATH) -> dict[str, tuple[str, str]]:
    """workspace name -> (library repo dir, package dir), from the policy
    file's ``version_skew`` block. Strict: a missing block is a config bug
    and fails loudly rather than silently checking nothing."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    block = cfg["version_skew"]
    return {ws: (spec["library"], spec["package"]) for ws, spec in block.items()}


def read_workspace_floor(workspace: str, root: Path = PYAUTO_ROOT) -> str | None:
    """The compatibility floor: ``config/general.yaml`` →
    ``version.minimum_library_version``. Returns None when absent (the
    workspace is not a floor candidate)."""
    general = root / workspace / "config" / "general.yaml"
    if not general.is_file():
        return None
    try:
        data = yaml.safe_load(general.read_text()) or {}
    except yaml.YAMLError:
        return None
    floor = (data.get("version") or {}).get("minimum_library_version")
    return str(floor).strip() if floor else None


def _newest_version(tags: list[str]) -> str | None:
    """Highest ``YYYY.M.D.B`` tag by numeric tuple, or None if none match."""
    versions = [t.strip() for t in tags if _TAG_RE.match(t.strip())]
    if not versions:
        return None
    return max(versions, key=lambda v: tuple(int(p) for p in v.split(".")))


def newest_release_tag(repo: str, root: Path = PYAUTO_ROOT) -> str | None:
    """Newest ``YYYY.M.D.B`` release tag on the library checkout (``git tag``),
    or None when the repo isn't a checkout or carries no release tags. No
    network — reads local tags only, so it stays inside the tick budget."""
    repo_dir = root / repo
    if not (repo_dir / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "tag"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return _newest_version(proc.stdout.splitlines())


def _tuple(v: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in v.split("."))
    except (ValueError, AttributeError):
        return None


def compare_floor(floor: str | None, newest: str | None) -> str:
    """UNSATISFIABLE (floor > newest release) / OK (floor <= newest) / BAD."""
    ft, nt = _tuple(floor or ""), _tuple(newest or "")
    if ft is None or nt is None:
        return "BAD"
    return "UNSATISFIABLE" if ft > nt else "OK"


def run(root: Path = PYAUTO_ROOT) -> dict[str, Any]:
    workspaces = []
    for workspace, (repo, _pkg) in workspace_library().items():
        floor = read_workspace_floor(workspace, root)
        if floor is None:
            continue  # no floor recorded → not a candidate
        newest = newest_release_tag(repo, root)
        # Library not checked out / no tags → cannot resolve the newest release.
        # Caution, not a hard block.
        status = "UNKNOWN" if newest is None else compare_floor(floor, newest)
        workspaces.append(
            {
                "workspace": workspace,
                "library": repo,
                "floor": floor,
                "newest_release": newest,
                "status": status,
            }
        )
    return {"workspaces": workspaces}


# --------------------------------------------------------------------------- #
# Deep PyPI leg (``--pypi``) — yank-awareness. Network; never run from the tick.
# --------------------------------------------------------------------------- #

PYPI_URL = "https://pypi.org/pypi/{package}/json"
PYPI_TIMEOUT_S = 10


def fetch_pypi_releases(package: str) -> dict[str, list] | None:
    """The package's ``releases`` map from the PyPI JSON API, or None when the
    API is unreachable/unparseable — offline must degrade to UNKNOWN, never a
    false hard block."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            PYPI_URL.format(package=package), timeout=PYPI_TIMEOUT_S
        ) as resp:
            data = json.load(resp)
    except Exception:
        return None
    releases = data.get("releases")
    return releases if isinstance(releases, dict) else None


def _installable(files: list) -> bool:
    """A release is installable iff at least one of its files is not yanked
    (PyPI marks yank per file; a fileless release installs nothing)."""
    return any(isinstance(f, dict) and not f.get("yanked") for f in files or [])


def pypi_floor_status(floor: str | None, releases: dict[str, list] | None) -> str:
    """OK / FLOOR_YANKED / UNSATISFIABLE / UNKNOWN / BAD for one floor vs PyPI.

    Floors are ``>=`` bounds, so a yanked floor with a newer installable
    release still resolves at install time — that is FLOOR_YANKED (fix by
    bumping the floor to an installable release), not a hard block. No
    installable release >= floor at all is the same defect class as the
    tag-based UNSATISFIABLE.
    """
    if releases is None:
        return "UNKNOWN"
    ft = _tuple(floor or "")
    if ft is None:
        return "BAD"
    installable = {
        v.strip()
        for v, files in releases.items()
        if _TAG_RE.match(v.strip()) and _installable(files)
    }
    if not any(_tuple(v) >= ft for v in installable):
        return "UNSATISFIABLE"
    return "OK" if (floor or "").strip() in installable else "FLOOR_YANKED"


def run_pypi(root: Path = PYAUTO_ROOT) -> dict[str, Any]:
    """Side-effect-free like run(): one PyPI fetch per distinct package, one
    entry per floored workspace."""
    releases_by_package: dict[str, dict[str, list] | None] = {}
    workspaces = []
    for workspace, (repo, pkg) in workspace_library().items():
        floor = read_workspace_floor(workspace, root)
        if floor is None:
            continue  # no floor recorded → not a candidate
        if pkg not in releases_by_package:
            releases_by_package[pkg] = fetch_pypi_releases(pkg)
        workspaces.append(
            {
                "workspace": workspace,
                "library": repo,
                "package": pkg,
                "floor": floor,
                "status": pypi_floor_status(floor, releases_by_package[pkg]),
            }
        )
    return {"workspaces": workspaces}


def main(argv: list[str]) -> int:
    pypi = "--pypi" in argv
    result = run_pypi() if pypi else run()
    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    # Persist only here, at the tick/CLI entrypoint — run()/run_pypi() are
    # side-effect-free so library callers (and the test suite) can never
    # clobber live state. The --pypi leg gets its own sidecar so the tick's
    # version_skew.json rewrite never clobbers on-demand PyPI evidence.
    name = "version_skew_pypi.json" if pypi else "version_skew.json"
    state.atomic_write_json(HEART_STATE_DIR / name, result)

    from heart.heart_color import c_ok, c_warn, c_fail, c_info, c_meta, glyph_ok, glyph_warn, glyph_fail

    workspaces = result["workspaces"]
    unsatisfiable = [w for w in workspaces if w["status"] == "UNSATISFIABLE"]
    bad = [w for w in workspaces if w["status"] == "BAD"]
    yanked = [w for w in workspaces if w["status"] == "FLOOR_YANKED"]
    unknown = [w for w in workspaces if w["status"] == "UNKNOWN"]
    blocking = unsatisfiable + bad  # release-blocking statuses
    if blocking:
        glyph = glyph_fail()
        parts = []
        if unsatisfiable:
            parts.append(c_fail(f"{len(unsatisfiable)} unsatisfiable"))
        if bad:
            parts.append(c_warn(f"{len(bad)} bad"))
        label = " ".join(parts)
    elif yanked or unknown:
        glyph = glyph_warn()
        parts = []
        if yanked:
            parts.append(c_warn(f"{len(yanked)} floor yanked"))
        if unknown:
            parts.append(c_warn(f"{len(unknown)} unknown"))
        label = " ".join(parts)
    else:
        glyph = glyph_ok()
        label = c_ok(f"{len(workspaces)} floors satisfiable")
    check_name = "version_skew --pypi" if pypi else "version_skew"
    print(f"{glyph} {c_info(check_name)} {label} {c_meta(f'({len(workspaces)} floors)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
