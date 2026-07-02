"""heart/readiness.py — composite release-readiness verdict.

Rolls the continuous-health signals already in ``state.json`` into a single
green / yellow / red verdict answering "is it safe to release?". This is the
**authoritative** release gate: PyAutoBuild is a pure executor and no longer
runs its own readiness checks (``verify_workspace_versions.sh`` was removed) —
the orchestrator (PyAutoAgent's release agent) consults this verdict via
``pyauto-heart readiness --json`` and only dispatches Build's ``release.yml``
when it is green.

The verdict uses STRICT release gates:

- **RED** (a real blocker) if any of the 5 libraries has failing CI, is off
  ``main``, has uncommitted source changes, or is behind origin; or any workspace
  is pinned AHEAD of its installed library, has a ``general.yaml`` ↔
  ``version.txt`` MISMATCH, or an unparseable (BAD) version; or the deep install
  verification last reported ``ready == false``; or the release-validation
  report last ingested reports ``release_ready == false`` (a stage failed).
- **YELLOW** (caution) for soft signals: workspace-validation not passing (the
  workspace scripts/notebooks carry standing debt, so this is advisory — never a
  hard block), script-timing regressions, stale open PRs, stale parked scripts, a
  workspace pinned BEHIND, a stale or never-run install verification, **no fresh
  release-validation report for the current source** (absent, stale by age, or
  whose ``commit_shas`` no longer match the current ``main`` HEADs, or which ran
  under a profile other than ``release``), and — crucially — any *unknown*
  (missing test-run report, a library absent from the snapshot). An unknown is
  never silently treated as green and never escalated to red.
- **GREEN** otherwise — which now REQUIRES a fresh, passing release-validation
  report matching the current source under the ``release`` profile, not just an
  absence of red signals.

Red dominates yellow structurally: reasons are collected into separate lists
and ``verdict = red if red_reasons else yellow if yellow_reasons else green``.
A ``score`` (0–100, weighted penalties) is advisory/sortable only — the colour,
not the number, is the gate. ``compute`` is a pure function of the snapshot for
easy testing and never raises on partial/malformed data.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from heart import state
from heart.checks.ci_status import FAILURE_CONCLUSIONS, load_required_workflows
from heart.heart_color import (
    c_bold, c_fail, c_info, c_meta, c_ok, c_warn,
    glyph_fail, glyph_ok, glyph_warn,
)

HEART_HOME = Path(__file__).resolve().parents[1]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"
RELEASE_READY_FILE = state.HEART_STATE_DIR / "release_ready.json"

DEFAULT_LIBRARIES = ("PyAutoConf", "PyAutoFit", "PyAutoArray", "PyAutoGalaxy", "PyAutoLens")

# Workspace repo groups whose required-workflow conclusions on `main` HEAD are a
# hard release gate (RED on failure). Libraries are gated separately (the lib
# loop); url-checking is advisory and intentionally absent from the required set
# (see config/repos.yaml `required_workflows`). A red `smoke_tests` on a
# workspace's main is a real release blocker — a green `url_check` cannot mask
# it because url is not a required workflow.
GATED_WORKSPACE_GROUPS = frozenset({"workspaces", "workspaces_test", "howto"})

# gate key -> (penalty per occurrence, cap). Score = 100 - sum(min(n*w, cap)).
# Advisory only; colour is decided by reason presence, not by score.
_WEIGHTS: dict[str, tuple[int, int]] = {
    "lib_ci": (30, 60),
    "lib_branch": (15, 30),
    "lib_dirty": (15, 30),
    "lib_behind": (20, 40),
    "test_failing": (15, 15),
    "skew_ahead": (25, 50),
    "lib_unknown": (10, 30),
    "test_unknown": (10, 10),
    "timing_red": (15, 15),
    "timing_yellow": (8, 8),
    "open_pr": (5, 15),
    "parked": (5, 15),
    "skew_behind": (8, 24),
    "skew_mismatch": (25, 50),
    "skew_bad": (25, 50),
    "skew_unknown": (10, 30),
    "install_not_ready": (40, 40),
    "install_stale": (10, 10),
    "install_unknown": (10, 10),
    "test_stale": (10, 10),
    "ws_ci": (20, 60),
    "validation_failed": (40, 40),
    "validation_absent": (15, 15),
    "validation_stale_sha": (15, 15),
    "validation_unknown": (12, 12),
    "validation_profile": (12, 12),
    "validation_stale": (10, 10),
}


def load_library_names(config_path: Path | str = CONFIG_PATH) -> list[str]:
    """Return repos.libraries[].name, or DEFAULT_LIBRARIES if unavailable."""
    try:
        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        libs = [r["name"] for r in cfg.get("repos", {}).get("libraries", [])]
        return libs or list(DEFAULT_LIBRARIES)
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return list(DEFAULT_LIBRARIES)


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# install verification older than this many days is treated as stale (YELLOW).
INSTALL_STALE_DAYS = 14
# workspace-validation test run older than this many days is treated as stale.
TEST_STALE_DAYS = 10
# a release-validation rehearsal older than this many days is stale (YELLOW),
# even if its commit_shas still match — releases move fast enough that a
# week-old rehearsal warrants a fresh run.
VALIDATION_STALE_DAYS = 7

# The library repos whose current `main` HEAD must match a validation report's
# recorded commit_shas for the release-validation gate to go GREEN.
_GATE_SHA_LIBS = DEFAULT_LIBRARIES


def _sha_eq(a: Any, b: Any) -> bool:
    """True if two commit SHAs refer to the same commit (prefix-tolerant).

    ci_status records a short (7-char) HEAD sha while a report may carry the
    full 40-char sha (or vice versa), so compare on the shorter length. A match
    needs at least 7 significant hex chars — an empty/too-short value never
    matches (it is treated as *unknown*, never a silent pass)."""
    sa = str(a or "").strip().lower()
    sb = str(b or "").strip().lower()
    n = min(len(sa), len(sb))
    if n < 7:
        return False
    return sa[:n] == sb[:n]


def _parse_ts(ts: Any) -> datetime.datetime | None:
    try:
        t = datetime.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=datetime.timezone.utc) if t.tzinfo is None else t


def _age_days(ts: Any, ref: datetime.datetime | None) -> float | None:
    """Days between ``ts`` and ``ref`` (the snapshot time), or None if unknown."""
    t = _parse_ts(ts)
    if t is None or ref is None:
        return None
    return (ref - t).total_seconds() / 86400.0


def compute(
    snapshot: dict | None,
    libraries: Sequence[str] | None = None,
    required_workflows: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Pure verdict function. Never raises on missing/partial data."""
    snapshot = snapshot or {}
    libs = list(libraries) if libraries is not None else load_library_names()
    req_wf = required_workflows if required_workflows is not None else load_required_workflows()
    repos = snapshot.get("repos", {}) or {}
    ref = _parse_ts(snapshot.get("ts")) or datetime.datetime.now(datetime.timezone.utc)

    red: list[str] = []
    yellow: list[str] = []
    counts: dict[str, int] = {}

    def hit(key: str, n: int = 1) -> None:
        counts[key] = counts.get(key, 0) + n

    # --- library gates (RED) ---
    for lib in libs:
        body = repos.get(lib)
        if not isinstance(body, dict) or not body:
            yellow.append(f"{lib}: status unknown")
            hit("lib_unknown")
            continue
        ci = body.get("ci_status", {}) or {}
        conclusion = ci.get("conclusion")
        if conclusion not in (None, "", "success"):
            red.append(f"{lib}: CI {conclusion}")
            hit("lib_ci")
        rs = body.get("repo_state", {}) or {}
        branch = rs.get("branch")
        if branch and branch != "main":
            red.append(f"{lib}: on branch {branch} (not main)")
            hit("lib_branch")
        dirty_real = _as_int(rs.get("dirty_real", rs.get("dirty_files", 0)))
        if dirty_real > 0:
            red.append(f"{lib}: {dirty_real} uncommitted source change(s)")
            hit("lib_dirty")
        behind = _as_int(rs.get("behind", 0))
        if behind > 0:
            red.append(f"{lib}: {behind} commit(s) behind origin")
            hit("lib_behind")

    # --- workspace CI gate (RED) ---
    # Gate each gated workspace/howto repo on the conclusion of its REQUIRED
    # workflows on the `main` HEAD. A failing required workflow (e.g. red
    # `Smoke Tests`) is a real release blocker; a green non-required workflow
    # (url_check) cannot mask it. Libraries are handled by the loop above, so
    # they are skipped here. Absent repos are simply not observed (no reason).
    for name, body in sorted(repos.items()):
        if not isinstance(body, dict):
            continue
        ci = body.get("ci_status", {}) or {}
        group = ci.get("group")
        if group not in GATED_WORKSPACE_GROUPS:
            continue
        required = req_wf.get(group, [])
        if not required:
            continue
        workflows = ci.get("workflows")
        if isinstance(workflows, dict):
            for wf in required:
                concl = (workflows.get(wf) or {}).get("conclusion")
                if concl in FAILURE_CONCLUSIONS:
                    red.append(f"{name}: {wf} {concl} on main")
                    hit("ws_ci")
        else:
            # Pre-structured sidecar: fall back to the rolled-up conclusion.
            concl = ci.get("conclusion")
            if concl in FAILURE_CONCLUSIONS:
                red.append(f"{name}: CI {concl} on main")
                hit("ws_ci")

    # --- test-run gate (RED if false, YELLOW if unknown) ---
    test_run = snapshot.get("test_run")
    if isinstance(test_run, dict) and "ready" in test_run:
        ready = test_run.get("ready")
        if ready is False:
            # Workspace scripts/notebooks carry standing debt; failing validation
            # is advisory (YELLOW), not a release blocker. Real blockers are the
            # library CI / install / version-skew gates above.
            yellow.append(
                f"workspace validation not passing "
                f"({_as_int(test_run.get('failed', 0))} failed, {test_run.get('run_label', '?')})"
            )
            hit("test_failing")
        elif ready is True:
            age = _age_days(test_run.get("ts"), ref)
            if age is not None and age > TEST_STALE_DAYS:
                yellow.append(f"test run stale ({int(age)}d old)")
                hit("test_stale")
        else:
            yellow.append("test run status unknown")
            hit("test_unknown")
        # parked staleness (YELLOW)
        parked = _as_int(test_run.get("parked_stale_count", 0))
        if parked > 0:
            yellow.append(f"{parked} stale parked script(s)")
            hit("parked")
    else:
        yellow.append("test run status unknown (no report.json)")
        hit("test_unknown")

    # --- version skew (RED ahead / YELLOW behind) ---
    skew = snapshot.get("version_skew")
    if isinstance(skew, dict):
        for w in skew.get("workspaces") or []:
            if not isinstance(w, dict):
                continue
            status = str(w.get("status", "")).upper()
            if status == "AHEAD":
                red.append(f"{w.get('workspace')}: pinned {w.get('pinned')} AHEAD of installed {w.get('installed')}")
                hit("skew_ahead")
            elif status == "MISMATCH":
                red.append(
                    f"{w.get('workspace')}: general.yaml {w.get('pinned')} "
                    f"≠ version.txt {w.get('version_txt')}"
                )
                hit("skew_mismatch")
            elif status == "BAD":
                red.append(
                    f"{w.get('workspace')}: unparseable version "
                    f"(pinned {w.get('pinned')} / installed {w.get('installed')})"
                )
                hit("skew_bad")
            elif status == "BEHIND":
                yellow.append(f"{w.get('workspace')}: pinned BEHIND installed {w.get('installed')}")
                hit("skew_behind")
            elif status == "UNKNOWN":
                yellow.append(f"{w.get('workspace')}: installed {w.get('library')} version unknown")
                hit("skew_unknown")

    # --- install verification (deep check: RED on fail, YELLOW if stale/unrun) ---
    vi = snapshot.get("verify_install")
    if isinstance(vi, dict) and "ready" in vi:
        if vi.get("ready") is False:
            failed = [
                str(c.get("check"))
                for c in (vi.get("checks") or [])
                if isinstance(c, dict) and str(c.get("status")).upper() == "FAIL"
            ]
            red.append(f"install verification FAILED (checks {', '.join(failed) or '?'})")
            hit("install_not_ready")
        else:
            age = _age_days(vi.get("ts"), ref)
            if age is None or age > INSTALL_STALE_DAYS:
                yellow.append(
                    "install verification stale "
                    + ("(age unknown)" if age is None else f"({int(age)}d old)")
                )
                hit("install_stale")
    else:
        yellow.append("install verification not run")
        hit("install_unknown")

    # --- release-validation gate (hard) -------------------------------------
    # GREEN-for-release now REQUIRES a fresh, passing validation_report whose
    # commit_shas match the current `main` HEADs and whose profile == release.
    # This is the M2 gate: the report proves the exact source about to ship was
    # built, published to TestPyPI, installed from the wheel, and exercised at
    # release fidelity. Absent/stale/source-not-matching → YELLOW ("no release
    # rehearsal for current source"); failing → RED. Pass/fail (release_ready)
    # is the RED axis; fidelity+freshness (profile / commit_shas / age) is the
    # YELLOW axis — a passing-but-stale report is a caution, not a blocker.
    vr = snapshot.get("validation_report")
    if isinstance(vr, dict) and vr:
        ready = vr.get("release_ready")
        if ready is False:
            failed_stages = [
                n for n, s in (vr.get("stages") or {}).items()
                if isinstance(s, dict) and s.get("status") == "fail"
            ]
            red.append(
                "release validation FAILED"
                + (f" (stage {', '.join(failed_stages)})" if failed_stages else "")
            )
            hit("validation_failed")
        elif ready is True:
            commit_shas = vr.get("commit_shas") or {}
            mismatched: list[str] = []
            unconfirmed: list[str] = []
            confirmed = 0
            for lib in _GATE_SHA_LIBS:
                want = commit_shas.get(lib)
                cur = ((repos.get(lib) or {}).get("ci_status") or {}).get("head_sha")
                if not want or not cur:
                    # Can't confirm THIS repo (unknown) — tracked separately so a
                    # mix of confirmed + unconfirmed libs doesn't silently reach
                    # GREEN just because zero were an outright mismatch.
                    unconfirmed.append(lib)
                    continue
                if _sha_eq(want, cur):
                    confirmed += 1
                else:
                    mismatched.append(lib)
            profile = str(vr.get("profile") or "").strip().lower()
            if not commit_shas:
                yellow.append("release validation source unconfirmed (no commit_shas)")
                hit("validation_unknown")
            elif mismatched:
                yellow.append(
                    "release validation stale: source moved since rehearsal ("
                    + ", ".join(mismatched) + ")"
                )
                hit("validation_stale_sha")
            elif confirmed == 0:
                yellow.append("release validation source unconfirmed (current HEADs unknown)")
                hit("validation_unknown")
            elif unconfirmed:
                # Some libs matched, but at least one gated repo's SHA could not
                # be confirmed either way — an unknown must never be silently
                # treated as green (same principle every other gate here follows).
                yellow.append(
                    "release validation partially unconfirmed (repo(s) with unknown "
                    "current HEAD: " + ", ".join(unconfirmed) + ")"
                )
                hit("validation_unknown")
            elif profile != "release":
                yellow.append(
                    f"release validation profile '{vr.get('profile') or '?'}' is not 'release'"
                )
                hit("validation_profile")
            else:
                age = _age_days(vr.get("ts"), ref)
                if age is None or age > VALIDATION_STALE_DAYS:
                    yellow.append(
                        "release validation stale "
                        + ("(age unknown)" if age is None else f"({int(age)}d old)")
                    )
                    hit("validation_stale")
                # else: fresh, passing, matching, release profile → GREEN-eligible
        else:
            yellow.append("release validation status unknown")
            hit("validation_unknown")
    else:
        yellow.append("no release validation for current source")
        hit("validation_absent")

    # --- script timing (YELLOW) ---
    timing = snapshot.get("script_timing", {}) or {}
    if _as_int(timing.get("red_count", 0)) > 0:
        yellow.append(f"{_as_int(timing.get('red_count'))} script timing regression(s)")
        hit("timing_red")
    elif _as_int(timing.get("yellow_count", 0)) > 0:
        yellow.append(f"{_as_int(timing.get('yellow_count'))} slow script(s)")
        hit("timing_yellow")

    # --- open PRs across all repos (YELLOW) ---
    for name, body in sorted(repos.items()):
        if not isinstance(body, dict):
            continue
        pr = body.get("open_prs", {}) or {}
        if _as_int(pr.get("open_count", 0)) > 0 and _as_int(pr.get("max_age_days", 0)) >= 7:
            yellow.append(f"{name}: open PR {_as_int(pr.get('max_age_days'))}d old")
            hit("open_pr")

    # --- score ---
    score = 100
    for key, n in counts.items():
        w, cap = _WEIGHTS.get(key, (0, 0))
        score -= min(n * w, cap)
    score = max(0, min(100, score))

    verdict = "red" if red else ("yellow" if yellow else "green")
    return {
        "verdict": verdict,
        "score": score,
        "reasons": red + yellow,
        "red_reasons": red,
        "yellow_reasons": yellow,
        "ts": snapshot.get("ts") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def run() -> dict[str, Any]:
    """Load state, compute, atomic-write release_ready.json, return verdict."""
    verdict = compute(state.load() or {})
    state.atomic_write_json(RELEASE_READY_FILE, verdict)
    return verdict


def load_verdict() -> dict[str, Any]:
    """Return the persisted verdict, falling back to a live compute."""
    if RELEASE_READY_FILE.is_file():
        try:
            return json.loads(RELEASE_READY_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return compute(state.load() or {})


def render_block(verdict: dict[str, Any], quiet: bool = False) -> list[str]:
    v = verdict.get("verdict", "green")
    score = verdict.get("score", 0)
    if v == "red":
        glyph, word = glyph_fail(), c_fail("RED")
    elif v == "yellow":
        glyph, word = glyph_warn(), c_warn("YELLOW")
    else:
        glyph, word = glyph_ok(), c_ok("GREEN")
    lines = [f"{c_info('RELEASE READINESS')}  {glyph} {word}  {c_meta(f'score {score}')}"]

    reds = verdict.get("red_reasons", [])
    yellows = verdict.get("yellow_reasons", [])
    limit = 1 if quiet else 6
    shown = 0
    for r in reds:
        lines.append("  " + c_fail(f"✗ {r}"))
        shown += 1
        if shown >= limit:
            break
    if shown < limit:
        for y in yellows[: limit - shown]:
            lines.append("  " + c_warn(f"! {y}"))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pyauto-heart readiness")
    ap.add_argument("--json", action="store_true", help="print the raw verdict JSON")
    ap.add_argument("--quiet", action="store_true", help="verdict line + top reason only")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    ns = ap.parse_args(argv)
    if ns.no_color:
        os.environ["NO_COLOR"] = "1"

    verdict = load_verdict()
    if ns.json:
        json.dump(verdict, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    for line in render_block(verdict, quiet=ns.quiet):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
