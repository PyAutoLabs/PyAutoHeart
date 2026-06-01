"""pulse/status.py — render cached Pulse state with colour to stdout."""

from __future__ import annotations

import argparse
import datetime
import json
import sys

from pulse import readiness, state
from pulse.pulse_color import (
    c_bold, c_dim, c_fail, c_info, c_meta, c_ok, c_warn,
    glyph_fail, glyph_ok, glyph_warn,
)


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return c_fail("no cache")
    if seconds < 60:
        return c_ok(f"{int(seconds)}s ago")
    if seconds < 3600:
        return c_meta(f"{int(seconds//60)}m ago")
    return c_warn(f"{int(seconds//3600)}h ago")


def _repo_glyph(repo: dict) -> tuple[str, str]:
    """Return (glyph, label) summarising one repo's state. Worst-of."""
    ci = repo.get("ci_status", {})
    rs = repo.get("repo_state", {})
    pr = repo.get("open_prs", {})
    # Only genuine source drift counts toward yellow; regenerated-artifact
    # noise (dirty_noise) is informational. Fall back to the old dirty_files
    # field for caches written before the real/noise split existed.
    dirty_real = rs.get("dirty_real", rs.get("dirty_files", 0))
    dirty_noise = rs.get("dirty_noise", 0)
    pr_age = pr.get("max_age_days", 0) if pr.get("open_count") else 0

    has_red = ci.get("conclusion") == "failure" or pr_age >= 30
    has_yellow = (
        ci.get("status") in ("in_progress", "queued")
        or dirty_real > 0
        or rs.get("ahead", 0) > 0
        or rs.get("behind", 0) > 0
        or (rs.get("branch") and rs.get("branch") != "main")
        or pr_age >= 7
    )

    fragments: list[str] = []
    if ci:
        if ci.get("conclusion") == "success":
            fragments.append(c_ok("CI ✓"))
        elif ci.get("conclusion") == "failure":
            fragments.append(c_fail("CI ✗"))
        elif ci.get("status") in ("in_progress", "queued"):
            fragments.append(c_warn(f"CI {ci['status']}"))
        elif ci.get("conclusion"):
            fragments.append(c_warn(f"CI {ci['conclusion']}"))
    if rs:
        if rs.get("branch") and rs.get("branch") != "main":
            fragments.append(c_warn(f"branch={rs['branch']}"))
        if dirty_real:
            fragments.append(c_warn(f"dirty={dirty_real}"))
        if dirty_noise:
            fragments.append(c_meta(f"+{dirty_noise} gen"))
        if rs.get("ahead"):
            fragments.append(c_warn(f"ahead={rs['ahead']}"))
        if rs.get("behind"):
            fragments.append(c_warn(f"behind={rs['behind']}"))
    if pr.get("open_count"):
        n = pr["open_count"]
        if pr_age >= 30:
            fragments.append(c_fail(f"PR×{n} (oldest {pr_age}d)"))
        elif pr_age >= 7:
            fragments.append(c_warn(f"PR×{n} (oldest {pr_age}d)"))
        else:
            fragments.append(c_info(f"PR×{n}"))

    if has_red:
        glyph = glyph_fail()
    elif has_yellow:
        glyph = glyph_warn()
    else:
        glyph = glyph_ok()
    label = "  ".join(fragments) if fragments else c_ok("clean / nominal")
    return glyph, label


def render(snapshot: dict, quiet: bool = False) -> None:
    ts = snapshot.get("ts", "")
    age = state.age_seconds()
    print(c_bold("PyAutoPulse status") + "  " + c_meta(f"snapshot {ts}  ({_format_age(age)})"))
    print()

    # Release-readiness verdict — the headline, at the very top.
    for line in readiness.render_block(readiness.load_verdict(), quiet=quiet):
        print(line)
    print()

    # Per-repo table.
    repos = snapshot.get("repos", {})
    if repos:
        print(c_info("REPOS"))
        # Group repos by their group label for readability.
        by_group: dict[str, list[tuple[str, dict]]] = {}
        for name, body in sorted(repos.items()):
            grp = body.get("repo_state", {}).get("group") or body.get("ci_status", {}).get("group") or "?"
            by_group.setdefault(grp, []).append((name, body))
        for group, entries in sorted(by_group.items()):
            print("  " + c_meta(group))
            for name, body in sorted(entries):
                glyph, label = _repo_glyph(body)
                print(f"    {glyph} {c_info(f'{name:<40}')} {label}")
        print()

    # Worktree drift block.
    wt = snapshot.get("worktree_drift") or {}
    if wt:
        orphans = wt.get("orphans", [])
        missing = wt.get("missing", [])
        dirty = wt.get("dirty", [])
        if dirty or missing:
            print(c_info("WORKTREES") + " " + glyph_fail() + " " + c_fail(
                f"{len(orphans)} orphan / {len(missing)} missing / {len(dirty)} dirty"
            ))
        elif orphans:
            print(c_info("WORKTREES") + " " + glyph_warn() + " " + c_warn(f"{len(orphans)} orphan dir(s) (clean)"))
        else:
            print(c_info("WORKTREES") + " " + glyph_ok() + " " + c_ok("no drift"))
        if not quiet and dirty:
            for d in dirty[:5]:
                print("  " + c_fail(f"  • {d.get('worktree')}/{d.get('repo')}: {d.get('dirty_files')} dirty"))
        print()

    # Script timing block.
    timing = snapshot.get("script_timing") or {}
    if timing:
        r = timing.get("red_count", 0)
        y = timing.get("yellow_count", 0)
        g = timing.get("green_count", 0)
        nb = timing.get("new_scripts_no_baseline", 0)
        if r:
            print(c_info("SCRIPT TIMINGS") + " " + glyph_fail() + " "
                  + c_fail(f"{r} regressions (>3× baseline)") + "  " + c_warn(f"{y} slow (>1.5×)"))
        elif y:
            print(c_info("SCRIPT TIMINGS") + " " + glyph_warn() + " "
                  + c_warn(f"{y} scripts >1.5× baseline") + "  " + c_meta(f"{g} within baseline"))
        else:
            print(c_info("SCRIPT TIMINGS") + " " + glyph_ok() + " "
                  + c_ok(f"{g} within baseline") + "  " + c_meta(f"({nb} new, no baseline)"))
        if not quiet:
            for entry in timing.get("red", [])[:5]:
                print("  " + c_fail(
                    f"  ✗ {entry['project']}/{entry['file'].split('/')[-1]}  "
                    f"{entry['latest_seconds']:.1f}s vs baseline {entry['baseline_seconds']:.1f}s  "
                    f"({entry['ratio']}×)"
                ))
            for entry in timing.get("yellow", [])[:5]:
                print("  " + c_warn(
                    f"  ! {entry['project']}/{entry['file'].split('/')[-1]}  "
                    f"{entry['latest_seconds']:.1f}s vs baseline {entry['baseline_seconds']:.1f}s  "
                    f"({entry['ratio']}×)"
                ))
        print()

    # Latest Build test-run block.
    test_run = snapshot.get("test_run") or {}
    if test_run:
        ready = test_run.get("ready")
        failed = test_run.get("failed", 0)
        passed = test_run.get("passed", 0)
        skipped = test_run.get("skipped", 0)
        label = test_run.get("run_label", "?")
        counts = c_meta(f"{passed}p / {failed}f / {skipped}s  @ {label}")
        if ready is False or failed:
            print(c_info("TEST RUN") + " " + glyph_fail() + " " + c_fail("NOT ready") + "  " + counts)
        elif ready is True:
            print(c_info("TEST RUN") + " " + glyph_ok() + " " + c_ok("ready") + "  " + counts)
        else:
            print(c_info("TEST RUN") + " " + glyph_warn() + " " + c_warn("ready unknown") + "  " + counts)
        stale = test_run.get("parked_stale_count", 0)
        if stale:
            print("  " + c_warn(f"  ! {stale} stale parked script(s)"))
        print()

    # Version-skew block — only shown when something is out of sync.
    skew = (snapshot.get("version_skew") or {}).get("workspaces", [])
    off = [w for w in skew if w.get("status") not in ("MATCH", None)]
    if off:
        ahead = [w for w in off if w.get("status") == "AHEAD"]
        glyph = glyph_fail() if ahead else glyph_warn()
        print(c_info("VERSION SKEW") + " " + glyph + " "
              + (c_fail(f"{len(ahead)} ahead") if ahead else c_warn(f"{len(off)} skewed")))
        if not quiet:
            for w in off[:8]:
                colour = c_fail if w.get("status") == "AHEAD" else c_warn
                print("  " + colour(
                    f"  {w.get('status')}: {w.get('workspace')} pinned {w.get('pinned')} "
                    f"vs installed {w.get('installed')}"
                ))
        print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pyauto-pulse status")
    ap.add_argument("--json", action="store_true", help="print raw state.json to stdout")
    ap.add_argument("--quiet", action="store_true", help="suppress drill-down details")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    ns = ap.parse_args(argv)
    if ns.no_color:
        import os
        os.environ["NO_COLOR"] = "1"

    snapshot = state.load()
    if snapshot is None:
        print(c_fail("no cache yet — run `pyauto-pulse tick` first"), file=sys.stderr)
        return 2

    if ns.json:
        json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    render(snapshot, quiet=ns.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
