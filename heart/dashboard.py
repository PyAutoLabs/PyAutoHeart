"""heart/dashboard.py — the ONE unified health-dashboard renderer.

This module is the single source of truth for "the board": one pure
:func:`render` function projects the SAME cached snapshot (+ the readiness
verdict + the release-validation report) into every surface's format. Nothing
here recomputes health — the web page, the CLI line, and the mobile card are all
projections of ``state.json`` + ``release_ready.json`` (+ ``validation_report``),
so the three surfaces *cannot disagree* (the "unify invariant").

    render(snapshot, verdict, validation, *, fmt) -> str
        fmt = "term"     # the full colour board (what `status`/`readiness` show)
            | "oneline"  # compact one-liner for the venv/prompt hook
            | "md"       # GitHub-flavoured markdown (step summary / issue / README)
            | "html"     # standalone self-contained page (GitHub Pages)
            | "json"     # the machine surface the Health Agent + mobile consume

Both ``render`` and the intermediate :func:`build_board` are **pure** (snapshot
in → value out, no I/O), mirroring ``heart/readiness.py::compute`` and
``heart/status.py::render``, so they stay trivially testable on Heart's
stdlib-only test footprint. ``status.render`` and ``readiness.render_block``
delegate here so there is exactly one definition of what the board looks like.

**Cloud-only-honest.** The scheduled cloud job only observes the two API-safe
checks (ci_status, open_prs); it has no local working tree. Passing the
local-only check families in ``unobserved`` makes the board mark them
"not observed here (dev-box only)" instead of silently showing them green. A
dev-box push of the full snapshot can enrich the SAME page by rendering with an
empty ``unobserved`` — never a second, competing page.
"""

from __future__ import annotations

import datetime
import html as _html
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from heart.heart_color import (
    c_bold, c_dim, c_fail, c_info, c_meta, c_ok, c_warn,
    glyph_fail, glyph_info, glyph_ok, glyph_warn,
)

# --- states a section / row can carry ---------------------------------------
OK = "ok"
WARN = "warn"
FAIL = "fail"
UNOBS = "unobserved"
INFO = "info"

# Check families that only a local working tree can observe. On the cloud job
# these are passed as ``unobserved`` so the board marks them honestly rather
# than implying they are green. (Spec §2 "Cloud-safe caveat".)
LOCAL_ONLY_FAMILIES = (
    "repo_state",
    "worktree_drift",
    "script_timing",
    "import_time",
    "unit_test_timing",
    "profiling_drift",
    "test_run",
    "version_skew",
)

# The board advertises its own age; older than this (seconds) is "stale" — a
# cached board that does not flag its own staleness is a footgun. The daemon
# ticks every ~5 min, so an hour without a fresh tick warrants a nudge.
STALE_AFTER_SECONDS = 3600

# Library repos, used to split the per-repo table into libraries vs workspaces
# when a repo body carries no group label. Derived from the policy file
# (config/repos.yaml `repos.libraries`) — dashboard cannot import readiness
# (cycle), so it reads the same file directly.
def _library_names() -> tuple:
    import pathlib

    import yaml

    cfg_path = pathlib.Path(__file__).resolve().parents[1] / "config" / "repos.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    return tuple(r["name"] for r in cfg["repos"]["libraries"])


DEFAULT_LIBRARIES = _library_names()
GATED_WORKSPACE_GROUPS = frozenset({"workspaces", "workspaces_test", "howto"})

# The public Pages board (the badge's entry point). Kept here so every surface
# that links "the webpage" agrees on the URL.
PAGES_URL = "https://pyautolabs.github.io/PyAutoHeart/"

SCHEMA_VERSION = 1


@dataclass
class Section:
    """One board row: a topic, its worst-of state, a summary, and details."""

    key: str
    title: str
    state: str
    summary: str
    details: list[str] = field(default_factory=list)


@dataclass
class Board:
    """The whole board as data — every surface is a projection of this."""

    verdict: str          # green | stale | yellow | red
    score: int
    ts: str               # snapshot timestamp
    age_seconds: float | None
    stale: bool           # board-tick staleness (snapshot age), NOT the verdict tier
    red_reasons: list[str]
    yellow_reasons: list[str]
    sections: list[Section]
    # readiness freshness tier: evidence missing/expired, nothing known-bad
    # (heart/readiness.py) — distinct from the tick-age `stale` bool above.
    stale_reasons: list[str] = field(default_factory=list)


# --- verdict/state → glyph & colour maps ------------------------------------
_VERDICT_STATE = {"red": FAIL, "yellow": WARN, "stale": INFO, "green": OK}
_VERDICT_WORD = {"red": "RED", "yellow": "YELLOW", "stale": "STALE", "green": "GREEN"}
_STATE_MD = {OK: "🟢", WARN: "🟡", FAIL: "🔴", UNOBS: "⚪", INFO: "🔵"}
_STATE_HTML = {OK: "ok", WARN: "warn", FAIL: "fail", UNOBS: "unobs", INFO: "info"}
_BADGE_COLOR = {"red": "red", "yellow": "yellow", "stale": "blue", "green": "brightgreen"}


def _colour(state: str, text: str) -> str:
    if state == OK:
        return c_ok(text)
    if state == WARN:
        return c_warn(text)
    if state == FAIL:
        return c_fail(text)
    if state == UNOBS:
        return c_meta(text)
    return c_info(text)


def _glyph(state: str) -> str:
    if state == OK:
        return glyph_ok()
    if state == WARN:
        return glyph_warn()
    if state == FAIL:
        return glyph_fail()
    if state == UNOBS:
        return c_meta("·")
    return glyph_info()


def _worst(states: Iterable[str]) -> str:
    order = {FAIL: 3, WARN: 2, UNOBS: 1, INFO: 1, OK: 0}
    best = OK
    for s in states:
        if order.get(s, 0) > order.get(best, 0):
            best = s
    return best


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_ts(ts: Any) -> datetime.datetime | None:
    try:
        t = datetime.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=datetime.timezone.utc) if t.tzinfo is None else t


def _age_seconds(ts: Any, now: datetime.datetime | None) -> float | None:
    t = _parse_ts(ts)
    if t is None:
        return None
    ref = now or datetime.datetime.now(datetime.timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=datetime.timezone.utc)
    return (ref - t).total_seconds()


def format_age(seconds: float | None, *, stale: bool = False) -> str:
    """Human 'age' string, format-agnostic (no colour)."""
    if seconds is None:
        return "no cache"
    prefix = "stale " if stale else ""
    if seconds < 60:
        return "just now" if not stale else "stale <1m ago"
    if seconds < 3600:
        return f"{prefix}{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{prefix}{int(seconds // 3600)}h ago"
    return f"{prefix}{int(seconds // 86400)}d ago"


def _repo_group(body: dict) -> str:
    return (
        (body.get("repo_state") or {}).get("group")
        or (body.get("ci_status") or {}).get("group")
        or ""
    )


def _is_library(name: str, body: dict) -> bool:
    grp = _repo_group(body)
    if grp:
        return grp == "libraries"
    return name in DEFAULT_LIBRARIES


def _ci_fragment(ci: dict) -> tuple[str, str] | None:
    """(state, text) for a repo's CI, or None when there is no CI signal."""
    if not ci:
        return None
    concl = ci.get("conclusion")
    if concl == "success":
        return OK, "CI ✓"
    if concl not in (None, ""):
        wf = ci.get("workflow")
        return FAIL, (f"CI ✗ {wf}".rstrip() if wf else "CI ✗")
    if ci.get("status") in ("in_progress", "queued"):
        return WARN, f"CI {ci['status']}"
    return None


def _lib_row(name: str, body: dict, *, unobserved: Sequence[str]) -> tuple[str, str]:
    """(state, one-line label) for a single library/workspace repo row."""
    frags: list[tuple[str, str]] = []
    ci = body.get("ci_status") or {}
    ci_frag = _ci_fragment(ci)
    if ci_frag:
        frags.append(ci_frag)

    if "repo_state" in unobserved:
        frags.append((UNOBS, "repo state n/a here"))
    else:
        rs = body.get("repo_state") or {}
        branch = rs.get("branch")
        dirty_real = _as_int(rs.get("dirty_real", rs.get("dirty_files", 0)))
        if branch and branch != "main":
            frags.append((FAIL, f"branch={branch}"))
        if dirty_real:
            frags.append((FAIL, f"dirty={dirty_real}"))
        if _as_int(rs.get("ahead")):
            frags.append((WARN, f"ahead={rs['ahead']}"))
        if _as_int(rs.get("behind")):
            frags.append((FAIL, f"behind={rs['behind']}"))

    pr = body.get("open_prs") or {}
    if _as_int(pr.get("open_count")):
        n = _as_int(pr.get("open_count"))
        age = _as_int(pr.get("max_age_days"))
        if age >= 30:
            frags.append((FAIL, f"PR×{n} ({age}d)"))
        elif age >= 7:
            frags.append((WARN, f"PR×{n} ({age}d)"))
        else:
            frags.append((INFO, f"PR×{n}"))

    if not frags:
        return OK, "clean / nominal"
    # State reflects the OBSERVED signals; an "n/a here" annotation never drags
    # a row with a real green CI down to unobserved. A row that is *only*
    # unobserved fragments stays unobserved.
    observed = [s for s, _ in frags if s != UNOBS]
    state = _worst(observed) if observed else UNOBS
    label = "  ".join(t for _, t in frags)
    return state, label


def _repo_section(
    key: str, title: str, repos: dict, want_lib: bool, *, unobserved: Sequence[str]
) -> Section | None:
    rows: list[tuple[str, str, str]] = []  # (state, name, label)
    for name, body in sorted(repos.items()):
        if not isinstance(body, dict):
            continue
        if _is_library(name, body) != want_lib:
            continue
        # Workspaces are gated only for a handful of groups; skip ungrouped noise.
        if not want_lib and _repo_group(body) not in GATED_WORKSPACE_GROUPS:
            continue
        state, label = _lib_row(name, body, unobserved=unobserved)
        rows.append((state, name, label))
    if not rows:
        return None
    overall = _worst(s for s, _, _ in rows)
    n_bad = sum(1 for s, _, _ in rows if s in (FAIL, WARN))
    summary = f"{len(rows)} repos" + (f", {n_bad} need attention" if n_bad else " nominal")
    details = [f"{name:<26} {label}" for _, name, label in rows]
    return Section(key=key, title=title, state=overall, summary=summary, details=details)


def build_board(
    snapshot: dict | None,
    verdict: dict | None,
    validation: dict | None = None,
    *,
    unobserved: Sequence[str] = (),
    now: datetime.datetime | None = None,
    stale_after: int = STALE_AFTER_SECONDS,
) -> Board:
    """Assemble the format-agnostic :class:`Board`. Pure; never raises."""
    snapshot = snapshot or {}
    verdict = verdict or {}
    unobserved = tuple(unobserved)
    repos = snapshot.get("repos", {}) or {}
    ts = snapshot.get("ts") or verdict.get("ts") or ""
    age = _age_seconds(ts, now)
    stale = age is not None and age > stale_after

    v = str(verdict.get("verdict", "green")).lower()
    score = _as_int(verdict.get("score", 0))
    red = list(verdict.get("red_reasons") or [])
    yellow = list(verdict.get("yellow_reasons") or [])
    stale_reasons = list(verdict.get("stale_reasons") or [])

    sections: list[Section] = []

    lib_sec = _repo_section("libraries", "Libraries", repos, True, unobserved=unobserved)
    if lib_sec:
        sections.append(lib_sec)
    ws_sec = _repo_section("workspaces", "Workspaces", repos, False, unobserved=unobserved)
    if ws_sec:
        sections.append(ws_sec)

    # Worktree drift ---------------------------------------------------------
    if "worktree_drift" in unobserved:
        sections.append(_unobs_section("worktree_drift", "Worktree drift"))
    else:
        wt = snapshot.get("worktree_drift") or {}
        if wt:
            orphans = wt.get("orphans", []) or []
            missing = wt.get("missing", []) or []
            dirty = wt.get("dirty", []) or []
            if dirty or missing:
                st = FAIL
                summary = f"{len(orphans)} orphan / {len(missing)} missing / {len(dirty)} dirty"
            elif orphans:
                st, summary = WARN, f"{len(orphans)} orphan dir(s) (clean)"
            else:
                st, summary = OK, "no drift"
            details = [
                f"{d.get('worktree')}/{d.get('repo')}: {d.get('dirty_files')} dirty"
                for d in dirty[:5]
            ]
            sections.append(Section("worktree_drift", "Worktree drift", st, summary, details))

    # Script timing ----------------------------------------------------------
    if "script_timing" in unobserved:
        sections.append(_unobs_section("script_timing", "Script timing"))
    else:
        timing = snapshot.get("script_timing") or {}
        if timing:
            r = _as_int(timing.get("red_count"))
            y = _as_int(timing.get("yellow_count"))
            g = _as_int(timing.get("green_count"))
            if r:
                st, summary = FAIL, f"{r} regressions (>3× baseline), {y} slow (>1.5×)"
            elif y:
                st, summary = WARN, f"{y} scripts >1.5× baseline, {g} within"
            else:
                st, summary = OK, f"{g} within baseline"
            details = [
                f"✗ {e['project']}/{e['file'].split('/')[-1]}  "
                f"{e['latest_seconds']:.1f}s vs {e['baseline_seconds']:.1f}s ({e['ratio']}×)"
                for e in (timing.get("red") or [])[:5]
            ]
            sections.append(Section("script_timing", "Script timing", st, summary, details))

    # Import timing (advisory; off-tick daily) --------------------------------
    if "import_time" in unobserved:
        sections.append(_unobs_section("import_time", "Import timing"))
    else:
        imp = snapshot.get("import_time") or {}
        if imp:
            r = _as_int(imp.get("red_count"))
            y = _as_int(imp.get("yellow_count"))
            g = _as_int(imp.get("green_count"))
            if r:
                st, summary = FAIL, f"{r} import regressions (>3× baseline), {y} slow (>1.5×)"
            elif y:
                st, summary = WARN, f"{y} imports >1.5× baseline, {g} within"
            elif not _as_int(imp.get("packages_measured")):
                st, summary = WARN, "no libraries importable (set HYGIENE_PYTHON)"
            else:
                st, summary = OK, f"{g} imports within baseline"
            details = [
                f"✗ {e['package']}  "
                f"{e['latest_seconds']:.2f}s vs {e['baseline_seconds']:.2f}s ({e['ratio']}×)"
                for e in (imp.get("red") or [])[:5]
            ]
            sections.append(Section("import_time", "Import timing", st, summary, details))

    # Unit-test timing (advisory; off-tick) -----------------------------------
    if "unit_test_timing" in unobserved:
        sections.append(_unobs_section("unit_test_timing", "Unit-test timing"))
    else:
        ut = snapshot.get("unit_test_timing") or {}
        if ut:
            r = _as_int(ut.get("red_count"))
            y = _as_int(ut.get("yellow_count"))
            g = _as_int(ut.get("green_count"))
            if r:
                st, summary = FAIL, f"{r} test regressions (>3× baseline), {y} slow (>1.5×)"
            elif y:
                st, summary = WARN, f"{y} tests >1.5× baseline, {g} within"
            elif not _as_int(ut.get("repos_measured")):
                st, summary = WARN, "no suite runnable (set HYGIENE_PYTHON / PYAUTO_ROOT)"
            else:
                st, summary = OK, f"{g} tracked tests within baseline"
            details = [
                f"✗ {e['repo']}  {e['test'].split('::')[-1]}  "
                f"{e['latest_seconds']:.2f}s vs {e['baseline_seconds']:.2f}s ({e['ratio']}×)"
                for e in (ut.get("red") or [])[:5]
            ]
            sections.append(Section("unit_test_timing", "Unit-test timing", st, summary, details))

    # Profiling pinned-value drift -------------------------------------------
    if "profiling_drift" in unobserved:
        sections.append(_unobs_section("profiling_drift", "Profiling drift"))
    else:
        drift = snapshot.get("profiling_drift") or {}
        if drift:
            n = _as_int(drift.get("drift_count"))
            scanned = _as_int(drift.get("files_scanned"))
            if not drift.get("observed"):
                st, summary = WARN, "autolens_profiling/results not found"
                details = []
            elif n:
                st, summary = WARN, f"{n} result(s) drifted from pinned baseline"
                details = [
                    f"✗ {f.get('path')}  "
                    f"[{', '.join(str(d.get('label')) for d in (f.get('drift') or []))}]"
                    for f in (drift.get("findings") or [])[:5]
                ]
            else:
                st, summary = OK, f"{scanned} results clean"
                details = []
            sections.append(
                Section("profiling_drift", "Profiling drift", st, summary, details)
            )

    # Test run ---------------------------------------------------------------
    if "test_run" in unobserved:
        sections.append(_unobs_section("test_run", "Test run"))
    else:
        tr = snapshot.get("test_run") or {}
        if tr:
            ready = tr.get("ready")
            counts = (
                f"{_as_int(tr.get('passed'))}p / {_as_int(tr.get('failed'))}f / "
                f"{_as_int(tr.get('skipped'))}s @ {tr.get('run_label', '?')}"
            )
            if ready is False or _as_int(tr.get("failed")):
                st, summary = FAIL, f"NOT ready — {counts}"
            elif ready is True:
                st, summary = OK, f"ready — {counts}"
            else:
                st, summary = WARN, f"ready unknown — {counts}"
            details = []
            stale_n = _as_int(tr.get("parked_stale_count"))
            if stale_n:
                details.append(f"{stale_n} stale parked script(s)")
            sections.append(Section("test_run", "Test run", st, summary, details))

    # Version skew -----------------------------------------------------------
    if "version_skew" in unobserved:
        sections.append(_unobs_section("version_skew", "Version skew"))
    else:
        skew = (snapshot.get("version_skew") or {}).get("workspaces") or []
        off = [w for w in skew if isinstance(w, dict) and w.get("status") not in ("MATCH", None)]
        blocking = [w for w in off if str(w.get("status")).upper() in ("AHEAD", "MISMATCH", "BAD")]
        if off:
            st = FAIL if blocking else WARN
            summary = f"{len(blocking)} blocking" if blocking else f"{len(off)} skewed"
            details = [
                f"{w.get('status')}: {w.get('workspace')} pinned {w.get('pinned')} "
                f"vs installed {w.get('installed')}"
                for w in off[:8]
            ]
            sections.append(Section("version_skew", "Version skew", st, summary, details))
        elif skew:
            sections.append(Section("version_skew", "Version skew", OK, "all workspaces in sync", []))

    # Install verification ---------------------------------------------------
    vi = snapshot.get("verify_install") or {}
    if isinstance(vi, dict) and "ready" in vi:
        if vi.get("ready") is False:
            fails = [c.get("check") for c in (vi.get("checks") or [])
                     if str(c.get("status")).upper() == "FAIL"]
            summary = f"FAILED ({', '.join(map(str, fails)) or '?'})  ({vi.get('ts', '?')})"
            sections.append(Section("verify_install", "Install verify", FAIL, summary, []))
        else:
            sections.append(Section("verify_install", "Install verify", OK,
                                    f"passed (last run {vi.get('ts', '?')})", []))

    # Release validation -----------------------------------------------------
    vr = validation if isinstance(validation, dict) and validation else (snapshot.get("validation_report") or {})
    if isinstance(vr, dict) and vr:
        ready = vr.get("release_ready")
        ver = vr.get("testpypi_version") or "?"
        profile = vr.get("profile") or "?"
        stages = vr.get("stages") or {}
        meta = f"v{ver}  profile={profile}  ({vr.get('ts', '?')})"
        if ready is False:
            st, summary = FAIL, f"NOT release_ready — {meta}"
        elif ready is True:
            st, summary = OK, f"release_ready — {meta}"
        else:
            st, summary = WARN, f"release_ready unknown — {meta}"
        details = [f"stages: " + ", ".join(f"{n}:{s.get('status', '?')}" for n, s in stages.items())] \
            if stages else []
        sections.append(Section("release_validation", "Release validation", st, summary, details))

    # URL hygiene (monitoring only) -----------------------------------------
    uc = snapshot.get("url_check") or {}
    if isinstance(uc, dict) and uc.get("repos"):
        total = _as_int(uc.get("total_findings"))
        dirty = [r for r in uc["repos"] if _as_int(r.get("findings")) > 0]
        if dirty:
            summary = f"{total} forbidden pattern(s) in {len(dirty)} repo(s)  (swept {uc.get('ts', '?')})"
            details = [f"{r['repo']}: {r['findings']}" for r in dirty[:8]]
            sections.append(Section("url_check", "URL hygiene", WARN, summary, details))
        else:
            sections.append(Section("url_check", "URL hygiene", OK,
                                    f"{len(uc['repos'])} repos clean (swept {uc.get('ts', '?')})", []))

    return Board(
        verdict=v,
        score=score,
        ts=ts,
        age_seconds=age,
        stale=stale,
        red_reasons=red,
        yellow_reasons=yellow,
        sections=sections,
        stale_reasons=stale_reasons,
    )


def _unobs_section(key: str, title: str) -> Section:
    return Section(key, title, UNOBS, "not observed here (dev-box only)", [])


# --- readiness header (shared by term + readiness.render_block) --------------
def render_readiness_block(verdict: dict[str, Any], *, quiet: bool = False) -> list[str]:
    """The coloured RELEASE READINESS header lines (one source of truth)."""
    v = str(verdict.get("verdict", "green")).lower()
    score = _as_int(verdict.get("score", 0))
    state = _VERDICT_STATE.get(v, OK)
    word = _colour(state, _VERDICT_WORD.get(v, "GREEN"))
    lines = [f"{c_info('RELEASE READINESS')}  {_glyph(state)} {word}  {c_meta(f'score {score}')}"]
    reds = verdict.get("red_reasons") or []
    yellows = verdict.get("yellow_reasons") or []
    stales = verdict.get("stale_reasons") or []
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
            shown += 1
    if shown < limit:
        for s in stales[: limit - shown]:
            lines.append("  " + c_info(f"? {s}"))
    return lines


# --- per-format projections --------------------------------------------------
def _render_term(board: Board, verdict: dict, *, quiet: bool) -> str:
    out: list[str] = []
    age_txt = format_age(board.age_seconds, stale=board.stale)
    age_c = c_warn(age_txt) if board.stale else c_meta(age_txt)
    out.append(c_bold("PyAutoHeart dashboard") + "  " + c_meta(f"snapshot {board.ts}  (") + age_c + c_meta(")"))
    if board.stale:
        out.append("  " + c_warn("! board is stale — run `pyauto-heart tick` for fresh numbers"))
    out.append("")
    out.extend(render_readiness_block(verdict, quiet=quiet))
    out.append("")
    for sec in board.sections:
        out.append(f"{_glyph(sec.state)} {c_info(sec.title.upper())}  {_colour(sec.state, sec.summary)}")
        if not quiet:
            for d in sec.details:
                out.append("    " + _colour(sec.state, d))
    return "\n".join(out)


def _render_oneline(board: Board) -> str:
    word = _VERDICT_WORD.get(board.verdict, "GREEN")
    state = _VERDICT_STATE.get(board.verdict, OK)
    dot = _colour(state, "●")
    if board.verdict == "red":
        tail = f"{len(board.red_reasons)} blockers"
    elif board.verdict == "yellow":
        tail = f"{len(board.yellow_reasons)} warnings"
    elif board.verdict == "stale":
        tail = f"{len(board.stale_reasons)} evidence gap(s)"
    else:
        tail = "all green"
    age = format_age(board.age_seconds, stale=board.stale)
    coloured_word = _colour(state, f"{word} {board.score}")
    return f"PyAuto {dot} {coloured_word}  {tail}  (tick {age})"


def _render_md(board: Board) -> str:
    word = _VERDICT_WORD.get(board.verdict, "GREEN")
    emoji = _STATE_MD[_VERDICT_STATE.get(board.verdict, OK)]
    age = format_age(board.age_seconds, stale=board.stale)
    lines = [
        f"## {emoji} PyAuto health — **{word}** (score {board.score})",
        "",
        f"_snapshot `{board.ts}` · {age}_"
        + ("  ⚠️ **stale — run `pyauto-heart tick`**" if board.stale else ""),
        "",
    ]
    if board.red_reasons:
        lines.append("**Blockers:** " + "; ".join(board.red_reasons[:6]))
        lines.append("")
    elif board.yellow_reasons:
        lines.append("**Warnings:** " + "; ".join(board.yellow_reasons[:6]))
        lines.append("")
    elif board.stale_reasons:
        lines.append("**Evidence gaps (re-run, don't fix):** "
                     + "; ".join(board.stale_reasons[:6]))
        lines.append("")
    lines += ["| | Check | Status |", "|--|--|--|"]
    for sec in board.sections:
        em = _STATE_MD[sec.state]
        lines.append(f"| {em} | {sec.title} | {_md_escape(sec.summary)} |")
    lines.append("")
    lines.append(f"[Full board]({PAGES_URL})")
    return "\n".join(lines)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def _render_html(board: Board) -> str:
    word = _VERDICT_WORD.get(board.verdict, "GREEN")
    vstate = _VERDICT_STATE.get(board.verdict, OK)
    age = format_age(board.age_seconds, stale=board.stale)
    rows = []
    for sec in board.sections:
        cls = _STATE_HTML[sec.state]
        details = ""
        if sec.details:
            items = "".join(f"<li>{_html.escape(d)}</li>" for d in sec.details)
            details = f"<ul class='det'>{items}</ul>"
        rows.append(
            f"<tr class='{cls}'><td class='dot'></td>"
            f"<td class='name'>{_html.escape(sec.title)}</td>"
            f"<td class='sum'>{_html.escape(sec.summary)}{details}</td></tr>"
        )
    reasons_html = ""
    reasons = board.red_reasons or board.yellow_reasons or board.stale_reasons
    if reasons:
        label = ("Blockers" if board.red_reasons
                 else "Warnings" if board.yellow_reasons
                 else "Evidence gaps")
        items = "".join(f"<li>{_html.escape(r)}</li>" for r in reasons[:8])
        reasons_html = f"<div class='reasons'><h2>{label}</h2><ul>{items}</ul></div>"
    stale_html = (
        "<p class='stale'>⚠️ This board is stale — the last tick is older than the "
        "freshness threshold; the numbers may not be current.</p>" if board.stale else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PyAuto health — {word}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; padding: 2rem 1rem; background: #0d1117; color: #c9d1d9; }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 .25rem; }}
  .verdict {{ display: inline-block; padding: .3rem .9rem; border-radius: 999px;
             font-weight: 700; letter-spacing: .04em; }}
  .verdict.ok {{ background: #1a7f37; color: #fff; }}
  .verdict.warn {{ background: #9e6a03; color: #fff; }}
  .verdict.fail {{ background: #b62324; color: #fff; }}
  .meta {{ color: #8b949e; margin: .5rem 0 1.25rem; font-size: .9rem; }}
  .stale {{ background: #341a00; color: #e3b341; padding: .5rem .75rem;
           border-radius: 6px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: .55rem .5rem; border-top: 1px solid #21262d; vertical-align: top; }}
  td.dot {{ width: 10px; }}
  td.dot::before {{ content: ""; display: inline-block; width: 10px; height: 10px;
                   border-radius: 50%; margin-top: .35rem; }}
  tr.ok td.dot::before {{ background: #3fb950; }}
  tr.warn td.dot::before {{ background: #d29922; }}
  tr.fail td.dot::before {{ background: #f85149; }}
  tr.unobs td.dot::before {{ background: #6e7681; }}
  tr.info td.dot::before {{ background: #58a6ff; }}
  td.name {{ font-weight: 600; white-space: nowrap; }}
  tr.unobs td.name, tr.unobs td.sum {{ color: #8b949e; }}
  ul.det {{ margin: .35rem 0 0; padding-left: 1.1rem; color: #8b949e;
           font-size: .85rem; }}
  .reasons {{ margin: 1.5rem 0; }}
  .reasons h2 {{ font-size: 1rem; }}
  footer {{ margin-top: 2rem; color: #8b949e; font-size: .8rem; }}
</style></head>
<body><div class="wrap">
  <h1>PyAuto organism health</h1>
  <p><span class="verdict {vstate}">{word} · score {board.score}</span></p>
  <p class="meta">snapshot {_html.escape(board.ts)} · {age}</p>
  {stale_html}
  {reasons_html}
  <table>{''.join(rows)}</table>
  <footer>Rendered by <code>heart/dashboard.py</code> — one renderer, many surfaces.
  Observer only: PyAutoHeart never writes outside its own repo/state.</footer>
</div></body></html>
"""


def to_dict(board: Board) -> dict[str, Any]:
    """The machine surface (fmt='json') the Health Agent + mobile consume."""
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": board.verdict,
        "score": board.score,
        "ts": board.ts,
        "age_seconds": board.age_seconds,
        "stale": board.stale,
        "red_reasons": board.red_reasons,
        "yellow_reasons": board.yellow_reasons,
        "pages_url": PAGES_URL,
        "sections": [
            {
                "key": s.key,
                "title": s.title,
                "state": s.state,
                "summary": s.summary,
                "details": s.details,
            }
            for s in board.sections
        ],
    }


def badge_endpoint(board: Board) -> dict[str, Any]:
    """A shields.io endpoint-badge payload (verdict colour). Auto-updating.

    Publish this as ``badge.json`` next to the Pages board and reference it via
    ``https://img.shields.io/endpoint?url=<pages>/badge.json`` so the README
    badge tracks the live verdict.
    """
    word = _VERDICT_WORD.get(board.verdict, "GREEN")
    return {
        "schemaVersion": 1,
        "label": "health",
        "message": f"{word} · {board.score}",
        "color": _BADGE_COLOR.get(board.verdict, "lightgrey"),
    }


def render(
    snapshot: dict | None,
    verdict: dict | None,
    validation: dict | None = None,
    *,
    fmt: str = "term",
    unobserved: Sequence[str] = (),
    now: datetime.datetime | None = None,
    quiet: bool = False,
    stale_after: int = STALE_AFTER_SECONDS,
) -> str:
    """Render the unified board in ``fmt``. Pure: snapshot in → string out."""
    board = build_board(
        snapshot, verdict, validation,
        unobserved=unobserved, now=now, stale_after=stale_after,
    )
    if fmt == "term":
        return _render_term(board, verdict or {}, quiet=quiet)
    if fmt == "oneline":
        return _render_oneline(board)
    if fmt == "md":
        return _render_md(board)
    if fmt == "html":
        return _render_html(board)
    if fmt == "json":
        return json.dumps(to_dict(board), indent=2, sort_keys=True)
    raise ValueError(f"unknown dashboard fmt: {fmt!r}")


# --- CLI shell (the only I/O in this module) --------------------------------
def main(argv: list[str] | None = None) -> int:
    """`pyauto-heart dashboard` — render the board from the CACHED snapshot.

    Reads cache only (never ticks) so it is instant. ``--oneline`` degrades
    cleanly to a hint when there is no state, so the venv/shell hook can source
    it without ever erroring or blocking the prompt.
    """
    import argparse
    import os

    from heart import readiness, state

    ap = argparse.ArgumentParser(prog="pyauto-heart dashboard")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--oneline", action="store_true", help="compact one-line summary (venv/prompt)")
    g.add_argument("--md", action="store_true", help="GitHub-flavoured markdown")
    g.add_argument("--html", action="store_true", help="standalone self-contained HTML page")
    g.add_argument("--json", action="store_true", help="the machine surface (Health Agent / mobile)")
    g.add_argument("--badge", action="store_true", help="emit a shields.io endpoint-badge JSON")
    ap.add_argument("--cloud", action="store_true",
                    help="mark local-only checks as 'not observed here' (cloud job vantage)")
    ap.add_argument("--quiet", action="store_true", help="suppress drill-down details (term)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    ap.add_argument("--stale-after", type=int, default=STALE_AFTER_SECONDS,
                    help=f"seconds before the board is flagged stale (default {STALE_AFTER_SECONDS})")
    ns = ap.parse_args(argv)
    if ns.no_color:
        os.environ["NO_COLOR"] = "1"

    fmt = "term"
    for name in ("oneline", "md", "html", "json"):
        if getattr(ns, name):
            fmt = name
            break
    if ns.badge:
        fmt = "badge"   # so the no-cache fallback below emits an "unknown" badge

    snapshot = state.load()
    if snapshot is None:
        # No cache. The one-line hook must never error or block the prompt.
        if fmt == "oneline":
            print("PyAuto ○ no fresh state (run `pyauto-heart tick`)")
            return 0
        if fmt == "badge":
            print(json.dumps({"schemaVersion": 1, "label": "health",
                              "message": "unknown", "color": "lightgrey"}))
            return 0
        print("no cache yet — run `pyauto-heart tick` first", file=sys.stderr)
        return 2

    verdict = readiness.load_verdict()
    validation = snapshot.get("validation_report") or {}
    unobserved = LOCAL_ONLY_FAMILIES if ns.cloud else ()

    if ns.badge:
        board = build_board(snapshot, verdict, validation,
                            unobserved=unobserved, stale_after=ns.stale_after)
        print(json.dumps(badge_endpoint(board)))
        return 0

    print(render(snapshot, verdict, validation, fmt=fmt, unobserved=unobserved,
                 quiet=ns.quiet, stale_after=ns.stale_after))
    return 0


if __name__ == "__main__":
    sys.exit(main())
