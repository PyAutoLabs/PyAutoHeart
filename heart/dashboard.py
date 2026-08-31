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
            | "md"       # GitHub-flavoured markdown (step summary / issue)
            | "md-brief" # the README strip: verdict + linked blockers + board link
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
"not observed here" instead of silently showing them green. The dev box
enriches the SAME page — never a second, competing page — via
``pyauto-heart publish`` (heart/publish.py), which commits a distilled
``state/devbox_board.json`` this renderer merges in (``devbox=``), each row
age-stamped "observed Nh ago on the dev box" and falling back to unobserved
once the observation is older than ``DEVBOX_FRESH_SECONDS``.

**Actionable, not just readable.** Every blocker/warning is also structured
(``Board.blockers``: text, repo, run url, and a copyable ``/bug`` prompt), and
sections that need a hand carry an ``action`` — the exact command or Claude
prompt to copy. An evidence gap carries more: the ``command`` that re-runs the
check behind it (looked up from ``STALE_REMEDIES`` by the readiness gate key,
never guessed from the sentence), and the whole stale tier carries one
``stale_plan`` — a single prompt, and where possible a single command chain,
that closes every current gap at once. The html surface renders these as one-tap 📋 buttons (the
PyAutoMind dashboard pattern); md links them; json carries them verbatim.
"""

from __future__ import annotations

import datetime
import html as _html
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from heart import validate
from heart.checks.test_run import counts_measured as tr_counts_measured
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
    "workspace_testmode_timing",
    "version_skew",
)

# The board advertises its own age; older than this (seconds) is "stale" — a
# cached board that does not flag its own staleness is a footgun. The daemon
# ticks every ~5 min, so an hour without a fresh tick warrants a nudge.
STALE_AFTER_SECONDS = 3600

# A published dev-box observation older than this renders as unobserved again
# (with its age) rather than as live data — a two-day-old drift report shown
# as current would be worse than the honest grey row.
DEVBOX_FRESH_SECONDS = 48 * 3600

# GitHub owner per repo, for repo/run links on blockers. Derived from the
# declared config surface (config/repos.yaml `owner:`), never hardcoded —
# the tenant firewall keeps instance facts out of organ code.
def _repo_owners() -> dict:
    import pathlib

    import yaml

    cfg_path = pathlib.Path(__file__).resolve().parents[1] / "config" / "repos.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    owners: dict = {}
    for group in (cfg.get("repos") or {}).values():
        for r in group if isinstance(group, list) else []:
            if isinstance(r, dict) and r.get("name") and r.get("owner"):
                owners[str(r["name"])] = str(r["owner"])
    return owners


REPO_OWNERS = _repo_owners()

# One line per local-only family on WHAT the dev box would observe — shown on
# the grey rows so "not observed here" is a fact with a remedy, not a shrug.
UNOBS_WATCHES = {
    "worktree_drift": "task worktrees vs the active.md ledger (orphans, missing, dirty)",
    "script_timing": "workspace script runtimes vs their baselines",
    "import_time": "library import costs vs their baselines",
    "unit_test_timing": "the slowest unit tests vs their baselines",
    "profiling_drift": "pinned profiling results vs their baselines",
    "workspace_testmode_timing": "TEST_MODE workspace script runtimes vs their baselines",
    "test_run": "the latest full workspace test-run verdict",
    "version_skew": "workspace version floors vs the newest releases",
}

# What 📋 on a grey row copies: observe the family locally, then publish the
# distilled observation so this page fills in.
OBSERVE_ACTION = {"label": "observe on the dev box",
                  "payload": "pyauto-heart tick && pyauto-heart publish"}

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

# The family look lives once, in the Brain (``board/_theme.py``): the
# stylesheet, the hero that redraws this organ's logo as a mark, and the
# cross-board footer. Imported rather than copied, so the look moves for the
# whole family at once — heart-health.yml checks PyAutoBrain out beside this
# repo, and a local run finds the sibling checkout the way the other PyAuto
# tools resolve each other.
HEART_HOME = pathlib.Path(__file__).resolve().parents[1]
BOARD_KEY = "heart"  # this board's entry in the Brain's palette table


def _workspace_root() -> pathlib.Path:
    """Where the sibling PyAuto checkouts live: `$PYAUTO_ROOT`, else `~/Code`.

    The org's own directory name is an instance fact, so it is never written
    here — a workspace that does not follow the default sets `$PYAUTO_ROOT`
    (the same variable the dev-flow doors read).
    """
    return pathlib.Path(os.environ.get("PYAUTO_ROOT") or pathlib.Path.home() / "Code")


def theme():
    """The shared theme module, or a RuntimeError naming the fix.

    Only the html surface needs it; the md/json/badge surfaces never call
    here, so the Health Agent keeps working with no PyAutoBrain in reach.
    """
    for cand in (os.environ.get("PYAUTO_BRAIN"), HEART_HOME / "PyAutoBrain",
                 HEART_HOME.parent / "PyAutoBrain",
                 _workspace_root() / "PyAutoBrain"):
        if not cand:
            continue
        board_dir = pathlib.Path(cand) / "board"
        if (board_dir / "_theme.py").is_file():
            if str(board_dir) not in sys.path:
                sys.path.insert(0, str(board_dir))
            import _theme
            return _theme
    raise RuntimeError(
        "the shared board theme (PyAutoBrain/board/_theme.py) is not in reach "
        "— check PyAutoBrain out beside this repo or set PYAUTO_BRAIN")

# The one-tap board family — the cross-board footer nav every board carries,
# each board skipping its own entry. The base comes from PAGES_URL so the
# owner is named exactly once in this file.
BOARD_FAMILY = (("mind", "PyAutoMind"), ("brain", "PyAutoBrain"),
                ("hands", "PyAutoHands"), ("memory", "PyAutoMemory"),
                ("organism", "PyAutoScientist"))


def _boards_nav_html() -> str:
    """The cross-board footer — one chip per sibling, each in its own organ's
    colour (the theme owns the chip palette; this board owns the URLs)."""
    base = PAGES_URL.rsplit("/", 2)[0]
    links = {key: f"{base}/{repo}/" for key, repo in BOARD_FAMILY}
    return theme().boards_footer(links, BOARD_KEY)

# v2: sections gained links/action/observed_ago; the board gained structured
# `blockers` ({text, severity, repo, repo_url, run_url, prompt}). Additive.
# v3: a blocker gained `command` (the shell remedy behind a stale row, None
# when the gap needs a conversation) and the board gained `stale_plan` — the
# ONE payload that closes every current evidence gap. Additive.
SCHEMA_VERSION = 3


@dataclass
class Section:
    """One board row: a topic, its worst-of state, a summary, and details."""

    key: str
    title: str
    state: str
    summary: str
    details: list[str] = field(default_factory=list)
    # {label, url} — e.g. the failing CI runs behind a red repo group.
    links: list[dict] = field(default_factory=list)
    # {label, payload} — what a 📋 button copies for this row (a command or a
    # Claude prompt), or None when the row needs no hand.
    action: dict | None = None
    # "observed 6h ago on the dev box" when this row came from a published
    # dev-box observation rather than this render's own snapshot.
    observed_ago: str | None = None


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
    # The reasons above, STRUCTURED: {text, severity, repo, repo_url, run_url,
    # prompt}. The flat lists stay for compatibility; this is what the html
    # 📋 buttons, the md links, and the json consumers act on.
    blockers: list[dict] = field(default_factory=list)
    # The ⏱ performance surface: CI wall-clock gates + their history, hang/kill
    # events, and the NO_RUN census — the block the Brain board consumes
    # verbatim. Every actionable row inside carries its own ready-to-paste
    # prompt; the consumer never re-derives one. None when neither timing slice
    # was observed (an absent block is honest; an empty one implies "measured,
    # nothing there").
    performance: dict | None = None
    # {count, command, prompt}: one copyable plan that closes EVERY current
    # evidence gap, so a STALE board is one tap from being cleared instead of
    # N. None when nothing is stale, or when no gap has a known remedy.
    stale_plan: dict | None = None


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


# The sparkline ramp. Eight levels is enough resolution for a 30-point daily
# history and narrow enough to sit inside a table cell on a phone.
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[Any]) -> str:
    """A unicode sparkline over ``values``; "" under two points.

    Pure and defensive: non-numeric entries are dropped, and a flat series
    renders mid-ramp rather than dividing by zero. One point is not a trend —
    the empty string keeps the board from drawing a shape that means nothing
    (the "every stored history is one value repeated seven times" lesson).
    """
    nums = [float(v) for v in (values or []) if isinstance(v, (int, float))]
    if len(nums) < 2:
        return ""
    lo, hi = min(nums), max(nums)
    if hi <= lo:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(nums)
    span = hi - lo
    last = len(SPARK_CHARS) - 1
    return "".join(
        SPARK_CHARS[min(last, int((n - lo) / span * len(SPARK_CHARS)))] for n in nums
    )


def _dur(seconds: Any) -> str:
    """Compact wall-clock: minutes above a minute, seconds below it."""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "?"
    if seconds < 60:
        return f"{int(round(seconds))}s"
    return f"{int(round(seconds / 60))}m"


def _gate_spark(history: Sequence[Any], key: str) -> str:
    """The sparkline of one gate's daily p50s, oldest first."""
    values: list[float] = []
    for entry in sorted(
        (e for e in (history or []) if isinstance(e, dict)),
        key=lambda e: str(e.get("date") or ""),
    ):
        row = (entry.get("gates") or {}).get(key) if isinstance(entry.get("gates"), dict) else None
        if isinstance(row, dict) and isinstance(row.get("p50_s"), (int, float)):
            values.append(float(row["p50_s"]))
    return sparkline(values)


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
    # A failed CI query is not a CI state: say so, rather than letting it read
    # as a pending run. Without this the panel showed "CI in_progress" on every
    # repo at once when the underlying `gh` call was simply broken.
    if ci.get("error"):
        return WARN, "CI unavailable (query failed)"
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
    links: list[dict] = []
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
        # The way OUT of a red row: the failing run itself, plus a ready-made
        # /bug prompt (rendered as the link's paired 📋 on the html surface).
        ci = body.get("ci_status") or {}
        if (state == FAIL and ci.get("url")
                and str(ci.get("conclusion") or "") not in ("", "success")):
            wf = ci.get("workflow") or "CI"
            links.append({
                "label": f"{name} run", "url": str(ci["url"]),
                "prompt": (f"/bug Heart board: {name} {wf} failing on main — "
                           f"failing run: {ci['url']}"),
            })
    if not rows:
        return None
    overall = _worst(s for s, _, _ in rows)
    n_bad = sum(1 for s, _, _ in rows if s in (FAIL, WARN))
    summary = f"{len(rows)} repos" + (f", {n_bad} need attention" if n_bad else " nominal")
    details = [f"{name:<26} {label}" for _, name, label in rows]
    return Section(key=key, title=title, state=overall, summary=summary,
                   details=details, links=links[:4])


def build_board(
    snapshot: dict | None,
    verdict: dict | None,
    validation: dict | None = None,
    *,
    unobserved: Sequence[str] = (),
    now: datetime.datetime | None = None,
    stale_after: int = STALE_AFTER_SECONDS,
    devbox: dict | None = None,
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
    # The gate key behind each stale reason (readiness.py `stale_details`),
    # index for index — the identity a remedy is looked up by. A verdict from
    # an older Heart carries no such key; the rows then fall back to the
    # generic re-run nudge and no plan is offered (never a guessed remedy).
    stale_keys = [
        str(d.get("key") or "")
        for d in (verdict.get("stale_details") or [])
        if isinstance(d, dict)
    ]
    if len(stale_keys) != len(stale_reasons):
        stale_keys = []

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
            canonical = wt.get("canonical_dirty", []) or []
            parked = wt.get("parked", []) or []
            if dirty or missing:
                st = FAIL
                summary = f"{len(orphans)} orphan / {len(missing)} missing / {len(dirty)} dirty"
            elif orphans or canonical:
                # Canonical-checkout dirt is the user's own working state, not
                # task drift — a caution with its own label, never a red.
                bits = []
                if orphans:
                    bits.append(f"{len(orphans)} orphan dir(s) (clean)")
                if canonical:
                    bits.append(f"{len(canonical)} canonical checkout(s) dirty")
                st, summary = WARN, " / ".join(bits)
            else:
                st = OK
                summary = "no drift" + (f" ({len(parked)} parked)" if parked else "")
            details = [
                f"{d.get('worktree')}/{d.get('repo')}: {d.get('dirty_files')} dirty"
                for d in dirty[:5]
            ] + [
                f"canonical {d.get('repo')}: {d.get('dirty_files')} dirty"
                for d in canonical[:5]
            ]
            action = ({"label": "triage the drift", "payload": "pyauto-heart fix drift"}
                      if st in (FAIL, WARN) else None)
            sections.append(Section("worktree_drift", "Worktree drift", st, summary,
                                    details, action=action))

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
            action = None
            if st in (FAIL, WARN):
                top = (timing.get("red") or timing.get("yellow") or [{}])[0]
                proj = top.get("project")
                if proj:
                    action = {"label": "bundle the regression context",
                              "payload": f"pyauto-heart fix timing {proj}"}
            sections.append(Section("script_timing", "Script timing", st, summary,
                                    details, action=action))

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

    # Workspace test-mode script timing (advisory; off-tick) ------------------
    if "workspace_testmode_timing" in unobserved:
        sections.append(_unobs_section("workspace_testmode_timing", "Workspace test-mode timing"))
    else:
        wt = snapshot.get("workspace_testmode_timing") or {}
        if wt:
            r = _as_int(wt.get("red_count"))
            y = _as_int(wt.get("yellow_count"))
            g = _as_int(wt.get("green_count"))
            if r:
                st, summary = FAIL, f"{r} script regressions (>3× baseline), {y} slow (>1.5×)"
            elif y:
                st, summary = WARN, f"{y} scripts >1.5× baseline, {g} within"
            elif not _as_int(wt.get("scripts_measured")):
                st, summary = WARN, "no script runnable (set HYGIENE_PYTHON / PYAUTO_ROOT)"
            else:
                st, summary = OK, f"{g} scripts within baseline (TEST_MODE)"
            details = [
                f"✗ {e['script'].split('/')[-1]}  "
                f"{e['latest_seconds']:.1f}s vs {e['baseline_seconds']:.1f}s ({e['ratio']}×)"
                for e in (wt.get("red") or [])[:5]
            ]
            sections.append(Section("workspace_testmode_timing", "Workspace test-mode timing", st, summary, details))

    # Test run ---------------------------------------------------------------
    if "test_run" in unobserved:
        sections.append(_unobs_section("test_run", "Test run"))
    else:
        tr = snapshot.get("test_run") or {}
        if tr:
            ready = tr.get("ready")
            measured = tr_counts_measured(tr)
            if measured:
                counts = (
                    f"{_as_int(tr.get('passed'))}p / {_as_int(tr.get('failed'))}f / "
                    f"{_as_int(tr.get('skipped'))}s @ {tr.get('run_label', '?')}"
                )
            else:
                # Conclusion-only verdict: never render fabricated zeros.
                counts = f"counts not ingested @ {tr.get('run_label', '?')}"
            if ready is False or (measured and _as_int(tr.get("failed"))):
                st, summary = FAIL, f"NOT ready — {counts}"
            elif ready is True:
                st, summary = OK, f"ready — {counts}"
            else:
                st, summary = WARN, f"ready unknown — {counts}"
            details = []
            for s in (tr.get("failing_scripts") or [])[:3]:
                if isinstance(s, dict):
                    details.append(
                        f"✗ {s.get('project')} {s.get('script')} ({s.get('status')})"
                    )
            stale_n = _as_int(tr.get("parked_stale_count"))
            if stale_n:
                details.append(f"{stale_n} stale parked script(s)")
            sections.append(Section("test_run", "Test run", st, summary, details))

    # CI wall-clock + the NO_RUN census (the ⏱ performance surface) -----------
    # Both are CLOUD-observed (the Actions API + the contents API), so they are
    # deliberately NOT in LOCAL_ONLY_FAMILIES: the scheduled job measures them
    # first-hand and must not mark them "not observed here". Timing rows are
    # advisory — they never move the readiness verdict.
    performance = _performance_sections(snapshot, sections)

    # Version skew -----------------------------------------------------------
    if "version_skew" in unobserved:
        sections.append(_unobs_section("version_skew", "Version skew"))
    else:
        skew = (snapshot.get("version_skew") or {}).get("workspaces") or []
        off = [w for w in skew if isinstance(w, dict) and w.get("status") not in ("OK", None)]
        blocking = [w for w in off if str(w.get("status")).upper() in ("UNSATISFIABLE", "BAD")]
        if off:
            st = FAIL if blocking else WARN
            summary = f"{len(blocking)} blocking" if blocking else f"{len(off)} unresolved"
            details = [
                f"{w.get('status')}: {w.get('workspace')} floor {w.get('floor')} "
                f"vs newest {w.get('library')} release {w.get('newest_release')}"
                for w in off[:8]
            ]
            sections.append(Section("version_skew", "Version skew", st, summary, details))
        elif skew:
            sections.append(Section("version_skew", "Version skew", OK, "all floors satisfiable", []))

    # Version skew — PyPI yank leg (deep `version_skew --pypi`; the slice is
    # absent until that on-demand probe has run, so no section = not yet run) --
    skew_pypi = (snapshot.get("version_skew_pypi") or {}).get("workspaces") or []
    pypi_off = [w for w in skew_pypi if isinstance(w, dict) and w.get("status") not in ("OK", None)]
    pypi_blocking = [w for w in pypi_off if str(w.get("status")).upper() in ("UNSATISFIABLE", "BAD")]
    if pypi_off:
        st = FAIL if pypi_blocking else WARN
        summary = f"{len(pypi_blocking)} blocking" if pypi_blocking else f"{len(pypi_off)} unresolved"
        details = [
            f"{w.get('status')}: {w.get('workspace')} floor {w.get('floor')} "
            f"({w.get('package')} on PyPI)"
            for w in pypi_off[:8]
        ]
        sections.append(Section("version_skew_pypi", "Version skew (PyPI)", st, summary, details))
    elif skew_pypi:
        sections.append(Section("version_skew_pypi", "Version skew (PyPI)", OK, "all floors installable", []))

    # Install verification ---------------------------------------------------
    vi = snapshot.get("verify_install") or {}
    if isinstance(vi, dict) and "ready" in vi:
        index = str(vi.get("index") or "index unknown")
        if vi.get("ready") is False:
            fails = [c.get("check") for c in (vi.get("checks") or [])
                     if str(c.get("status")).upper() == "FAIL"]
            summary = (
                f"FAILED ({index}; {', '.join(map(str, fails)) or '?'})  "
                f"({vi.get('ts', '?')})"
            )
            sections.append(Section("verify_install", "Install verify", FAIL, summary, []))
        elif index == "find-links":
            sections.append(Section(
                "verify_install",
                "Install verify",
                WARN,
                f"development-only (find-links; last run {vi.get('ts', '?')})",
                [],
            ))
        else:
            sections.append(Section("verify_install", "Install verify", OK,
                                    f"passed ({index}; last run {vi.get('ts', '?')})", []))

    # Release validation -----------------------------------------------------
    vr = validation if isinstance(validation, dict) and validation else (snapshot.get("validation_report") or {})
    if isinstance(vr, dict) and vr:
        ready = vr.get("release_ready")
        ver = vr.get("testpypi_version") or "?"
        profile = vr.get("profile") or "?"
        stages = vr.get("stages")
        stages = stages if isinstance(stages, dict) else {}
        meta = f"v{ver}  profile={profile}  ({vr.get('ts', '?')})"
        # Same normaliser the readiness gate uses, so this row can never
        # contradict the header verdict. `incomplete` is an evidence gap (WARN),
        # not a failure (FAIL) — a green integrate-only ingest lands there, and
        # a FAIL row beside a stale header reads as a broken release.
        outcome = validate.report_outcome(vr)
        if outcome == "fail":
            st, summary = FAIL, f"NOT release_ready — {meta}"
        elif outcome == "incomplete":
            st, summary = WARN, f"incomplete — no rehearsal evidence — {meta}"
        elif outcome == "pass":
            st, summary = OK, f"release_ready — {meta}"
        else:
            st, summary = WARN, f"release_ready unknown — {meta}"
        details = [f"stages: " + ", ".join(
            f"{n}:{s.get('status', '?') if isinstance(s, dict) else '?'}"
            for n, s in stages.items())] \
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

    sections = _devbox_enrich(sections, devbox, now)

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
        blockers=_structure_reasons(red, yellow, stale_reasons, repos, stale_keys),
        performance=performance,
        stale_plan=build_stale_plan(stale_reasons, stale_keys),
    )


def _ci_timing_section(ct: dict, gates: list[dict], events: list[dict],
                       errors: list, history: list[dict]) -> Section:
    """The CI wall-clock row: how long the gates a change must pass now take."""
    timed = [g for g in gates if _as_int(g.get("runs_counted"))]
    warned = [g for g in gates if g.get("state") == "warn"]
    # Events (a hang, a kill-timer suspect) are the only hard rows here; a
    # slowed gate is advisory by construction — an alarm that cries wolf on
    # timing jitter gets ignored, and then so does the real one.
    if events:
        st = FAIL
    elif warned or errors:
        st = WARN
    else:
        st = OK

    bits = [f"{len(timed)} gates"]
    slowest = max(timed, key=lambda g: g.get("median_s") or 0, default=None)
    if slowest:
        bits.append(f"slowest {slowest.get('repo')} {slowest.get('workflow')} "
                    f"{_dur(slowest.get('median_s'))}")
    if warned:
        bits.append(f"{len(warned)} slowed")
    if events:
        bits.append(f"{len(events)} hang event" + ("" if len(events) == 1 else "s"))
    if errors:
        bits.append(f"{len(errors)} unavailable")

    details = []
    for g in sorted(timed, key=lambda g: g.get("median_s") or 0, reverse=True)[:5]:
        # Coverage beside time, always: a speed row without the run count
        # rewards a gate that got faster by running less.
        line = (f"{g.get('repo')} {g.get('workflow')}  p50 {_dur(g.get('median_s'))}  "
                f"max {_dur(g.get('max_s'))}  ({_as_int(g.get('runs_counted'))} runs)")
        spark = _gate_spark(history, f"{g.get('repo')}/{g.get('workflow')}")
        details.append(f"{line}  {spark}" if spark else line)

    links = [
        {"label": f"{e.get('repo')} {e.get('kind')}", "url": str(e.get("run_url")),
         "prompt": str(e.get("prompt") or "")}
        for e in events if e.get("run_url")
    ][:4]

    # A slowed gate has no run URL of its own (it is a distribution, not a run),
    # so its prompt rides the row's 📋 when exactly one gate slowed. More than
    # one and the board would have to pick a favourite — they all stay in the
    # `performance` block instead, where each carries its own prompt.
    action = None
    if len(warned) == 1 and warned[0].get("prompt"):
        action = {"label": "copy the slowdown prompt", "payload": str(warned[0]["prompt"])}

    return Section("ci_timing", "CI wall-clock", st, " · ".join(bits), details,
                   links=links, action=action)


def _no_run_section(totals: dict, rows: list[dict], repo_rows: list[dict]) -> Section:
    """The NO_RUN census row: what the release run is not running, and why."""
    slow = _as_int(totals.get("slow"))
    needs_fix = _as_int(totals.get("needs_fix"))
    permanent = _as_int(totals.get("permanent"))
    unmeasured = _as_int(totals.get("unmeasured_slow"))
    present = _as_int(totals.get("repos_present"))

    if unmeasured or needs_fix:
        st = WARN
    elif not slow and not needs_fix and permanent:
        st = INFO           # only correct-by-design skips: a fact, not a to-do
    else:
        st = OK
    summary = (f"{slow} SLOW ({unmeasured} unmeasured) / {needs_fix} NEEDS_FIX / "
               f"{permanent} permanent across {present} workspaces")

    details = [
        f"{r.get('repo')}: {r.get('entry')} {r.get('marker')} {r.get('date') or 'undated'}"
        for r in rows[:5]
    ]
    absent = [str(r.get("repo")) for r in repo_rows if not r.get("present")]
    if absent:
        # Honest data, not an error — some repos genuinely have no no_run.yaml.
        details.append(f"no no_run.yaml: {', '.join(absent[:4])}")

    action = None
    if rows and rows[0].get("prompt"):
        action = {"label": "copy the fix prompt", "payload": str(rows[0]["prompt"])}
    return Section("no_run_census", "NO_RUN census", st, summary, details, action=action)


def _performance_sections(snapshot: dict, sections: list[Section]) -> dict | None:
    """Append the two ⏱ rows (when observed) and return the `performance` block.

    The block is the contract the Brain board consumes verbatim: every
    actionable row inside it — a slowed gate, a hang event, a marker row —
    carries its own ready-to-paste prompt string, written by the producer and
    never re-derived by a renderer.
    """
    ct = snapshot.get("ci_timing")
    ct = ct if isinstance(ct, dict) else {}
    nr = snapshot.get("no_run_census")
    nr = nr if isinstance(nr, dict) else {}
    if not ct and not nr:
        return None

    gates = [g for g in (ct.get("gates") or []) if isinstance(g, dict)]
    events = [e for e in (ct.get("events") or []) if isinstance(e, dict)]
    history = [h for h in (ct.get("history") or []) if isinstance(h, dict)]
    errors = list(ct.get("errors") or [])
    totals = nr.get("totals") if isinstance(nr.get("totals"), dict) else {}
    rows = [r for r in (nr.get("rows") or []) if isinstance(r, dict)]
    repo_rows = [r for r in (nr.get("repos") or []) if isinstance(r, dict)]

    if ct:
        sections.append(_ci_timing_section(ct, gates, events, errors, history))
    if nr:
        sections.append(_no_run_section(totals, rows, repo_rows))

    return {
        "schema": 1,
        "gates": [
            {
                "repo": g.get("repo"),
                "workflow": g.get("workflow"),
                "median_s": g.get("median_s"),
                "pr_median_s": g.get("pr_median_s"),
                "max_s": g.get("max_s"),
                "runs_counted": _as_int(g.get("runs_counted")),
                "state": g.get("state") or OK,
                "prompt": g.get("prompt"),
                "actions_url": g.get("actions_url") or "",
                "spark": _gate_spark(history, f"{g.get('repo')}/{g.get('workflow')}"),
            }
            for g in gates
        ],
        "history": history,
        "events": events,
        "errors": errors,
        "no_run": {
            "totals": totals,
            "repos": repo_rows,
            "rows": rows[:10],
        },
    }


def _unobs_section(key: str, title: str) -> Section:
    watches = UNOBS_WATCHES.get(key)
    return Section(
        key, title, UNOBS,
        "not observed here — measured on the dev box",
        [f"watches {watches}"] if watches else [],
        action=dict(OBSERVE_ACTION),
    )


# --- what closes an evidence gap -------------------------------------------
#
# gate key (heart/readiness.py `stale_details`) -> the check that closes it.
# STALE's rule is the whole design here: every remedy RE-RUNS a check and none
# of them touches code — that is what separates the tier from yellow. `command`
# is what a terminal copies; it is None where the remedy genuinely needs a
# conversation (a release rehearsal is dispatched by the Brain's Release Agent,
# never by the Heart). `step` is the imperative phrase both the per-row prompt
# and the all-in-one plan are written from, so the two can never disagree.
TICK_CMD = "pyauto-heart tick"
VERIFY_INSTALL_CMD = "pyauto-heart verify_install --report-json"
REHEARSE_STEP = ("dispatch a release rehearsal with `/release rehearse`, then "
                 "`pyauto-heart validate --ingest <artifacts>`")

STALE_REMEDIES: dict[str, dict] = {
    # install verification — the deep pip/conda install-path check
    "install_unknown": {"command": VERIFY_INSTALL_CMD,
                        "step": f"run `{VERIFY_INSTALL_CMD}`"},
    "install_stale": {"command": VERIFY_INSTALL_CMD,
                      "step": f"re-run `{VERIFY_INSTALL_CMD}` (the evidence expired)"},
    "install_non_release": {
        "command": VERIFY_INSTALL_CMD,
        "step": (f"re-run `{VERIFY_INSTALL_CMD}` against PyPI/TestPyPI "
                 "(find-links evidence cannot satisfy a release gate)")},
    # the workspace validation surface — PyAutoHands writes the report, the
    # tick reads it, so the remedy names both halves.
    "test_unknown": {
        "command": TICK_CMD,
        "step": (f"re-read the latest workspace validation run with `{TICK_CMD}` "
                 "(run the suite first if PyAutoHands has no "
                 "`run_logs/latest/report.json`)")},
    "test_stale": {
        "command": TICK_CMD,
        "step": (f"re-run the workspace validation suite, then `{TICK_CMD}` "
                 "(the last report aged out)")},
    # the release-validation rehearsal — Brain-dispatched, so no command
    "validation_absent": {"command": None, "step": REHEARSE_STEP},
    "validation_stale": {"command": None,
                         "step": REHEARSE_STEP + " (the last rehearsal aged out)"},
    "validation_stale_sha": {
        "command": None,
        "step": REHEARSE_STEP + " (main moved since the last rehearsal)"},
    "validation_profile": {"command": None,
                           "step": REHEARSE_STEP + " under the `release` profile"},
    "validation_unknown": {"command": None,
                           "step": REHEARSE_STEP + " so the shipped source is confirmed"},
    # repo/CI evidence the tick re-polls
    "lib_unknown": {"command": TICK_CMD,
                    "step": f"re-poll the repo with `{TICK_CMD}`"},
    "lib_ci_unavailable": {
        "command": TICK_CMD,
        "step": f"re-poll CI with `{TICK_CMD}` (the query failed, not the CI)"},
    "skew_unknown": {"command": TICK_CMD,
                     "step": f"re-read the version floors with `{TICK_CMD}`"},
    "skew_pypi_unknown": {
        "command": None,
        "step": ("re-run the deep PyPI leg "
                 "(`python3 -m heart.checks.version_skew --pypi` from the Heart "
                 "checkout) once PyPI answers again")},
}

# What a stale row copies when its key has no entry here (a gap added since
# this table, or a verdict from an older Heart that carries no keys at all).
GENERIC_STALE_PROMPT = "/health re-run the stale evidence: {text}"


def stale_remedy(key: str) -> dict | None:
    """The remedy for one gate key, or None when this board has none."""
    remedy = STALE_REMEDIES.get(key or "")
    return dict(remedy) if remedy else None


def _stale_prompt(text: str, remedy: dict | None) -> str:
    """One gap's Claude prompt: what to re-run, on which gap, and the rule."""
    if not remedy:
        return GENERIC_STALE_PROMPT.format(text=text)
    return (f"/health {remedy['step']} — the Heart's evidence gap: \"{text}\". "
            "Re-run the check only, never change code to clear it; then "
            f"`{TICK_CMD}` and re-read `pyauto-heart readiness`.")


def build_stale_plan(stale_reasons: list, stale_keys: list) -> dict | None:
    """The ONE payload that clears the whole tier.

    A STALE board is normally several gaps at once, and closing them one chip
    at a time is exactly the friction that leaves a board stale for weeks. So
    the gaps are also published as a single ordered plan: ``prompt`` names every
    gap with the check that closes it, and ``command`` is the shell chain that
    does the same — offered ONLY when every gap has a command, because a chain
    that silently skips a gap reads as "that cleared it" when it did not.

    None when nothing is stale, or when no gap has a known remedy.
    """
    if not stale_reasons:
        return None
    keys = list(stale_keys) + [""] * (len(stale_reasons) - len(stale_keys))
    remedies = [stale_remedy(k) for k in keys[:len(stale_reasons)]]
    if not any(remedies):
        return None
    steps, commands = [], []
    for n, (text, remedy) in enumerate(zip(stale_reasons, remedies), 1):
        step = remedy["step"] if remedy else "re-run the check behind it"
        steps.append(f"{n}. {text} → {step}")
        cmd = (remedy or {}).get("command")
        if cmd and cmd not in commands:
            commands.append(cmd)
    prompt = (
        f"/health clear the Heart's {len(stale_reasons)} evidence gap(s) — re-run "
        "the checks named below; never change code to clear one:\n"
        + "\n".join(steps)
        + f"\nThen run `{TICK_CMD} && pyauto-heart readiness` and report the new verdict."
    )
    command = None
    if all(r and r.get("command") for r in remedies):
        chain = commands + [c for c in (TICK_CMD, "pyauto-heart readiness")
                            if c not in commands]
        command = " && ".join(chain)
    return {"count": len(stale_reasons), "command": command, "prompt": prompt}


def _reason_item(text: str, severity: str, repos: dict, key: str = "") -> dict:
    """Structure one flat reason string into an actionable blocker.

    Reasons follow the ``"<repo>: <problem>"`` convention (readiness.py), so
    the prefix resolves the repo; the repo's cached ``ci_status.url`` is the
    failing run when CI is red. The prompt is what 📋 copies — a `/bug` door
    into the Brain for real problems, and for an evidence gap the check that
    actually closes it, looked up by its gate ``key`` (STALE's rule: re-run the
    check, never fix code). A stale row also carries ``command`` — the shell
    remedy — or None where the remedy needs a conversation.
    """
    head = text.split(":", 1)[0].strip()
    body = repos.get(head) if isinstance(repos, dict) else None
    repo = head if isinstance(body, dict) else None
    owner = REPO_OWNERS.get(repo) if repo else None
    repo_url = f"https://github.com/{owner}/{repo}" if owner else None
    run_url = None
    if repo:
        ci = body.get("ci_status") or {}
        if ci.get("url") and str(ci.get("conclusion") or "") not in ("", "success"):
            run_url = str(ci["url"])
    command = None
    if severity == "stale":
        remedy = stale_remedy(key)
        command = (remedy or {}).get("command")
        prompt = _stale_prompt(text, remedy)
    else:
        prompt = f"/bug Heart board: {text}"
        if run_url:
            prompt += f" — failing run: {run_url}"
    return {"text": text, "severity": severity, "repo": repo,
            "repo_url": repo_url, "run_url": run_url, "prompt": prompt,
            "command": command}


def _structure_reasons(red: list, yellow: list, stales: list, repos: dict,
                       stale_keys: list | None = None) -> list[dict]:
    """Every reason as an actionable item. ``stale_keys`` rides index for index
    with ``stales`` (empty when the verdict predates them)."""
    keys = list(stale_keys or [])
    keys += [""] * (len(stales) - len(keys))
    items = [_reason_item(str(t), sev, repos)
             for sev, texts in (("red", red), ("yellow", yellow))
             for t in texts]
    items += [_reason_item(str(t), "stale", repos, k) for t, k in zip(stales, keys)]
    return items


def _devbox_enrich(
    sections: list[Section], devbox: dict | None, now: datetime.datetime | None
) -> list[Section]:
    """Fill unobserved rows from a published dev-box observation.

    A fresh (< ``DEVBOX_FRESH_SECONDS``) family renders with its real state and
    an "observed Nh ago on the dev box" stamp; an expired one stays grey but
    says when the dev box last looked. Rows this render observed itself are
    never overridden — the dev-box data only ever fills gaps.
    """
    if not isinstance(devbox, dict):
        return sections
    dsecs = devbox.get("sections") or {}
    age = _age_seconds(devbox.get("ts"), now)
    if age is None or not isinstance(dsecs, dict):
        return sections
    ago = format_age(age)
    out: list[Section] = []
    for sec in sections:
        d = dsecs.get(sec.key)
        if sec.state != UNOBS or not isinstance(d, dict):
            out.append(sec)
            continue
        if age <= DEVBOX_FRESH_SECONDS and d.get("state") in (OK, WARN, FAIL, INFO):
            out.append(Section(
                sec.key, sec.title, str(d["state"]),
                str(d.get("summary") or ""),
                [str(x) for x in (d.get("details") or [])][:8],
                links=sec.links, action=sec.action,
                observed_ago=f"observed {ago} on the dev box",
            ))
        else:
            out.append(Section(
                sec.key, sec.title, UNOBS,
                f"not observed here — dev box last looked {ago}",
                sec.details, links=sec.links, action=sec.action,
            ))
    return out


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
    # A terminal reading STALE gets the door out of it, not just the diagnosis:
    # `fix stale` prints each gap's command and the one plan that clears them
    # all. Suppressed in quiet mode — that line is a shell prompt, not a board.
    if stales and not quiet:
        lines.append("  " + c_meta("→ clear them: pyauto-heart fix stale"))
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


def _shown_reasons(board: Board) -> tuple[str, list[dict]]:
    """The one reason tier a surface displays, structured, worst first."""
    if board.red_reasons:
        return "Blockers", [b for b in board.blockers if b["severity"] == "red"]
    if board.yellow_reasons:
        return "Warnings", [b for b in board.blockers if b["severity"] == "yellow"]
    if board.stale_reasons:
        return ("Evidence gaps (re-run, don't fix)",
                [b for b in board.blockers if b["severity"] == "stale"])
    return "", []


def _md_reason(item: dict) -> str:
    """One reason as markdown: repo linked, failing run linked."""
    text = _md_escape(item["text"])
    if item.get("repo") and item.get("repo_url"):
        rest = _md_escape(item["text"][len(item["repo"]):])
        text = f"[{item['repo']}]({item['repo_url']}){rest}"
    if item.get("run_url"):
        text += f" ([run]({item['run_url']}))"
    return text


def _md_prompts_block(items: list[dict], plan: dict | None = None) -> list[str]:
    """A collapsed block of copyable fix prompts (GitHub's fenced-code copy
    button makes each one one-tap on the web view).

    When the gaps carry a whole-tier ``plan``, it leads: one prompt (and, where
    every gap has one, one command chain) that closes all of them.
    """
    if not items:
        return []
    lines = ["<details>",
             "<summary>📋 fix prompts — copy one into a Claude Code chat</summary>", ""]
    if plan and items[0].get("severity") == "stale":
        lines += ["**Clear every gap in one go:**", "", "```", plan["prompt"], "```", ""]
        if plan.get("command"):
            lines += ["```", plan["command"], "```", ""]
    for it in items[:6]:
        if it.get("command"):
            lines += ["```", it["command"], "```", ""]
        lines += ["```", it["prompt"], "```", ""]
    lines += ["</details>", ""]
    return lines


def _render_md(board: Board) -> str:
    word = _VERDICT_WORD.get(board.verdict, "GREEN")
    emoji = _STATE_MD[_VERDICT_STATE.get(board.verdict, OK)]
    age = format_age(board.age_seconds, stale=board.stale)
    lines = [
        f"## {emoji} PyAutoHeart Dashboard — **{word}** (score {board.score})",
        "",
        f"_snapshot `{board.ts}` · {age}_"
        + ("  ⚠️ **stale — run `pyauto-heart tick`**" if board.stale else ""),
        "",
    ]
    label, items = _shown_reasons(board)
    if items:
        lines.append(f"**{label}:** " + "; ".join(_md_reason(i) for i in items[:6]))
        lines.append("")
        lines += _md_prompts_block(items, board.stale_plan)
    lines += ["| | Check | Status |", "|--|--|--|"]
    for sec in board.sections:
        em = _STATE_MD[sec.state]
        status = _md_escape(sec.summary)
        if sec.observed_ago:
            status += f" · _{_md_escape(sec.observed_ago)}_"
        lines.append(f"| {em} | {sec.title} | {status} |")
    lines.append("")
    lines.append(f"[Dashboard]({PAGES_URL})")
    return "\n".join(lines)


def _render_md_brief(board: Board) -> str:
    """The README strip: one glance, not a wall. It renders under the README's
    own "## Current health" heading, so it carries no heading of its own — a
    single verdict line with the board link inline; blockers/warnings appear
    only when there ARE any (stale evidence gaps and timestamps live on the
    board, not the README — the strip earns extra lines only when something
    needs a human)."""
    word = _VERDICT_WORD.get(board.verdict, "GREEN")
    emoji = _STATE_MD[_VERDICT_STATE.get(board.verdict, OK)]
    lines = [
        f"{emoji} **{word}** · score {board.score} · [dashboard →]({PAGES_URL})"
    ]
    if board.verdict in ("red", "yellow"):
        label, items = _shown_reasons(board)
        if items:
            lines += ["", f"**{label}:** "
                      + "; ".join(_md_reason(i) for i in items[:4])]
    return "\n".join(lines)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def _copy_btn(payload: str, label: str = "copy", face: str = "📋") -> str:
    """A one-tap clipboard button (the PyAutoMind dashboard pattern): tap it
    and the payload — a Claude prompt or a command — is ready to paste.

    ``face`` is what the button shows: the bare 📋 for a chip beside a row, a
    short worded face (⌨ command chain) where the board offers more than one
    payload and the reader has to choose between them.

    A worded face takes the theme's `text` modifier. The base `button.copy` is
    a fixed 2.6rem SQUARE — right for a bare glyph, and a trap for words: the
    label wrapped inside 42px into a one-word-per-line column and spilled out
    of its own box. Whitespace in the face is the test, because that is what
    makes a face a phrase rather than a glyph.
    """
    cls = "copy text" if len(face.split()) > 1 else "copy"
    return (f"<button class='{cls}' type='button' "
            f"title='{_html.escape(label, quote=True)}' "
            f"data-cmd=\"{_html.escape(payload, quote=True)}\">{_html.escape(face)}</button>")


def _html_reason(item: dict) -> str:
    """One blocker/warning as html: repo linked, run linked, prompt one tap away."""
    text = _html.escape(item["text"])
    if item.get("repo") and item.get("repo_url"):
        rest = _html.escape(item["text"][len(item["repo"]):])
        text = (f"<a href=\"{_html.escape(item['repo_url'], quote=True)}\">"
                f"{_html.escape(item['repo'])}</a>{rest}")
    if item.get("run_url"):
        text += (f" <a class='out' href=\"{_html.escape(item['run_url'], quote=True)}\">"
                 f"run ↗</a>")
    if item.get("command"):
        text += " " + _copy_btn(item["command"],
                                "copy the command that re-runs this check",
                                "⌨")
    if item.get("prompt"):
        text += " " + _copy_btn(item["prompt"], "copy the fix prompt for a Claude Code chat")
    return f"<li>{text}</li>"


def _html_stale_plan(board: Board, items: list[dict]) -> str:
    """The one-tap "clear them all" line above the evidence gaps.

    Rendered only when the gaps are the tier on show — the board displays one
    tier at a time, and a plan for reasons the reader cannot see is noise.
    """
    plan = board.stale_plan
    if not plan or not items or items[0].get("severity") != "stale":
        return ""
    chips = _copy_btn(plan["prompt"],
                      "copy one prompt that clears every gap, for a Claude Code chat",
                      "📋 clear them all")
    if plan.get("command"):
        chips += " " + _copy_btn(plan["command"],
                                 "copy the command chain that re-runs every check",
                                 "⌨ command chain")
    return f"<p class='plan'>{chips}</p>"


# The Heart's verdict in the theme's tone vocabulary. The board's own
# `_VERDICT_STATE` stays the internal truth; this is only how it is painted.
_VERDICT_TONE = {"red": "bad", "yellow": "warn", "stale": "warn",
                 "green": "ok"}

_LEDE = ("Is it safe to release? Every check the Heart observes, with the "
         "evidence behind each verdict. \u2328 copies the command that re-runs a "
         "check; \U0001f4cb copies a ready-to-paste prompt for a Claude Code chat.")

# The page-specific shapes the shared sheet has no opinion on: the per-row
# state dot, the evidence list, the stale banner. Written against the theme's
# variables, so this board follows the family accent rather than setting a
# second palette.
_EXTRA_CSS = """
table.board td.dot{width:1.15rem;padding-right:.35rem}
table.board td.dot::before{content:"";display:inline-block;width:10px;
 height:10px;border-radius:50%;margin-top:.35rem;background:var(--muted)}
table.board tr.ok td.dot::before{background:var(--ok)}
table.board tr.warn td.dot::before{background:var(--warn)}
table.board tr.fail td.dot::before{background:var(--bad)}
table.board tr.info td.dot::before{background:var(--accent)}
table.board td.name{font-weight:600;white-space:nowrap}
table.board tr.unobs td.name,table.board tr.unobs td.sum{color:var(--muted)}
/* The detail lines are column-ALIGNED text — `<repo>   CI ✓   PR×1`,
   space-padded so the columns line up exactly as they do in the terminal
   render. Html collapses those runs of spaces, so every one of them arrived
   as run-on prose in a proportional face and the whole block read as one
   grey paragraph column. `pre-wrap` keeps the padding and a monospace face
   makes it mean something again; a line too long for the column still wraps
   rather than scrolling the page (the shared sheet's wrap guard). */
ul.det{margin:.35rem 0 0;padding-left:1.1rem;color:var(--muted);
 font-size:.8rem;white-space:pre-wrap;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.ago{color:var(--muted)}
/* The out-links carry DATA in their labels — `<repo> run`, and this org's
   longest repo name is 36 characters. `nowrap` made one of those a single
   unbreakable 500px word, which set the summary column's min-content width
   and pushed the whole page sideways on a phone (measured: a 375px viewport
   scrolling to 521px). Nothing is lost by letting them wrap: a short label
   like `run ↗` has no wrap opportunity to take, and a long one should break
   rather than break the page. The shared theme's `overflow-wrap` does the
   rest. */
a.out{font-size:.85rem}
.stale{background:var(--btn);border:1px solid var(--warn);color:var(--warn);
 padding:.55rem .75rem;border-radius:8px}
.reasons{margin:1.5rem 0}
/* The whole-tier fix line sits between the heading and the gaps it closes, so
   the reader meets it before the per-row ones. */
.plan{margin:.4rem 0 .8rem}
.plan .copy{margin-right:.4rem}
.reasons li{margin:.3rem 0}
.hint{color:var(--muted);font-size:.85em;margin:.5rem 0 0}
footer{margin-top:2rem;color:var(--muted);font-size:.82em}
/* Narrow screens: the shared theme turns a table row into a stacked card
   (`table.recent` under its own breakpoint); these three lines say how THIS
   board's cells fall into it. Measured at a 390px viewport before: the name
   column held 127px for one short line while the summary was squeezed into
   213px and ran 472px down the screen — the text bunched to the right with
   the name column left as empty height beside it. Now the dot and the name
   read as one header line and the summary takes the full width beneath. */
@media(max-width:34rem){
 table.board td.dot{width:auto;padding-right:.4rem}
 table.board td.dot::before{margin-top:0}
 table.board td.name{white-space:normal}
 table.board td.sum{flex:1 0 100%;margin-top:.15rem}
 table.board ul.det{padding-left:.9rem}
}
"""


def _render_html(board: Board) -> str:
    word = _VERDICT_WORD.get(board.verdict, "GREEN")
    vstate = _VERDICT_STATE.get(board.verdict, OK)
    age = format_age(board.age_seconds, stale=board.stale)
    rows = []
    for sec in board.sections:
        cls = _STATE_HTML[sec.state]
        summary = _html.escape(sec.summary)
        if sec.observed_ago:
            summary += f" <span class='ago'>· {_html.escape(sec.observed_ago)}</span>"
        for link in sec.links:
            summary += (f" <a class='out' href=\"{_html.escape(str(link.get('url', '')), quote=True)}\">"
                        f"{_html.escape(str(link.get('label', 'link')))} ↗</a>")
            if link.get("prompt"):
                summary += " " + _copy_btn(str(link["prompt"]),
                                           "copy the fix prompt for a Claude Code chat")
        if sec.action and sec.action.get("payload"):
            summary += " " + _copy_btn(str(sec.action["payload"]),
                                       str(sec.action.get("label", "copy")))
        details = ""
        if sec.details:
            items = "".join(f"<li>{_html.escape(d)}</li>" for d in sec.details)
            details = f"<ul class='det'>{items}</ul>"
        rows.append(
            f"<tr class='{cls}'><td class='dot'></td>"
            f"<td class='name'>{_html.escape(sec.title)}</td>"
            f"<td class='sum'>{summary}{details}</td></tr>"
        )
    reasons_html = ""
    label, items = _shown_reasons(board)
    if items:
        lis = "".join(_html_reason(i) for i in items[:8])
        hint = ("<p class='hint'>⌨ copies the command that re-runs a check; "
                "📋 copies a ready-to-paste prompt for a Claude Code chat.</p>")
        reasons_html = (f"<div class='reasons'><h2>{label}</h2>"
                        f"{_html_stale_plan(board, items)}<ul>{lis}</ul>{hint}</div>")
    stale_html = (
        "<p class='stale'>⚠️ This board is stale — the last tick is older than the "
        "freshness threshold; the numbers may not be current.</p>" if board.stale else ""
    )
    t_ = theme()
    hero = t_.hero(BOARD_KEY, "Dashboard", _LEDE)
    # The way back from the Pages board to the repository front door; owner
    # from the declared config surface (REPO_OWNERS), so the segment drops
    # out on a tenant whose config does not list this repo.
    repo_name = PAGES_URL.rstrip("/").rsplit("/", 1)[-1]
    gh_owner = REPO_OWNERS.get(repo_name)
    github_link = (f' · <a href="https://github.com/{gh_owner}/{repo_name}'
                   '/blob/main/README.md">GitHub Page</a>' if gh_owner else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PyAutoHeart Dashboard — {word}</title>
<style>{t_.css(BOARD_KEY)}{_EXTRA_CSS}</style>
</head>
<body>
{hero}
<p class="verdict {_VERDICT_TONE.get(board.verdict, '')}"><b>{word} · score
 {board.score}</b><span class="muted">snapshot {_html.escape(board.ts)} ·
 {age} · <a href="dashboard.md">markdown version</a>{github_link}</span></p>
{stale_html}
{reasons_html}
<table class="recent board">{''.join(rows)}</table>
{_boards_nav_html()}
<footer>Rendered by <code>heart/dashboard.py</code> — one renderer, many
surfaces. Observer only: PyAutoHeart never writes outside its own repo/state.
\U0001f4cb and \u2328 buttons copy a Claude prompt or a command to your clipboard.</footer>
<script>{t_.JS}</script>
</body></html>
"""


def to_dict(board: Board) -> dict[str, Any]:
    """The machine surface (fmt='json') the Health Agent + mobile consume."""
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "verdict": board.verdict,
        "score": board.score,
        "ts": board.ts,
        "age_seconds": board.age_seconds,
        "stale": board.stale,
        "red_reasons": board.red_reasons,
        "yellow_reasons": board.yellow_reasons,
        # Evidence gaps belong on this surface too: the Health Agent and mobile
        # read it, and a reason that moves from the red axis to the stale one
        # would otherwise vanish from both rather than being re-classified.
        "stale_reasons": board.stale_reasons,
        # Structured, actionable reasons — what the 📋 buttons copy and where
        # they link. The flat lists above stay for v1 consumers.
        "blockers": board.blockers,
        # One payload that closes every current evidence gap (v3). None when
        # nothing is stale — a sibling board renders it, never re-derives it.
        "stale_plan": board.stale_plan,
        "pages_url": PAGES_URL,
        "sections": [
            {
                "key": s.key,
                "title": s.title,
                "state": s.state,
                "summary": s.summary,
                "details": s.details,
                "links": s.links,
                "action": s.action,
                "observed_ago": s.observed_ago,
            }
            for s in board.sections
        ],
    }
    # The ⏱ performance block, additive (schema v2 stays v2). Emitted only when
    # something was actually measured — a consumer must be able to tell "no
    # timing observed" from "timing observed, nothing to report", and this is
    # also the block the NEXT render reads back as its own history.
    if board.performance is not None:
        payload["performance"] = board.performance
    return payload


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
    devbox: dict | None = None,
) -> str:
    """Render the unified board in ``fmt``. Pure: snapshot in → string out."""
    board = build_board(
        snapshot, verdict, validation,
        unobserved=unobserved, now=now, stale_after=stale_after, devbox=devbox,
    )
    if fmt == "term":
        return _render_term(board, verdict or {}, quiet=quiet)
    if fmt == "oneline":
        return _render_oneline(board)
    if fmt == "md":
        return _render_md(board)
    if fmt == "md-brief":
        return _render_md_brief(board)
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
    g.add_argument("--md-brief", action="store_true",
                   help="the README strip: verdict + linked blockers + board link")
    g.add_argument("--html", action="store_true", help="standalone self-contained HTML page")
    g.add_argument("--json", action="store_true", help="the machine surface (Health Agent / mobile)")
    g.add_argument("--badge", action="store_true", help="emit a shields.io endpoint-badge JSON")
    ap.add_argument("--cloud", action="store_true",
                    help="mark local-only checks as 'not observed here' (cloud job vantage)")
    ap.add_argument("--devbox", metavar="PATH", default=None,
                    help="published dev-box board JSON to fill unobserved rows "
                         "(default under --cloud: state/devbox_board.json if present)")
    ap.add_argument("--quiet", action="store_true", help="suppress drill-down details (term)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    ap.add_argument("--stale-after", type=int, default=STALE_AFTER_SECONDS,
                    help=f"seconds before the board is flagged stale (default {STALE_AFTER_SECONDS})")
    ns = ap.parse_args(argv)
    if ns.no_color:
        os.environ["NO_COLOR"] = "1"

    fmt = "term"
    for name, label in (("oneline", "oneline"), ("md", "md"),
                        ("md_brief", "md-brief"), ("html", "html"), ("json", "json")):
        if getattr(ns, name):
            fmt = label
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

    # The published dev-box observation (heart/publish.py) fills unobserved
    # rows; on the cloud job it is auto-detected in the checkout.
    import pathlib
    devbox = None
    devbox_path = ns.devbox
    if devbox_path is None and ns.cloud:
        default = pathlib.Path(__file__).resolve().parents[1] / "state" / "devbox_board.json"
        if default.exists():
            devbox_path = str(default)
    if devbox_path:
        try:
            devbox = json.loads(pathlib.Path(devbox_path).read_text())
        except (OSError, ValueError) as e:
            print(f"warning: could not read devbox board {devbox_path}: {e}",
                  file=sys.stderr)

    if ns.badge:
        board = build_board(snapshot, verdict, validation,
                            unobserved=unobserved, stale_after=ns.stale_after)
        print(json.dumps(badge_endpoint(board)))
        return 0

    print(render(snapshot, verdict, validation, fmt=fmt, unobserved=unobserved,
                 quiet=ns.quiet, stale_after=ns.stale_after, devbox=devbox))
    return 0


if __name__ == "__main__":
    sys.exit(main())
