"""heart/noise.py — classify `git status --porcelain` entries into genuine
source drift vs regenerated-artifact noise, and emit the repo_state sidecar.

Many PyAuto workspaces commit generated dataset artifacts (`*.fits`,
`tracer.json`, regenerated `README.md`, …) that get rewritten every time a
build/simulator script runs. They perpetually show as dirty, which would keep
every such repo permanently yellow. This module splits the porcelain into
``real`` (source changes worth acting on) and ``noise`` (regenerated
artifacts), using the ``noise_globs`` list from ``config/repos.yaml``.

A path is NOISE if it matches any noise glob OR is an untracked directory
(porcelain ``??`` with a trailing ``/`` — e.g. a generated ``results/…/sma/``
output dir). Everything else — including modified ``.py`` under a ``results/``
folder — is REAL. fnmatch is used for matching, where ``*`` crosses ``/``.

Run as a module (``python3 -m heart.noise``) it reads porcelain on stdin,
classifies, writes the per-repo ``repo_state`` sidecar atomically, and prints
``<real> <noise>`` to stdout for the bash caller's log line.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import sys
from pathlib import Path
from typing import Any

import yaml

from heart import state

HEART_HOME = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = HEART_HOME / "config" / "repos.yaml"

# Cap how many file paths we persist per repo so a 150-dirty workspace can't
# bloat the sidecar; the counts are always exact, only the listing is capped.
MAX_LISTED = 500


def load_noise_globs(config_path: Path | str = DEFAULT_CONFIG) -> list[str]:
    """Return the ``noise_globs`` list from repos.yaml (empty if absent)."""
    p = Path(config_path)
    if not p.is_file():
        return []
    cfg = yaml.safe_load(p.read_text()) or {}
    globs = cfg.get("noise_globs", []) or []
    return [str(g) for g in globs]


def _is_noise(code: str, path: str, noise_globs: list[str]) -> bool:
    if any(fnmatch.fnmatch(path, g) for g in noise_globs):
        return True
    # Untracked directory (git porcelain appends a trailing slash) — a
    # generated output dir, not source the user authored.
    if code.strip() == "??" and path.endswith("/"):
        return True
    return False


def split_lines(
    porcelain_lines: list[str], noise_globs: list[str]
) -> tuple[list[str], list[str]]:
    """Split porcelain lines into (real_lines, noise_lines), codes intact.

    Each line is the raw ``git status --porcelain`` form: a two-char status
    code, a space, then the path (which may itself contain spaces). Rename
    entries (``R  old -> new``) are classified by their destination path.
    The full lines are returned so callers can still distinguish untracked
    (``??``) from modified entries.
    """
    real: list[str] = []
    noise: list[str] = []
    for line in porcelain_lines:
        if not line.strip():
            continue
        code = line[:2]
        path = line[3:] if len(line) > 3 else ""
        if not path:
            continue
        if " -> " in path:  # rename/copy: keep the destination
            path = path.split(" -> ", 1)[1]
        if _is_noise(code, path, noise_globs):
            noise.append(line)
        else:
            real.append(line)
    return real, noise


def _line_path(line: str) -> str:
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path


def classify_dirty(
    porcelain_lines: list[str], noise_globs: list[str]
) -> tuple[list[str], list[str]]:
    """Split porcelain lines into (real_paths, noise_paths) — see split_lines."""
    real_lines, noise_lines = split_lines(porcelain_lines, noise_globs)
    return [_line_path(l) for l in real_lines], [_line_path(l) for l in noise_lines]


def build_sidecar(
    *,
    name: str,
    group: str,
    branch: str,
    ahead: int,
    behind: int,
    upstream: str,
    ts: str,
    porcelain_lines: list[str],
    noise_globs: list[str],
) -> dict[str, Any]:
    real, noise = classify_dirty(porcelain_lines, noise_globs)
    return {
        "name": name,
        "group": group,
        "present": True,
        "branch": branch,
        "dirty_files": len(real) + len(noise),  # total, kept for back-compat
        "dirty_real": len(real),
        "dirty_noise": len(noise),
        "real_files": real[:MAX_LISTED],
        "noise_files": noise[:MAX_LISTED],
        "ahead": ahead,
        "behind": behind,
        "upstream": upstream,
        "ts": ts,
    }


def run_batch(in_dir: Path, real_out: Path, config: str) -> int:
    """Classify one porcelain file per repo: ``<in_dir>/<name>`` holds that
    repo's ``git status --porcelain`` output. Real-only lines are written to
    ``<real_out>/<name>`` and one ``<name>\\t<real_mod>\\t<real_untr>\\t<noise>``
    line per repo goes to stdout (untracked = ``??`` code). No sidecar is
    written — this is the display path (health_sync.sh), not the census.
    """
    noise_globs = load_noise_globs(config)
    real_out.mkdir(parents=True, exist_ok=True)
    for f in sorted(in_dir.iterdir()):
        if not f.is_file():
            continue
        real_lines, noise_lines = split_lines(f.read_text().splitlines(), noise_globs)
        (real_out / f.name).write_text(
            "\n".join(real_lines) + ("\n" if real_lines else "")
        )
        real_untr = sum(1 for l in real_lines if l[:2] == "??")
        print(f"{f.name}\t{len(real_lines) - real_untr}\t{real_untr}\t{len(noise_lines)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="heart.noise")
    ap.add_argument("--name")
    ap.add_argument("--group", default="")
    ap.add_argument("--branch", default="")
    ap.add_argument("--ahead", type=int, default=0)
    ap.add_argument("--behind", type=int, default=0)
    ap.add_argument("--upstream", default="")
    ap.add_argument("--ts", default="")
    ap.add_argument("--out", help="sidecar JSON path to write")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--batch", help="dir of per-repo porcelain files (no sidecar mode)")
    ap.add_argument("--real-out", help="dir to write per-repo real-only porcelain (with --batch)")
    ns = ap.parse_args(argv)

    if ns.batch:
        if not ns.real_out:
            ap.error("--batch requires --real-out")
        return run_batch(Path(ns.batch), Path(ns.real_out), ns.config)
    if not ns.name or not ns.out:
        ap.error("--name and --out are required (unless using --batch)")

    ts = ns.ts or datetime.datetime.now(datetime.timezone.utc).isoformat()
    porcelain_lines = sys.stdin.read().splitlines()
    noise_globs = load_noise_globs(ns.config)

    sidecar = build_sidecar(
        name=ns.name,
        group=ns.group,
        branch=ns.branch,
        ahead=ns.ahead,
        behind=ns.behind,
        upstream=ns.upstream,
        ts=ts,
        porcelain_lines=porcelain_lines,
        noise_globs=noise_globs,
    )
    state.atomic_write_json(Path(ns.out), sidecar)
    print(f"{sidecar['dirty_real']} {sidecar['dirty_noise']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
