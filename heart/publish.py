"""heart/publish.py — push a distilled dev-box observation into the Heart repo.

The cloud board is honest about what it cannot see: the local-only check
families render "not observed here". This module is the sanctioned enrichment
path from ``dashboard.py``'s docstring — the dev box distills ITS board's
local-only rows into ``state/devbox_board.json``, commits, and pushes to the
Heart's own repo (the observer rule holds: Heart writes only its own
repo/state). The cloud render then fills those rows, age-stamped
"observed Nh ago on the dev box", falling back to grey once the observation
expires (``dashboard.DEVBOX_FRESH_SECONDS``).

Privacy: the distilled file carries section states, summaries, and detail
lines ONLY — and any detail line naming a local filesystem path (``/home/``,
``~``, the expanded home dir) is dropped before it leaves the machine. The
repo is public; worktree names and repo names are fine, absolute local paths
are not. ``tests/test_publish.py`` pins this.

Usage:
    pyauto-heart publish              # distill + commit + push
    pyauto-heart publish --dry-run    # print the distilled JSON, write nothing
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from heart import dashboard, readiness, state

HEART_ROOT = Path(__file__).resolve().parents[1]
DEVBOX_FILE = HEART_ROOT / "state" / "devbox_board.json"

# Only the families the cloud job cannot observe travel; everything else the
# cloud measures itself, and merging two vantages of the same family would
# break the unify invariant. repo_state is excluded: it folds into per-repo
# rows, not a section of its own.
PUBLISH_FAMILIES = tuple(
    f for f in dashboard.LOCAL_ONLY_FAMILIES if f != "repo_state"
)

DEVBOX_SCHEMA_VERSION = 1


def _scrub(lines: list[str]) -> list[str]:
    """Drop detail lines that name a local filesystem path."""
    home = os.path.expanduser("~")
    out = []
    for line in lines:
        text = str(line)
        if "/home/" in text or "~" in text or (home and home in text):
            continue
        out.append(text)
    return out


def build_devbox_board(snapshot: dict | None, verdict: dict | None) -> dict[str, Any]:
    """Distill the LOCAL board's local-only families. Pure; never raises."""
    board = dashboard.build_board(snapshot, verdict, unobserved=())
    sections: dict[str, Any] = {}
    for sec in board.sections:
        if sec.key not in PUBLISH_FAMILIES:
            continue
        if sec.state == dashboard.UNOBS:
            continue  # nothing observed locally either — publish no claim
        sections[sec.key] = {
            "state": sec.state,
            "summary": sec.summary,
            "details": _scrub(sec.details)[:8],
        }
    return {
        "schema_version": DEVBOX_SCHEMA_VERSION,
        "ts": (snapshot or {}).get("ts") or "",
        "sections": sections,
    }


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(HEART_ROOT), *args],
                          capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="pyauto-heart publish")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the distilled JSON, write and push nothing")
    ns = ap.parse_args(argv)

    snapshot = state.load()
    if snapshot is None:
        print("no cached state — run `pyauto-heart tick` first", file=sys.stderr)
        return 2
    verdict = readiness.load_verdict()
    devbox = build_devbox_board(snapshot, verdict)
    payload = json.dumps(devbox, indent=2, sort_keys=True) + "\n"

    if ns.dry_run:
        print(payload, end="")
        return 0
    if not devbox["sections"]:
        print("nothing observed locally to publish — run `pyauto-heart tick` first",
              file=sys.stderr)
        return 2

    branch = _git("branch", "--show-current").stdout.strip()
    if branch != "main":
        print(f"checkout is on '{branch}' — publish commits to main only; "
              "switch to main first (or use --dry-run)", file=sys.stderr)
        return 2

    DEVBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DEVBOX_FILE.exists() and DEVBOX_FILE.read_text() == payload:
        print("devbox board already published and current — nothing to push")
        return 0
    DEVBOX_FILE.write_text(payload)

    rel = str(DEVBOX_FILE.relative_to(HEART_ROOT))
    _git("add", rel)
    committed = _git("commit", "-m",
                     "heart: publish dev-box board (local-only check families)")
    if committed.returncode != 0 and "nothing to commit" in committed.stdout + committed.stderr:
        print("devbox board unchanged after staging — nothing to push")
        return 0
    if committed.returncode != 0:
        print(committed.stderr or committed.stdout, file=sys.stderr)
        return 1

    # Concurrent pushes (self-heals, other sessions) are normal on main;
    # rebase our one commit onto the new tip and retry.
    for attempt in (1, 2, 3):
        pushed = _git("push", "origin", "HEAD")
        if pushed.returncode == 0:
            print(f"published {rel} ({len(devbox['sections'])} families, "
                  f"snapshot {devbox['ts']})")
            return 0
        rebased = _git("pull", "--rebase", "origin", "main")
        if rebased.returncode != 0:
            print(rebased.stderr or rebased.stdout, file=sys.stderr)
            return 1
    print("could not push the devbox board after 3 attempts", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
