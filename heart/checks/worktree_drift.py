"""heart/checks/worktree_drift.py — task worktrees vs the Mind's claims.

Compares the worktree dirs on disk (``$PYAUTO_WT_ROOT`` plus any claimed path
outside it) against the ``worktree:`` claims in PyAutoMind's ``active.md`` and
``parked.md``, and categorises:

  ORPHAN           on disk, claimed by neither active.md nor parked.md
  PARKED           on disk, claimed by parked.md (legitimate — not drift)
  MISSING          claimed by active.md but the path does not exist
  DIRTY            a REAL task worktree (never a symlink) with uncommitted work
  CANONICAL DIRTY  a canonical checkout that task worktrees symlink to, itself
                   dirty — counted ONCE per checkout, never per worktree

The symlink distinction is the point: ``worktree_create`` symlinks every
non-claimed repo back to ``~/Code/PyAutoLabs/<repo>``, so following symlinks
counted one dirty canonical repo once per task worktree that linked it (the
"66 dirty" totals). A user's dirty canonical checkout is their own working
state, not task drift — it gets its own category and a yellow, not a red.

Claims are path-tested directly (``~`` expanded), wherever they point — a
claim under ``PyAutoLabs/.codex-worktrees/`` is tracked like any other, not
reported missing because it isn't under the wt root. Claim values that are not
path-shaped (parked.md carries prose like ``worktree: none (…)``) are ignored.

Discovery under the wt root skips **hidden** directories: the wt root is a plain
directory other tools write into (a JetBrains project dir lands at
``<wt root>/.idea``), and such a dir is claimed by nobody, so it was reported as a
permanent, unfixable ORPHAN. Only *discovery* is filtered — an explicit claim on
a dotted path is still honoured, which is why ``.codex-worktrees`` claims keep
working.

Monitoring-only: surfaced on the dashboard/status, never a readiness reason.
``worktree_drift.sh`` is a thin shim over this module for tick.sh.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HEART_HOME = Path(__file__).resolve().parents[2]
_p3 = Path(__file__).resolve().parents[3]
PYAUTO_ROOT = _p3 if _p3.name == "PyAutoLabs" else Path.home() / "Code" / "PyAutoLabs"
PYAUTO_WT_ROOT = Path(os.environ.get("PYAUTO_WT_ROOT") or Path.home() / "Code" / "PyAutoLabs-wt")
ACTIVE_MD = PYAUTO_ROOT / "PyAutoMind" / "active.md"
PARKED_MD = PYAUTO_ROOT / "PyAutoMind" / "parked.md"
HEART_STATE_DIR = Path(os.environ.get("HEART_STATE_DIR") or Path.home() / ".pyauto-heart")


def _claims(registry_md: Path) -> list[dict[str, str]]:
    """``[{task, path}]`` from a registry file's ``worktree:`` lines.

    Only path-shaped values count (``/…`` or ``~…``): parked.md legitimately
    records prose like ``worktree: none (pushed to origin; local removed)``."""
    if not registry_md.is_file():
        return []
    claims = []
    current_task = None
    for line in registry_md.read_text().splitlines():
        m = re.match(r"^## (\S+)", line)
        if m:
            current_task = m.group(1)
        elif current_task:
            wm = re.search(r"worktree:\s*(\S+)", line)
            if wm and wm.group(1)[0] in "/~":
                claims.append(
                    {"task": current_task, "path": os.path.expanduser(wm.group(1))}
                )
    return claims


def _dirty_file_count(repo: Path) -> int:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return 0
    return len(res.stdout.strip().splitlines()) if res.stdout.strip() else 0


def scan(
    wt_root: Path = PYAUTO_WT_ROOT,
    active_md: Path = ACTIVE_MD,
    parked_md: Path = PARKED_MD,
) -> dict[str, Any]:
    active_claims = _claims(active_md)
    parked_claims = _claims(parked_md)
    active_paths = {c["path"] for c in active_claims}
    parked_paths = {c["path"] for c in parked_claims}

    # Worktree dirs on disk: everything under the wt root, plus any claimed
    # path that exists elsewhere (e.g. .codex-worktrees/<task>).
    on_disk: list[dict[str, Any]] = []
    seen: set[str] = set()

    def note(path: Path, name: str) -> None:
        key = str(path)
        if key in seen or not path.is_dir():
            return
        seen.add(key)
        has_content = any(
            child.is_symlink() or (child.is_dir() and (child / ".git").exists())
            for child in path.iterdir()
        )
        on_disk.append({"name": name, "path": key, "has_real_worktrees": has_content})

    if wt_root.is_dir():
        for entry in sorted(wt_root.iterdir()):
            # Hidden dirs under the wt root are not task worktrees and never
            # will be: the wt root is an ordinary directory a user's tools also
            # write into (JetBrains puts its project dir at `<wt root>/.idea`).
            # Discovery skips them; the claims leg below does NOT, so a task
            # whose `worktree:` genuinely points at a dotted path is still
            # tracked and never reported missing.
            if entry.is_dir() and not entry.name.startswith("."):
                note(entry, entry.name)
    for c in active_claims + parked_claims:
        note(Path(c["path"]), Path(c["path"]).name)

    on_disk_paths = {d["path"] for d in on_disk}
    orphans = [
        d for d in on_disk
        if d["path"] not in active_paths and d["path"] not in parked_paths
    ]
    parked = [d for d in on_disk if d["path"] in parked_paths]
    # Missing is a property of the CLAIMED path itself, tested directly —
    # never of whether it happens to sit under the wt root.
    missing = [c for c in active_claims if not Path(c["path"]).is_dir()]

    # Task-branch dirt: real (non-symlink) worktree children only. Dirty
    # canonical checkouts reached via symlinks are deduped by resolved path.
    dirty: list[dict[str, Any]] = []
    canonical_seen: set[str] = set()
    canonical_dirty: list[dict[str, Any]] = []
    for entry in on_disk:
        for child in Path(entry["path"]).iterdir():
            if not (child.is_dir() and (child / ".git").exists()):
                continue
            if child.is_symlink():
                target = str(child.resolve())
                if target in canonical_seen:
                    continue
                canonical_seen.add(target)
                n = _dirty_file_count(child)
                if n:
                    canonical_dirty.append({"repo": child.name, "dirty_files": n})
                continue
            n = _dirty_file_count(child)
            if n:
                dirty.append(
                    {"worktree": entry["name"], "repo": child.name, "dirty_files": n}
                )

    return {
        "on_disk_count": len(on_disk),
        "claimed_count": len(active_claims),
        "orphans": orphans,
        "parked": parked,
        "missing": missing,
        "dirty": dirty,
        "canonical_dirty": sorted(canonical_dirty, key=lambda d: d["repo"]),
    }


def main(argv: list[str] | None = None) -> int:
    result = scan()

    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(HEART_STATE_DIR / "worktree_drift.json", result)

    from heart.heart_color import c_fail, c_info, c_ok, c_warn, glyph_fail, glyph_ok, glyph_warn

    orphan_n = len(result["orphans"])
    missing_n = len(result["missing"])
    dirty_n = len(result["dirty"])
    canon_n = len(result["canonical_dirty"])
    if dirty_n or missing_n:
        glyph, label = glyph_fail(), c_fail(
            f"drift: {orphan_n} orphan / {missing_n} missing / {dirty_n} dirty"
        )
    elif orphan_n or canon_n:
        bits = []
        if orphan_n:
            bits.append(f"{orphan_n} orphan dir(s) (clean)")
        if canon_n:
            bits.append(f"{canon_n} canonical checkout(s) dirty")
        glyph, label = glyph_warn(), c_warn(" / ".join(bits))
    else:
        glyph, label = glyph_ok(), c_ok("no drift")
    print(f"{glyph} {c_info('worktrees')} {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
