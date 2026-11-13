"""heart/readiness.py — composite release-readiness verdict.

Rolls the continuous-health signals already in ``state.json`` into a single
green / yellow / red verdict answering "is it safe to release?". This is the
**authoritative** release gate: PyAutoHands is a pure executor and no longer
runs its own readiness checks (``verify_workspace_versions.sh`` was removed) —
the orchestrator (PyAutoAgent's release agent) consults this verdict via
``pyauto-heart readiness --json`` and only dispatches Build's ``release.yml``
when it is green.

The verdict uses STRICT release gates:

- **RED** (a real blocker) if any of the 5 libraries has failing CI, is off
  ``main``, has uncommitted source changes, or is behind origin; or any workspace
  records a compatibility floor (``version.minimum_library_version``) that
  exceeds the newest released version of its library (UNSATISFIABLE — no
  installable release can satisfy it), or an unparseable (BAD) floor/tag; or the
  deep install verification last reported ``ready == false``; or the
  release-validation report last ingested reports ``validation_outcome ==
  "fail"`` (a stage failed, or failing/timed-out counts were recorded). A report
  predating that field falls back to ``release_ready == false`` → RED, so the
  gate fails closed on evidence it cannot classify. Also RED: the release push
  credential (``heart/checks/token_push.py``, the ``pyautolabs-bot`` App's
  ``git push --dry-run``) was refused by any library; a missing, partial or
  older-than-48 h proof is STALE. Also RED: an expired or refused Stage 3
  quarantine entry (``heart/quarantine.py``, PyAutoHeart#344); an ACTIVE one
  is YELLOW with the list — a quarantined script is skipped, never green.
- **YELLOW** (caution) for soft signals: workspace-validation not passing (the
  workspace scripts/notebooks carry standing debt, so this is advisory — never a
  hard block), script-timing regressions, stale open PRs, stale parked scripts, a
  stale or never-run install verification, **no fresh
  release-validation report for the current source** (absent, stale by age, or
  whose ``commit_shas`` are no longer ancestors of the current ``main`` HEADs —
  main moving FORWARD from the rehearsed commits keeps it fresh, since a
  release publishes exactly those commits (M2.4, ``validation_ahead``) — or
  which ran under a profile other than ``release``), and — crucially — any *unknown*
  (missing test-run report, a library absent from the snapshot). An unknown is
  never silently treated as green and never escalated to red.
- **STALE** (an evidence gap, the freshness tier) when nothing is known-bad but
  some evidence is *missing or expired*: a check that was never run, a
  passing-but-aged report, a rehearsal whose ``commit_shas`` are not (or not
  provably) ancestors of ``main``, a validation report whose ``validation_outcome`` is ``incomplete``
  (nothing failed; the rehearsal evidence is absent), an unknown repo/version
  status. The remedy for a stale reason is to
  **re-run the check**, never to fix code — which is exactly what separates it
  from yellow. The tier is not a skip lever: evidence whose *last known result
  was adverse* stays yellow/red until a fresh run says otherwise; only
  unknown or passed-but-expired evidence lands here.
- **GREEN** otherwise — which now REQUIRES a fresh, passing release-validation
  report matching the current source under the ``release`` profile, not just an
  absence of red signals.

Consumer semantics: the **release gate needs GREEN** — STALE blocks a release
exactly like YELLOW, but prescribes refreshing evidence rather than a human
acknowledgement.

**Profiles** (``--profile``): ``default`` is the dev-box verdict described
above, byte-for-byte unchanged. ``release-ci`` is the scheduled-nightly
release gate (``PyAutoHands/docs/nightly_release_design.md`` §5): the same
gates evaluated over a snapshot the nightly driver assembles **in CI**, where
dev-box-local evidence (``repo_state``, ``version_skew``, ``script_timing``,
the local ``test_run`` cache, ``manifest_drift``, ``profiling_drift``) does
not exist. Under ``release-ci`` a *gap* in that local-only evidence is
reported in ``na_reasons`` ("out of scope", listed, never silently green)
instead of blocking as stale — while any local evidence actually *present*
still counts in full, and gaps in the **required** evidence set (library
``ci_status``, the release-validation report, ``verify_install``) block
exactly as they always did. The workspace script surface is not weakened by
scoping out the local ``test_run`` cache: the release-validation report *is*
that surface at release fidelity, and required-workflow conclusions gate the
workspaces' ``main``. Keeping evidence gaps in their own tier is what makes
GREEN reachable in steady state and keeps YELLOW meaning "something is
actually wrong" instead of training ack-fatigue.

**The development-ship gate** (``--dev --repos A,B``; ``dev_ship``; human
ruling 2026-11-12, PyAutoHeart#340) answers a different question from the
release gate — "may this branch ship?", not "may ``main`` be published?":

- *Scope.* A development ship is blocked only by CI on ``main`` of the repos
  the branch touches and of their dependants (the libraries downstream of
  them in ``config/repos.yaml``'s ``dependencies`` graph). Every other reason
  — release validation, install verification, version floors, organ
  workflows, the dev box's checkout state — is still shown, in
  ``shown_reasons``, but does not block. In-scope ``main`` CI is read for a
  repo of ANY group (an organ, a developer workspace), not only the
  release-gated ones; a run with no conclusion yet is STALE. A touched repo
  Heart does not poll is STALE ("not polled by Heart"); a name the policy file
  never mentions is a usage error.
- *Not Heart-gated* (human ruling 2026-11-13, PyAutoHeart#343). A touched
  repo on ``config/repos.yaml``'s ``not_heart_gated`` list (unpolled repos
  whose PR checks are the gate: profiling, inference, assistants,
  visualization, Scientist, Broca, Cortex) is *gate-deferred*: not STALE, not
  blocking, listed in ``gate_deferred`` and shown as "not Heart-gated: PR
  checks decide". The merge paths enforce the rest — at least one completed
  check on the PR head, all green. When every touched repo is deferred, Heart
  holds no evidence the verdict uses, so the snapshot's age is not judged.
- *Feed age.* Evidence older than ``DEV_MAX_FEED_AGE_HOURS`` (6 h, also the
  ceiling a caller may set), of unknown age, or dated more than 5 min in the
  future, is STALE: re-check, or park. Each in-scope CI sidecar is aged by its
  own collection ``ts``, and the snapshot by its aggregation ``ts``. **STALE
  never passes** this gate; only GREEN does (``pass``). This replaces the
  earlier rule under which the dev-ship gate treated an organism-wide STALE as
  a pass.
- *Source.* From the CLI it judges the dev box's local
  ``$HEART_STATE_DIR/state.json`` (the tick's snapshot), and says so with that
  snapshot's age. The event triggers in ``heart-health.yml`` refresh the
  published cloud feed, which this command does not read.
- *Release gating is unchanged:* ``compute`` and the plain ``readiness`` call
  read exactly as before.

Red dominates yellow, which dominates stale, structurally: reasons are
collected into separate lists and
``verdict = red if red_reasons else yellow if yellow_reasons else stale if stale_reasons else green``.
A ``score`` (0–100, weighted penalties) is advisory/sortable only — the colour,
not the number, is the gate. Each stale reason also travels with the gate key
that produced it in ``stale_details`` (additive — the flat reason lists are
unchanged), so a surface can look up the check that closes a gap instead of
parsing the sentence. ``compute`` is a pure function of the snapshot for
easy testing and never raises on partial/malformed data.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from heart import quarantine as _quarantine
from heart import state, validate
from heart.checks.ci_status import FAILURE_CONCLUSIONS, load_required_workflows
from heart.checks.test_run import counts_measured as tr_counts_measured
from heart.checks.token_push import STALE_HOURS as TOKEN_PUSH_STALE_HOURS
from heart.heart_color import (
    c_bold, c_fail, c_info, c_meta, c_ok, c_warn,
    glyph_fail, glyph_ok, glyph_warn,
)

HEART_HOME = Path(__file__).resolve().parents[1]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"
RELEASE_READY_FILE = state.HEART_STATE_DIR / "release_ready.json"


# Workspace repo groups whose required-workflow conclusions on `main` HEAD are a
# hard release gate (RED on failure). Libraries are gated separately (the lib
# loop); url-checking is advisory and intentionally absent from the required set
# (see config/repos.yaml `required_workflows`). A red `smoke_tests` on a
# workspace's main is a real release blocker — a green `url_check` cannot mask
# it because url is not a required workflow.
GATED_WORKSPACE_GROUPS = frozenset({"workspaces", "workspaces_test", "howto", "pipelines"})

# gate key -> (penalty per occurrence, cap). Score = 100 - sum(min(n*w, cap)).
# Advisory only; colour is decided by reason presence, not by score.
_WEIGHTS: dict[str, tuple[int, int]] = {
    "lib_ci": (30, 60),
    "lib_branch": (15, 30),
    "lib_dirty": (15, 30),
    "lib_behind": (20, 40),
    "test_failing": (15, 15),
    "skew_unsatisfiable": (25, 50),
    "lib_unknown": (10, 30),
    "test_unknown": (10, 10),
    "timing_red": (15, 15),
    "timing_yellow": (8, 8),
    "profiling_drift": (10, 30),
    "open_pr": (5, 15),
    "parked": (5, 15),
    "skew_bad": (25, 50),
    "skew_unknown": (10, 30),
    "skew_pypi_unsatisfiable": (25, 50),
    "skew_pypi_bad": (25, 50),
    "skew_pypi_floor_yanked": (8, 24),
    "skew_pypi_unknown": (10, 30),
    "required_workflow_drift": (20, 40),
    "required_workflow_unknown": (10, 30),
    "install_not_ready": (40, 40),
    "install_non_release": (10, 10),
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
    "token_push_refused": (40, 40),
    "token_push_unknown": (10, 10),
    "token_push_stale": (10, 10),
    "quarantine_active": (5, 15),
    "quarantine_expired": (40, 40),
    "quarantine_refused": (40, 40),
}


def load_library_names(config_path: Path | str = CONFIG_PATH) -> list[str]:
    """Return every polled library — repos.libraries[].name from the policy
    file. Strict: the file is in-repo and the group is load-bearing — a missing
    or empty group is a config bug that must fail loudly.

    This is the full *polled* set (what the tick monitors). The *release gate*
    is a subset — see ``load_release_gate_names``."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    libs = [r["name"] for r in cfg["repos"]["libraries"]]
    if not libs:
        raise ValueError(f"empty repos.libraries group in {config_path}")
    return libs


def load_release_gate_names(config_path: Path | str = CONFIG_PATH) -> list[str]:
    """The release-gating libraries: polled libraries minus those a library
    marks ``release_gate: false``. A library can be polled for health/CI
    (monitoring) without being part of the release-validation gate — e.g. a
    library mid-resurrection that is not yet released and so has no release
    evidence to confirm. Absent key ⇒ gating (the default); only an explicit
    ``false`` opts out. Strict: an empty result is a config bug."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    libs = [
        r["name"] for r in cfg["repos"]["libraries"]
        if r.get("release_gate", True)
    ]
    if not libs:
        raise ValueError(f"no release-gating libraries in {config_path}")
    return libs


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
# recorded commit_shas for the release-validation gate to go GREEN. This is the
# release-gating subset (a resurrecting, not-yet-released library is polled but
# excluded — see load_release_gate_names).
_GATE_SHA_LIBS = tuple(load_release_gate_names())


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
    profile: str = "default",
    quarantine: list[Any] | None = None,
) -> dict[str, Any]:
    """Pure verdict function. Never raises on missing/partial data.

    ``quarantine`` is the Stage 3 quarantine's raw entries (default: the
    repo-tracked ``config/stage3_quarantine.yaml``, PyAutoHeart#344)."""
    snapshot = snapshot or {}
    libs = list(libraries) if libraries is not None else load_release_gate_names()
    req_wf = required_workflows if required_workflows is not None else load_required_workflows()
    repos = snapshot.get("repos", {}) or {}
    ref = _parse_ts(snapshot.get("ts")) or datetime.datetime.now(datetime.timezone.utc)
    release_ci = profile == "release-ci"

    red: list[str] = []
    yellow: list[str] = []
    # The freshness tier: nothing known-bad, but evidence missing/expired —
    # remedy is re-running a check. Never receives a reason whose last known
    # result was adverse (those stay red/yellow); see the module docstring.
    stale: list[str] = []
    # The gate key behind each entry of ``stale``, index for index — the
    # machine-readable identity of a gap, so a consumer looks its remedy up
    # instead of guessing it from the sentence.
    stale_keys: list[str] = []
    # Out-of-scope-by-profile: dev-box-local evidence gaps under release-ci.
    # Listed for transparency (never silently green) but non-gating — the
    # module docstring's "Profiles" section is the rule's home.
    na: list[str] = []
    counts: dict[str, int] = {}
    repository_reasons: list[dict[str, str]] = []
    # Repos whose main moved FORWARD from the rehearsed commit -> commits ahead
    # (None when GitHub did not say). Informational: the rehearsal still covers
    # exactly what a release would publish (M2.4).
    validation_ahead: dict[str, Any] = {}

    def hit(key: str, n: int = 1) -> None:
        counts[key] = counts.get(key, 0) + n

    def add_stale(msg: str, key: str) -> None:
        """File an evidence gap: its sentence, its gate key, its penalty."""
        stale.append(msg)
        stale_keys.append(key)
        hit(key)

    def repo_red(repo: str, key: str, text: str, workflow: str = "") -> None:
        red.append(text)
        hit(key)
        repository_reasons.append({"repo": repo, "key": key, "text": text,
                                   "severity": "red", "workflow": workflow})

    def scope_local(msg: str, key: str) -> None:
        """File a *gap* in dev-box-local evidence: stale on the default
        profile; out-of-scope (na, non-gating) under release-ci. Adverse
        local evidence never routes through here — it stays red/yellow."""
        if release_ci:
            na.append(msg + " (dev-box-local — out of scope for release-ci)")
        else:
            add_stale(msg, key)

    # --- library gates (RED) ---
    for lib in libs:
        body = repos.get(lib)
        if not isinstance(body, dict) or not body:
            add_stale(f"{lib}: status unknown", "lib_unknown")
            repository_reasons.append({"repo": lib, "key": "lib_unknown",
                                       "text": stale[-1], "severity": "stale"})
            continue
        ci = body.get("ci_status", {}) or {}
        conclusion = ci.get("conclusion")
        if ci.get("error"):
            # The CI query failed, so we have no evidence either way. That is a
            # STALE "unknown", never a pass — and it must be *said*, because a
            # silent unknown here is indistinguishable from a healthy repo.
            add_stale(f"{lib}: CI status unavailable ({ci['error']})",
                      "lib_ci_unavailable")
            repository_reasons.append({"repo": lib, "key": "lib_ci_unavailable",
                                       "text": stale[-1], "severity": "stale"})
        elif conclusion not in (None, "", "success"):
            repo_red(lib, "lib_ci", f"{lib}: CI {conclusion}")
        rs = body.get("repo_state", {}) or {}
        branch = rs.get("branch")
        if branch and branch != "main":
            repo_red(lib, "lib_branch", f"{lib}: on branch {branch} (not main)")
        dirty_real = _as_int(rs.get("dirty_real", rs.get("dirty_files", 0)))
        if dirty_real > 0:
            repo_red(lib, "lib_dirty", f"{lib}: {dirty_real} uncommitted source change(s)")
        behind = _as_int(rs.get("behind", 0))
        if behind > 0:
            repo_red(lib, "lib_behind", f"{lib}: {behind} commit(s) behind origin")

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
                    repo_red(name, "ws_ci", f"{name}: {wf} {concl} on main", wf)
        else:
            # Pre-structured sidecar: fall back to the rolled-up conclusion.
            concl = ci.get("conclusion")
            if concl in FAILURE_CONCLUSIONS:
                repo_red(name, "ws_ci", f"{name}: CI {concl} on main")

    # --- test-run gate (RED if false, YELLOW if unknown) ---
    test_run = snapshot.get("test_run")
    if isinstance(test_run, dict) and "ready" in test_run:
        ready = test_run.get("ready")
        if ready is False:
            # Workspace scripts/notebooks carry standing debt; failing validation
            # is advisory (YELLOW), not a release blocker. Real blockers are the
            # library CI / install / version-skew gates above. The reason may
            # only state counts somebody measured (a local report or the run's
            # ingested artifact) — a conclusion-only verdict says so instead of
            # asserting "0 failed".
            label = test_run.get("run_label", "?")
            if tr_counts_measured(test_run):
                failed = _as_int(test_run.get("failed", 0))
                timeout = _as_int(test_run.get("timeout", 0))
                msg = f"workspace validation not passing ({failed} failed"
                if timeout:
                    msg += f", {timeout} timeout"
                msg += f", {label}"
                scripts = [
                    s for s in (test_run.get("failing_scripts") or [])
                    if isinstance(s, dict)
                ]
                if scripts:
                    msg += ": " + ", ".join(
                        f"{s.get('project')} {s.get('script')}" for s in scripts[:3]
                    )
                    if len(scripts) > 3:
                        msg += f", +{len(scripts) - 3} more"
                yellow.append(msg + ")")
            else:
                yellow.append(
                    f"workspace validation failing ({label}; counts not ingested — see run)"
                )
            hit("test_failing")
        elif ready is True:
            # Passing-but-expired evidence is a freshness gap, not a warning —
            # the last known result was good.
            age = _age_days(test_run.get("ts"), ref)
            if age is not None and age > TEST_STALE_DAYS:
                scope_local(f"test run stale ({int(age)}d old)", "test_stale")
        else:
            scope_local("test run status unknown", "test_unknown")
        # parked staleness (YELLOW)
        parked = _as_int(test_run.get("parked_stale_count", 0))
        if parked > 0:
            yellow.append(f"{parked} stale parked script(s)")
            hit("parked")
    else:
        scope_local("test run status unknown (no report.json)", "test_unknown")

    # --- version skew (RED floor unsatisfiable / STALE unknown) ---
    skew = snapshot.get("version_skew")
    if isinstance(skew, dict):
        for w in skew.get("workspaces") or []:
            if not isinstance(w, dict):
                continue
            status = str(w.get("status", "")).upper()
            if status == "UNSATISFIABLE":
                red.append(
                    f"{w.get('workspace')}: floor {w.get('floor')} exceeds newest "
                    f"{w.get('library')} release {w.get('newest_release')} "
                    f"(no installable version satisfies it)"
                )
                hit("skew_unsatisfiable")
            elif status == "BAD":
                red.append(
                    f"{w.get('workspace')}: unparseable version "
                    f"(floor {w.get('floor')} / newest {w.get('newest_release')})"
                )
                hit("skew_bad")
            elif status == "UNKNOWN":
                add_stale(
                    f"{w.get('workspace')}: newest {w.get('library')} release unknown",
                    "skew_unknown",
                )

    # --- version skew, PyPI yank leg (deep `version_skew --pypi`; the slice is
    # absent until that on-demand probe has run — absence is no signal) ---
    skew_pypi = snapshot.get("version_skew_pypi")
    if isinstance(skew_pypi, dict):
        for w in skew_pypi.get("workspaces") or []:
            if not isinstance(w, dict):
                continue
            status = str(w.get("status", "")).upper()
            if status == "UNSATISFIABLE":
                red.append(
                    f"{w.get('workspace')}: no installable {w.get('package')} release "
                    f"on PyPI satisfies floor {w.get('floor')} (all candidates yanked)"
                )
                hit("skew_pypi_unsatisfiable")
            elif status == "BAD":
                red.append(
                    f"{w.get('workspace')}: unparseable floor {w.get('floor')} (PyPI leg)"
                )
                hit("skew_pypi_bad")
            elif status == "FLOOR_YANKED":
                yellow.append(
                    f"{w.get('workspace')}: floor {w.get('floor')} names a yanked/"
                    f"unavailable {w.get('package')} release — bump the floor"
                )
                hit("skew_pypi_floor_yanked")
            elif status == "UNKNOWN":
                add_stale(
                    f"{w.get('workspace')}: PyPI unreachable for {w.get('package')}",
                    "skew_pypi_unknown",
                )

    # --- manifest drift (YELLOW — identity hygiene vs PyAutoMind/repos.yaml) ---
    manifest = snapshot.get("manifest_drift")
    if isinstance(manifest, dict) and manifest.get("available"):
        for label, chk in (manifest.get("checks") or {}).items():
            if isinstance(chk, dict) and not chk.get("ok", True):
                n = len(chk.get("problems") or [])
                yellow.append(
                    f"manifest drift: {label} — {n} mismatch(es) vs PyAutoMind/repos.yaml"
                )
                hit("manifest_drift")

    # --- required-workflow drift (YELLOW — a gate that is not wired up) ---
    #
    # A required workflow with no file never satisfies ci_status.rollup()'s
    # all_green, so the repo reads `in_progress` forever — indistinguishable
    # from a run in flight, which is why this state survives unnoticed. It is a
    # *configuration* finding, not red CI: the repo's code is fine, its gate is
    # missing, and colouring it red would misattribute the fault.
    wf_drift = snapshot.get("required_workflow_drift")
    if isinstance(wf_drift, dict) and wf_drift.get("available"):
        for row in wf_drift.get("repos") or []:
            if not isinstance(row, dict):
                continue
            for wf in row.get("missing") or []:
                yellow.append(
                    f"{row.get('name')}: required workflow '{wf}' has no workflow "
                    f"file — CI can never roll up green"
                )
                hit("required_workflow_drift")
        # A repo whose workflow list could not be read is an unknown, never an
        # implied pass: without it a missing gate would hide behind the error.
        unreadable = [
            str(row.get("name"))
            for row in wf_drift.get("repos") or []
            if isinstance(row, dict) and row.get("error")
        ]
        if unreadable:
            add_stale(
                "required workflows unverified (workflow list unreadable): "
                + ", ".join(sorted(unreadable)),
                "required_workflow_unknown",
            )

    # --- install verification (deep check: RED on fail, YELLOW if stale/unrun) ---
    #
    # The sidecar arrives either from a local `verify_install --report-json` run
    # (index=pypi) or folded out of a release Stage 3 artifact by `validate
    # --ingest` (index=testpypi — the about-to-ship wheels). Both satisfy this
    # leg: for a release gate the wheels about to ship are the right evidence
    # (human decision, 2026-07-15). The index is named in every reason line so
    # the verdict states which install path it actually verified — a TestPyPI
    # pass must never silently read as proof that installing from PyPI works.
    # Development-only `find-links` evidence is useful before publication but
    # cannot satisfy a release gate: a pass stays STALE until PyPI/TestPyPI is
    # exercised. A failure remains RED because an exact local artifact failing
    # its install contract is still actionable evidence.
    #
    # A WARN check row is verdict-neutral, by decision 2026-09-17. The only
    # producer today is check F's released-bootstrap facet in a --version
    # rehearsal: it reports that the Colab bootstrap a reader gets from the
    # CURRENT release is broken, which is not evidence against shipping the
    # candidate — the release is the remedy, and grading it YELLOW would block
    # the very release that fixes it. Only `ready is False` (i.e. a FAIL row)
    # is RED; WARN rows travel in the sidecar and render on the dashboard.
    vi = snapshot.get("verify_install")
    if isinstance(vi, dict) and "ready" in vi:
        index = str(vi.get("index") or "index unknown")
        if vi.get("ready") is False:
            failed = [
                str(c.get("check"))
                for c in (vi.get("checks") or [])
                if isinstance(c, dict) and str(c.get("status")).upper() == "FAIL"
            ]
            red.append(
                f"install verification FAILED ({index}; checks {', '.join(failed) or '?'})"
            )
            hit("install_not_ready")
        elif index == "find-links":
            add_stale(
                "install verification development-only (find-links; "
                "PyPI/TestPyPI evidence required)",
                "install_non_release",
            )
        else:
            age = _age_days(vi.get("ts"), ref)
            if age is None or age > INSTALL_STALE_DAYS:
                add_stale(
                    f"install verification stale ({index}, "
                    + ("age unknown)" if age is None else f"{int(age)}d old)"),
                    "install_stale",
                )
    else:
        add_stale("install verification not run", "install_unknown")

    # --- release-validation gate (hard) -------------------------------------
    # GREEN-for-release now REQUIRES a fresh, passing validation_report whose
    # commit_shas match the current `main` HEADs and whose profile == release.
    # This is the M2 gate: the report proves the exact source about to ship was
    # built, published to TestPyPI, installed from the wheel, and exercised at
    # release fidelity. Absent/stale/source-not-matching → YELLOW ("no release
    # rehearsal for current source"); failing → RED. `validation_outcome` is the
    # RED axis; fidelity+freshness (profile / commit_shas / age) is the YELLOW
    # axis — a passing-but-stale report is a caution, not a blocker.
    #
    # Read `validation_outcome`, NOT `release_ready`: the boolean collapses
    # "something failed" and "the rehearsal evidence is missing" into one
    # `false`, and the tick's integrate-only auto-ingest always lands on that
    # `false` (see heart/validate.py). Grading it RED reported a failure that
    # had not happened. A report predating the field carries no discriminator,
    # so `false` there stays RED — fail closed.
    vr = snapshot.get("validation_report")
    if isinstance(vr, dict) and vr:
        outcome = validate.report_outcome(vr)
        if outcome == "incomplete":
            # Nothing is wrong; the rehearsal evidence is simply absent. The
            # wording must contain "release validation" — the Health Agent
            # classifier matches on that string to route the remedy.
            add_stale("release validation incomplete: no rehearsal for current source",
                      "validation_absent")
        elif outcome == "fail":
            vr_stages = vr.get("stages")
            failed_stages = [
                n for n, s in (vr_stages if isinstance(vr_stages, dict) else {}).items()
                if isinstance(s, dict) and s.get("status") == "fail"
            ]
            red.append(
                "release validation FAILED"
                + (f" (stage {', '.join(failed_stages)})" if failed_stages else "")
            )
            hit("validation_failed")
        elif outcome == "pass":
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
            vr_profile = str(vr.get("profile") or "").strip().lower()
            # M2.4 (PyAutoHands#337): a release publishes EXACTLY the rehearsed
            # commits, so main moving FORWARD from them does not stale the
            # rehearsal. A moved repo stays fresh when the rehearsed SHA is
            # proven an ancestor of the current HEAD (rehearsal_ancestry
            # sidecar, both SHAs matching); it is stale when proven NOT an
            # ancestor (main rewritten / off-main commit) or when unproven.
            anc_rows = (snapshot.get("rehearsal_ancestry") or {}).get("repos") or {}
            not_ancestor: list[str] = []
            unproven: list[str] = []
            for lib in mismatched:
                row = anc_rows.get(lib) if isinstance(anc_rows, dict) else None
                cur = ((repos.get(lib) or {}).get("ci_status") or {}).get("head_sha")
                if not (isinstance(row, dict)
                        and _sha_eq(row.get("rehearsed"), commit_shas.get(lib))
                        and _sha_eq(row.get("head"), cur)
                        and row.get("status") not in (None, "", "unknown")):
                    unproven.append(lib)
                elif row.get("ancestor") is True:
                    validation_ahead[lib] = row.get("ahead_by")
                else:
                    not_ancestor.append(lib)
            if not commit_shas:
                add_stale("release validation source unconfirmed (no commit_shas)",
                          "validation_unknown")
            elif not_ancestor:
                add_stale(
                    "release validation stale: rehearsed commit not an ancestor of main ("
                    + ", ".join(not_ancestor) + ")",
                    "validation_stale_sha",
                )
            elif unproven:
                add_stale(
                    "release validation stale: source moved since rehearsal ("
                    + ", ".join(unproven) + "; ancestry unproven)",
                    "validation_stale_sha",
                )
            elif confirmed == 0 and not validation_ahead:
                add_stale("release validation source unconfirmed (current HEADs unknown)",
                          "validation_unknown")
            elif unconfirmed:
                # Some libs matched, but at least one gated repo's SHA could not
                # be confirmed either way — an unknown must never be silently
                # treated as green (same principle every other gate here follows).
                add_stale(
                    "release validation partially unconfirmed (repo(s) with unknown "
                    "current HEAD: " + ", ".join(unconfirmed) + ")",
                    "validation_unknown",
                )
            elif vr_profile != "release":
                add_stale(
                    f"release validation profile '{vr.get('profile') or '?'}' is not 'release'",
                    "validation_profile",
                )
            else:
                age = _age_days(vr.get("ts"), ref)
                if age is None or age > VALIDATION_STALE_DAYS:
                    add_stale(
                        "release validation stale "
                        + ("(age unknown)" if age is None else f"({int(age)}d old)"),
                        "validation_stale",
                    )
                # else: fresh, passing, matching, release profile → GREEN-eligible
        else:
            add_stale("release validation status unknown", "validation_unknown")
    else:
        add_stale("no release validation for current source", "validation_absent")

    # --- Stage 3 quarantine (PyAutoHeart#344; ruling 2026-11-12) ---
    # An ACTIVE entry is YELLOW with the list: its script was skipped, and a
    # skipped script can never make the verdict GREEN. An EXPIRED or refused
    # entry is RED — the script gates again. A file that cannot be read is RED
    # too: an unreadable policy is never an empty one.
    try:
        q_entries = quarantine if quarantine is not None else _quarantine.load()
        q_red, q_yellow = _quarantine.readiness_reasons(q_entries, ref.date())
        q_active = _quarantine.classify(q_entries, ref.date())["active"]
    except (OSError, ValueError, yaml.YAMLError) as exc:
        q_red, q_yellow = [f"Stage 3 quarantine file unreadable: {exc}"], []
        q_active = []
    # What the ingested rehearsal ACTUALLY skipped. The live file alone is not
    # enough: once an entry is removed or expires, a rehearsal that never ran
    # its script would read GREEN (and M2.4 ancestry keeps it fresh). Each
    # skipped script is at least YELLOW; RED when its entry is no longer active
    # — only a fresh rehearsal that ran the script clears it.
    live = {(str(r["entry"].get("workspace")), str(r["entry"].get("script")))
            for r in q_active}
    vr_q = (vr.get("quarantined") if isinstance(vr, dict) else None) or []
    for row in (r for r in vr_q if isinstance(r, dict)):
        e = row.get("quarantine_entry") if isinstance(row.get("quarantine_entry"), dict) else {}
        key = (str(e.get("workspace")), str(e.get("script")))
        label = (f"{key[0]}/{key[1]}" if e.get("script")
                 else f"{row.get('project')}: {row.get('script')}")
        if key in live:
            q_yellow.append(f"Stage 3 rehearsal skipped {label} (quarantined, "
                            f"{row.get('quarantined') or e.get('bug')})")
        else:
            q_red.append(f"Stage 3 rehearsal skipped {label}, but its quarantine "
                         "is no longer active — re-run the rehearsal so it runs")
    for t in q_red:
        red.append(t)
        hit("quarantine_expired" if ("EXPIRED" in t or "no longer active" in t)
            else "quarantine_refused")
    for t in q_yellow:
        yellow.append(t)
        hit("quarantine_active")
    # A pass on Stage 3's one retry is recorded as flaky (shown, not a tier).
    stage3_flaky = [f for f in ((vr or {}).get("flaky") or []) if isinstance(f, dict)] \
        if isinstance(vr, dict) else []
    # Shown on every surface, never a tier: the retry is the ruling's, and a
    # flaky pass is a pass — but a human reading the verdict must see it.
    flaky_reasons = [f"flaky: passed on retry — {f.get('project')}: {f.get('script')}"
                     + (f" (first: {f.get('first_error')})" if f.get("first_error") else "")
                     for f in stage3_flaky]

    # --- release push credential (RED on a refused push, STALE if unproven) ---
    # heart/checks/token_push.py: the pyautolabs-bot App's `git push --dry-run`
    # to each release library. A refusal means the release's tag push would
    # fail — a real blocker. A missing, partial or aged sidecar is an evidence
    # gap (re-run the check), never an implied pass.
    tp = snapshot.get("token_push")
    tp_repos = tp.get("repos") if isinstance(tp, dict) else None
    if isinstance(tp_repos, dict) and tp_repos:
        identity = str(tp.get("identity") or "identity unknown")
        refused = [lib for lib in libs
                   if isinstance(tp_repos.get(lib), dict) and tp_repos[lib].get("push") is False]
        unproven = [lib for lib in libs
                    if not isinstance(tp_repos.get(lib), dict)
                    or not isinstance(tp_repos[lib].get("push"), bool)]
        age = _age_days(tp.get("checked_at"), ref)
        if refused:
            red.append(f"release push credential refused ({identity}): {', '.join(refused)}")
            hit("token_push_refused")
        elif unproven:
            add_stale(f"release push credential unverified ({identity}): {', '.join(unproven)}",
                      "token_push_unknown")
        elif age is None or age * 24 > TOKEN_PUSH_STALE_HOURS:
            add_stale(
                f"release push credential check stale ({identity}, "
                + ("age unknown)" if age is None else f"{int(age * 24)}h old)"),
                "token_push_stale",
            )
    else:
        add_stale("release push credential check not run", "token_push_unknown")

    # --- script timing (YELLOW) ---
    timing = snapshot.get("script_timing", {}) or {}
    if _as_int(timing.get("red_count", 0)) > 0:
        yellow.append(f"{_as_int(timing.get('red_count'))} script timing regression(s)")
        hit("timing_red")
    elif _as_int(timing.get("yellow_count", 0)) > 0:
        yellow.append(f"{_as_int(timing.get('yellow_count'))} slow script(s)")
        hit("timing_yellow")

    # --- profiling pinned-value drift (YELLOW) ---
    # autolens_profiling result JSONs record drift against pinned
    # likelihood/evidence baselines instead of crashing (profiling records
    # and flags; adjudication is autolens_workspace_test's remit). Any
    # drifted result means the profiling baselines are non-comparable
    # until resolved — library regression or stale pin, either way a
    # human decision.
    drift = snapshot.get("profiling_drift", {}) or {}
    drift_findings = drift.get("findings") or []
    if drift_findings:
        for f in drift_findings[:5]:
            yellow.append(
                f"profiling drift: {f.get('path')} "
                f"[{', '.join(str(d.get('label')) for d in (f.get('drift') or []))}]"
            )
            hit("profiling_drift")
        if len(drift_findings) > 5:
            yellow.append(
                f"profiling drift: +{len(drift_findings) - 5} more drifted result(s)"
            )

    # --- open PRs across all repos (YELLOW) ---
    for name, body in sorted(repos.items()):
        if not isinstance(body, dict):
            continue
        pr = body.get("open_prs", {}) or {}
        if _as_int(pr.get("open_count", 0)) > 0 and _as_int(pr.get("max_age_days", 0)) >= 7:
            yellow.append(f"{name}: open PR {_as_int(pr.get('max_age_days'))}d old")
            hit("open_pr")
            repository_reasons.append({"repo": name, "key": "open_pr",
                                       "text": yellow[-1], "severity": "yellow"})

    # --- release-ci: name the dev-box-local evidence this snapshot never
    # carried, so "out of scope" is a stated fact, not an assumption. Slices
    # that ARE present were evaluated in full by the gates above.
    if release_ci:
        unobserved = [
            k for k in ("version_skew", "script_timing", "manifest_drift", "profiling_drift")
            if not snapshot.get(k)
        ]
        if not any(
            isinstance(repos.get(lib), dict) and (repos.get(lib) or {}).get("repo_state")
            for lib in libs
        ):
            unobserved.insert(0, "repo_state")
        if unobserved:
            na.append(
                "not observed in this snapshot (dev-box-local — out of scope "
                "for release-ci): " + ", ".join(unobserved)
            )

    # --- score ---
    score = 100
    penalties = []
    for key, n in counts.items():
        w, cap = _WEIGHTS.get(key, (0, 0))
        points = min(n * w, cap)
        score -= points
        penalties.append({"key": key, "count": n, "weight": w,
                          "cap": cap, "points": points})
    score = max(0, min(100, score))

    verdict = "red" if red else ("yellow" if yellow else ("stale" if stale else "green"))
    return {
        "verdict": verdict,
        "score": score,
        "penalties": penalties,
        "repository_reasons": repository_reasons,
        "profile": profile,
        "reasons": red + yellow + stale,
        "red_reasons": red,
        "yellow_reasons": yellow,
        "stale_reasons": stale,
        # Each stale reason with the gate key that produced it — the surfaces
        # key their remedy off this and never off the sentence. Additive: the
        # flat lists above are the contract every existing consumer reads.
        "stale_details": [{"text": t, "key": k} for t, k in zip(stale, stale_keys)],
        "na_reasons": na,
        "stage3_quarantine": q_yellow,
        "stage3_flaky": stage3_flaky,
        "flaky_reasons": flaky_reasons,
        "validation_ahead": validation_ahead,
        "validation_note": (
            "release validation covers the rehearsed commits; main is ahead by "
            + ", ".join(f"{lib} {n if n is not None else '?'}"
                        for lib, n in validation_ahead.items())
            + " commit(s) — a release publishes the rehearsed SHAs (M2.4)"
        ) if validation_ahead else "",
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


# --- the development-ship gate (PyAutoHeart#340) ----------------------------
#
# See the module docstring, "The development-ship gate". The release verdict
# above is computed in full and then *partitioned*: the reasons this gate
# enforces block, every other reason is carried in ``shown_reasons``. On top of
# that partition, every in-scope repo's ``main`` CI is read straight from the
# snapshot, whatever its group — an organ or a developer workspace is gated by
# nothing in ``compute``, but red CI on it is still the branch's business.
#
# WHAT IT READS. The snapshot passed in. From the CLI that is the dev box's
# local ``$HEART_STATE_DIR/state.json`` (default ``~/.pyauto-heart/``), written
# by ``pyauto-heart tick`` / the watch daemon — NOT the published cloud feed.
# The event triggers (heart-health.yml: ``repository_dispatch: heart-refresh``
# from library CI on main, Release Integrate completing) refresh the PUBLISHED
# cloud feed (Pages ``state.json`` / ``board.json``), which the Brain's door
# (``bin/heart_feed.py``) reads; they do not touch a dev box's local state.
# The verdict names its ``source`` and that source's age, so a reader always
# knows which of the two it is looking at.

#: Evidence older than this many hours is STALE at a development ship. Also the
#: largest maximum a caller may ask for (the ruling's ceiling); smaller is allowed.
DEV_MAX_FEED_AGE_HOURS = 6.0

#: A snapshot dated this far in the future is clock skew; beyond it the age is
#: unknown (STALE), never "fresh".
DEV_CLOCK_SKEW_HOURS = 5.0 / 60.0

#: The reason keys of ``compute`` a development ship enforces, all of them CI on
#: ``main`` of one named repo (``repository_reasons[].key``).
DEV_ENFORCED_KEYS = frozenset({"lib_ci", "ws_ci", "lib_ci_unavailable", "lib_unknown"})

#: The shown reason for a gate-deferred repo (PyAutoHeart#343).
GATE_DEFERRED_REASON = "not Heart-gated: PR checks decide"


def load_dependencies(config_path: Path | str = CONFIG_PATH) -> dict[str, list[str]]:
    """``dependencies:`` from the policy file — library -> libraries it imports.

    Strict: the dev-ship scope is built from it, and a silently missing graph
    would shrink every scope to the touched repos alone (dependants unblocked),
    so an absent or malformed block fails loudly."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    deps = cfg.get("dependencies")
    if not isinstance(deps, dict) or not deps:
        raise ValueError(f"no dependencies graph in {config_path}")
    out: dict[str, list[str]] = {}
    for lib, ups in deps.items():
        if not isinstance(ups, list):
            raise ValueError(f"dependencies.{lib} must be a list in {config_path}")
        out[str(lib)] = [str(u) for u in ups]
    return out


def load_repo_groups(config_path: Path | str = CONFIG_PATH) -> dict[str, str]:
    """Every polled repo -> its group (``repos.<group>[].name``)."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    return {
        r["name"]: group
        for group, entries in (cfg.get("repos") or {}).items()
        for r in entries or []
        if isinstance(r, dict) and r.get("name")
    }


def load_known_repo_names(config_path: Path | str = CONFIG_PATH) -> list[str]:
    """Every repo the policy file names anywhere — polled groups, ``excluded``,
    ``version_skew``, the smoke table, the dependency graph. A dev-ship
    ``--repos`` entry outside this set is a usage error (a typo), never a
    silent pass."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    names = set(load_repo_groups(config_path))
    names |= {str(x) for x in cfg.get("excluded") or [] if isinstance(x, str)}
    names |= {str(k) for k in (cfg.get("version_skew") or {})}
    smoke = cfg.get("smoke") or {}
    names |= {str(k) for k in (smoke.get("import_names") or {})}
    names |= {str(w.get("directory")) for w in (smoke.get("workspaces") or {}).values()
              if isinstance(w, dict) and w.get("directory")}
    for lib, ups in (cfg.get("dependencies") or {}).items():
        names.add(str(lib))
        names |= {str(u) for u in ups or []}
    return sorted(names)


def load_not_heart_gated(config_path: Path | str = CONFIG_PATH) -> list[str]:
    """``not_heart_gated:`` — unpolled repos whose PR checks are the gate
    (PyAutoHeart#343). Strict: a name that Heart polls cannot also be deferred
    (its main CI is the evidence), and every name must be in ``excluded:`` —
    a misfiled name raises rather than silently ungating a polled repo."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    raw = cfg.get("not_heart_gated") or []
    if not isinstance(raw, list):
        raise ValueError(f"not_heart_gated must be a list in {config_path}")
    names = [str(x) for x in raw]
    polled = set(load_repo_groups(config_path))
    excluded = {str(x) for x in cfg.get("excluded") or [] if isinstance(x, str)}
    bad = [n for n in names if n in polled or n not in excluded]
    if bad:
        raise ValueError("not_heart_gated names must be unpolled repos listed in "
                         f"excluded: {', '.join(bad)} ({config_path})")
    return names


def load_release_scope_workflows(config_path: Path | str = CONFIG_PATH) -> frozenset[str]:
    """``dev_ship.release_scope_workflows`` — workflow names that never count
    as a repo's own ``main`` CI at a development ship. Absent ⇒ none (every
    run counts: the fail-closed direction)."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    wfs = (cfg.get("dev_ship") or {}).get("release_scope_workflows") or []
    return frozenset(str(w) for w in wfs)


def load_ci_workflows(config_path: Path | str = CONFIG_PATH) -> dict[str, list[str]]:
    """``dev_ship.ci_workflows`` — repo -> the workflows that are its own CI
    (advisory groups only; libraries and gated workspaces use
    ``required_workflows``). Absent repo ⇒ no declaration (STALE at a ship)."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    raw = (cfg.get("dev_ship") or {}).get("ci_workflows") or {}
    return {str(k): [str(w) for w in (v or [])] for k, v in raw.items()}


def dependants_of(repos: Sequence[str], dependencies: dict[str, list[str]]) -> set[str]:
    """Everything downstream of ``repos`` in the graph, transitively (the
    repos themselves excluded unless they sit downstream of one another)."""
    down: dict[str, set[str]] = {}
    for lib, ups in dependencies.items():
        for up in ups:
            down.setdefault(up, set()).add(lib)
    seen: set[str] = set()
    frontier = list(repos)
    while frontier:
        for d in down.get(frontier.pop(), ()):
            if d not in seen:
                seen.add(d)
                frontier.append(d)
    return seen


def dev_scope(
    repos: Sequence[str],
    dependencies: dict[str, list[str]],
    known: Sequence[str] = (),
    universe: Sequence[str] | None = None,
    not_gated: Sequence[str] = (),
) -> dict[str, list[str]]:
    """The repos a development ship answers for.

    ``repos`` is what the caller states the branch touches (comma-separated
    entries are split). Names match the policy's spelling case-insensitively
    and are reported in it. ``known`` is the polled set; a touched repo outside
    it is listed under ``not_polled`` — Heart holds no evidence about it, so
    ``dev_ship`` reads it STALE, never green. With ``universe`` (every name the
    policy file mentions), a name outside it raises ``ValueError``: a typo is a
    usage error, not a repo. A touched repo in ``not_gated`` (and not polled)
    is ``gate_deferred``: Heart does not judge it, its PR checks do, so it is
    neither ``not_polled`` nor ``enforced``."""
    pool = (*(universe or ()), *known, *not_gated, *dependencies,
            *(u for ups in dependencies.values() for u in ups))
    canon = {n.lower(): n for n in pool}
    touched: list[str] = []
    for raw in repos:
        for part in str(raw).split(","):
            name = part.strip()
            if name:
                name = canon.get(name.lower(), name)
                if name not in touched:
                    touched.append(name)
    if universe is not None:
        unknown = [t for t in touched if t.lower() not in {n.lower() for n in pool}]
        if unknown:
            raise ValueError("not a repo named in config/repos.yaml: " + ", ".join(unknown))
    known_l = {n.lower() for n in known}
    deferred_l = {n.lower() for n in not_gated} - known_l
    deferred = [t for t in touched if t.lower() in deferred_l]
    judged = [t for t in touched if t not in deferred]
    dependants = sorted(dependants_of(judged, dependencies) - set(touched))
    return {
        "touched": touched,
        "dependants": dependants,
        "enforced": [*judged, *dependants],
        "not_polled": [t for t in judged if known and t.lower() not in known_l],
        "gate_deferred": deferred,
    }


def split_dev_reasons(
    verdict: dict[str, Any], enforced: Sequence[str]
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Partition a release verdict for a development ship.

    Returns ``(blocking_red, blocking_stale, shown)``: the enforced reasons
    (``DEV_ENFORCED_KEYS`` on an in-scope repo) by tier, and every other reason
    of the verdict as ``{"severity", "text"}`` — shown, never dropped."""
    scope = {str(r).lower() for r in enforced}
    blocking = [
        r for r in verdict.get("repository_reasons") or []
        if isinstance(r, dict) and r.get("key") in DEV_ENFORCED_KEYS
        and str(r.get("repo", "")).lower() in scope
    ]
    red = [str(r.get("text")) for r in blocking if r.get("severity") == "red"]
    stale = [str(r.get("text")) for r in blocking if r.get("severity") == "stale"]
    taken = set(red) | set(stale)
    shown = [
        {"severity": sev, "text": str(t)}
        for sev, key in (("red", "red_reasons"), ("yellow", "yellow_reasons"),
                         ("stale", "stale_reasons"), ("flaky", "flaky_reasons"))
        for t in verdict.get(key) or []
        if str(t) not in taken
    ]
    return red, stale, shown


def feed_age_hours(ts: Any, now: datetime.datetime) -> float | None:
    """Hours since ``ts``. None — unknown, so STALE — when it is absent,
    unparseable, or further in the future than ``DEV_CLOCK_SKEW_HOURS``; a
    future stamp within the skew reads as 0."""
    t = _parse_ts(ts)
    if t is None:
        return None
    age = (now - t).total_seconds() / 3600.0
    if age < -DEV_CLOCK_SKEW_HOURS:
        return None
    return max(0.0, age)


def check_max_age_hours(value: Any) -> float:
    """A caller's maximum feed age: finite, positive, at most the ruling's 6 h."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"max feed age {value!r} is not a number") from None
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"max feed age must be finite and positive, got {value!r}")
    if v > DEV_MAX_FEED_AGE_HOURS:
        raise ValueError(f"max feed age {v:g} h exceeds the {DEV_MAX_FEED_AGE_HOURS:g} h ceiling")
    return v


def _no_conclusion(c: Any) -> bool:
    return c in (None, "")


def _in_scope_ci(
    snapshot: dict, enforced: Sequence[str], groups: dict[str, str],
    gate_libs: set[str], req_wf: dict[str, list[str]],
    now: datetime.datetime, max_age_hours: float,
    release_scope: frozenset[str] = frozenset(),
    ci_workflows: dict[str, list[str]] | None = None,
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """``main`` CI of every in-scope repo, read from the snapshot: (red, stale).

    Release-gate libraries and gated workspaces already carry their red / error
    / absent reasons from ``compute``; this adds what the release gate passes
    over — no conclusion yet, aged evidence — and gives every other group
    (organs, build workflow, developer workspaces) the same treatment in full.
    For an advisory group only the repo's declared CI workflows
    (``ci_workflows``) count, each judged on its latest run: any failing →
    red; any unfinished, or with no recorded run, → stale. Every
    other workflow (ops: boards, sweeps, schedules; never a ``release_scope``
    one) failing on HEAD is returned in ``shown``, never blocking. An advisory
    repo with no declaration is STALE. Returns ``(red, stale, shown)``."""
    repos = snapshot.get("repos") or {}
    ci_workflows = ci_workflows or {}
    red: list[str] = []
    stale: list[str] = []
    shown: list[dict[str, str]] = []
    for name in enforced:
        group = groups.get(name)
        if group is None:
            continue  # not polled: reported by the caller
        body = repos.get(name)
        ci = body.get("ci_status") if isinstance(body, dict) else None
        ci = ci if isinstance(ci, dict) and ci else None
        if group == "libraries" and name in gate_libs:
            if ci and not ci.get("error") and _no_conclusion(ci.get("conclusion")):
                stale.append(f"{name}: CI on main has no conclusion yet "
                             f"({ci.get('status') or 'status unknown'})")
        elif ci is None:
            stale.append(f"{name}: CI status on main not observed")
        elif ci.get("error"):
            stale.append(f"{name}: CI status unavailable ({ci['error']})")
        elif group in GATED_WORKSPACE_GROUPS and req_wf.get(group):
            wfs = ci.get("workflows")
            if isinstance(wfs, dict):
                for wf in req_wf[group]:
                    if _no_conclusion((wfs.get(wf) or {}).get("conclusion")):
                        stale.append(f"{name}: {wf} on main has no conclusion yet")
            elif _no_conclusion(ci.get("conclusion")):
                stale.append(f"{name}: CI on main has no conclusion yet")
        elif name not in ci_workflows:
            stale.append(f"{name}: no CI workflows declared (config/repos.yaml "
                         "dev_ship.ci_workflows) — its main CI cannot be judged")
        else:
            declared = set(ci_workflows[name])
            raw = ci.get("workflows")
            every = {wf: e for wf, e in (raw.items() if isinstance(raw, dict) else ())
                     if isinstance(e, dict)}
            for wf, e in sorted(every.items()):
                if (wf not in declared and wf not in release_scope and e.get("on_head")
                        and e.get("conclusion") in FAILURE_CONCLUSIONS):
                    shown.append({"severity": "red",
                                  "text": f"{name}: {wf} {e.get('conclusion')} on main "
                                          "(ops workflow — not CI, not blocking)"})
            # The LATEST run of EACH declared workflow is judged (ci_status
            # keeps one entry per workflow — its newest run on main): any red
            # blocks, any missing or unfinished one is STALE. One workflow's
            # newer green never hides another's red.
            failing: list[str] = []
            for wf in sorted(declared):
                e = every.get(wf)
                if e is None:
                    stale.append(f"{name}: {wf} has no recorded run on main "
                                 "(not in the CI status page) — cannot be judged")
                elif e.get("conclusion") in FAILURE_CONCLUSIONS:
                    failing.append(f"{wf} {e.get('conclusion')}")
                elif e.get("conclusion") != "success":
                    stale.append(f"{name}: {wf} on main has no conclusion yet")
            if failing:
                red.append(f"{name}: {', '.join(failing)} on main")
        # The evidence's own collection time, where the sidecar carries one;
        # the snapshot's aggregation ts is judged separately by the caller.
        if ci and ci.get("ts"):
            age = feed_age_hours(ci.get("ts"), now)
            if age is None:
                stale.append(f"{name}: CI evidence age unknown (ts {ci.get('ts')})")
            elif age > max_age_hours:
                stale.append(f"{name}: CI evidence {age:.1f} h old, over the "
                             f"{max_age_hours:g} h maximum — re-check, or park")
    return red, stale, shown


def dev_ship(
    snapshot: dict | None,
    repos: Sequence[str],
    *,
    now: datetime.datetime | None = None,
    max_age_hours: float = DEV_MAX_FEED_AGE_HOURS,
    libraries: Sequence[str] | None = None,
    required_workflows: dict[str, list[str]] | None = None,
    dependencies: dict[str, list[str]] | None = None,
    groups: dict[str, str] | None = None,
    universe: Sequence[str] | None = None,
    release_scope: Sequence[str] | None = None,
    ci_workflows: dict[str, list[str]] | None = None,
    not_gated: Sequence[str] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """The development-ship verdict for a branch touching ``repos``.

    Pure apart from the policy-file reads its defaults make. GREEN is the only
    pass; RED is red CI on ``main`` in scope; STALE is in-scope CI evidence
    missing, unfinished or aged, a touched repo Heart does not poll, or a
    snapshot older than ``max_age_hours`` (or of unknown age) — re-check, or
    park. A touched repo on the ``not_heart_gated`` list is gate-deferred:
    listed and shown, never STALE or blocking (PyAutoHeart#343); its PR
    checks decide at merge. An in-scope RED outranks staleness: the last known result was
    adverse, exactly as the release tiers treat it. Raises ``ValueError`` for
    a name the policy file never mentions or an invalid maximum age."""
    snapshot = snapshot or {}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    max_age_hours = check_max_age_hours(max_age_hours)
    libs = list(libraries) if libraries is not None else load_release_gate_names()
    req_wf = required_workflows if required_workflows is not None else load_required_workflows()
    full = compute(snapshot, libraries=libs, required_workflows=req_wf)
    deps = dependencies if dependencies is not None else load_dependencies()
    groups_given = groups
    groups = groups if groups is not None else load_repo_groups()
    if universe is None and groups_given is None:
        universe = load_known_repo_names()  # live policy: a typo is a usage error
    ng = (list(not_gated) if not_gated is not None
          else load_not_heart_gated() if groups_given is None else [])
    scope = dev_scope(repos, deps, known=list(groups), universe=universe, not_gated=ng)
    red, stale, shown = split_dev_reasons(full, scope["enforced"])
    for name in scope["gate_deferred"]:
        shown.append({"severity": "deferred",
                      "text": f"{name}: {GATE_DEFERRED_REASON}"})

    if not scope["touched"]:
        stale.append("no touched repos stated — the dev-ship scope is unknown")
    for name in scope["not_polled"]:
        stale.append(f"not polled by Heart: {name}")

    rs = (frozenset(release_scope) if release_scope is not None
          else load_release_scope_workflows() if groups_given is None else frozenset())
    ciw = (ci_workflows if ci_workflows is not None
           else load_ci_workflows() if groups_given is None else {})
    ci_red, ci_stale, ci_shown = _in_scope_ci(snapshot, scope["enforced"], groups, set(libs),
                                             req_wf, now, max_age_hours, rs, ciw)
    red += ci_red
    stale += ci_stale
    shown += ci_shown

    age = feed_age_hours(snapshot.get("ts"), now)
    # Every touched repo deferred: no Heart evidence enters the verdict, so the
    # snapshot's age cannot make it stale (its age is still reported).
    heart_judged = bool(scope["enforced"]) or not scope["touched"]
    if heart_judged and age is None:
        stale.append("Heart snapshot age unknown (no ts, or dated in the future) "
                     "— re-check, or park")
    elif heart_judged and age > max_age_hours:
        stale.append(f"Heart snapshot {age:.1f} h old, over the {max_age_hours:g} h "
                     "maximum — re-check, or park")

    verdict = "red" if red else ("stale" if stale else "green")
    return {
        "gate": "dev-ship",
        "verdict": verdict,
        "pass": verdict == "green",
        "remedy": {"red": "fix the failing in-scope CI on main, or park",
                   "stale": "re-check (refresh the evidence, then ask again), or park",
                   "green": ""}[verdict],
        "scope": scope,
        "gate_deferred": list(scope["gate_deferred"]),
        "red_reasons": red,
        "yellow_reasons": [],
        "stale_reasons": stale,
        "reasons": red + stale,
        "shown_reasons": shown,
        "organism_verdict": full["verdict"],
        "source": source or "snapshot passed in",
        "feed_ts": snapshot.get("ts"),
        "feed_age_hours": None if age is None else round(age, 2),
        "max_feed_age_hours": max_age_hours,
        "ts": now.isoformat(),
    }


def render_dev(verdict: dict[str, Any]) -> list[str]:
    """The human reading of a ``dev_ship`` verdict."""
    v = str(verdict.get("verdict", "?"))
    colour = {"green": c_ok, "red": c_fail}.get(v, c_warn)
    glyph = {"green": glyph_ok, "red": glyph_fail}.get(v, glyph_warn)
    scope = verdict.get("scope") or {}
    head = ", ".join(scope.get("touched") or []) or "?"
    if scope.get("dependants"):
        head += f" (+ dependants {', '.join(scope['dependants'])})"
    if scope.get("gate_deferred"):
        head += f" (gate-deferred {', '.join(scope['gate_deferred'])})"
    age = verdict.get("feed_age_hours")
    lines = [c_bold(f"DEV SHIP  {head}"),
             f"  {glyph()} {colour(v.upper())}  "
             + ("passes" if verdict.get("pass") else f"blocks — {verdict.get('remedy')}"),
             c_meta(f"  judged: {verdict.get('source')}, "
                    f"age {'unknown' if age is None else f'{age:.1f} h'} "
                    f"(max {verdict.get('max_feed_age_hours'):g} h)")]
    for t in verdict.get("red_reasons") or []:
        lines.append(f"    {c_fail('red  ')} {t}")
    for t in verdict.get("stale_reasons") or []:
        lines.append(f"    {c_warn('stale')} {t}")
    shown = verdict.get("shown_reasons") or []
    if shown:
        lines.append(c_info(f"  shown, not blocking (organism verdict "
                            f"{str(verdict.get('organism_verdict', '?')).upper()}):"))
        for r in shown:
            lines.append(f"    {r['severity']:<6} {r['text']}")
    return lines


def render_block(verdict: dict[str, Any], quiet: bool = False) -> list[str]:
    """The coloured RELEASE READINESS header block.

    Delegates to ``heart/dashboard.py`` so the readiness header has exactly one
    definition, shared by ``readiness`` output, ``status``, and the unified
    ``dashboard`` board (the "one renderer" invariant).
    """
    from heart import dashboard
    return dashboard.render_readiness_block(verdict, quiet=quiet)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pyauto-heart readiness")
    ap.add_argument("--json", action="store_true", help="print the raw verdict JSON")
    ap.add_argument("--quiet", action="store_true", help="verdict line + top reason only")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    ap.add_argument(
        "--profile",
        choices=["default", "release-ci"],
        default="default",
        help="evidence profile: 'release-ci' scopes dev-box-local gaps to "
             "na_reasons for the scheduled-nightly gate (design §5); default "
             "is unchanged dev-box behaviour",
    )
    ap.add_argument(
        "--dev",
        action="store_true",
        help="the development-ship gate: block only on CI on main of --repos "
             "and their dependants; every other reason is shown, not "
             "blocking; STALE never passes (implied by --repos)",
    )
    ap.add_argument(
        "--repos",
        action="append",
        default=[],
        metavar="REPO[,REPO...]",
        help="the repos the branch touches (repeatable or comma-separated)",
    )
    def _max_age(raw: str) -> float:
        try:
            return check_max_age_hours(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    ap.add_argument(
        "--max-feed-age-hours",
        type=_max_age,
        default=DEV_MAX_FEED_AGE_HOURS,
        help=f"dev gate: evidence older than this is STALE (default and ceiling "
             f"{DEV_MAX_FEED_AGE_HOURS:g}; a smaller positive value is allowed)",
    )
    ns = ap.parse_args(argv)
    if ns.no_color:
        os.environ["NO_COLOR"] = "1"

    if ns.dev or ns.repos:
        repos = [p.strip() for r in ns.repos for p in r.split(",") if p.strip()]
        if not repos:
            ap.error("--dev needs --repos: the repos this branch touches")
        if ns.profile != "default":
            ap.error("--dev reads the dev-box snapshot; it takes no --profile")
        # Always a live compute over the dev box's LOCAL snapshot (the tick's
        # state.json — not the published cloud feed), never persisted:
        # release_ready.json stays the release verdict its consumers expect.
        source = f"local snapshot {state.HEART_STATE_FILE}"
        try:
            dev = dev_ship(state.load() or {}, repos, max_age_hours=ns.max_feed_age_hours,
                           source=source)
        except ValueError as exc:
            ap.error(str(exc))
        if ns.json:
            json.dump(dev, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return 0
        lines = render_dev(dev)
        for line in lines[:2] if ns.quiet else lines:
            print(line)
        return 0

    if ns.profile != "default":
        # Profile verdicts are always a live compute and are never persisted:
        # release_ready.json stays the default-profile artifact its existing
        # consumers (dashboard/status) expect.
        verdict = compute(state.load() or {}, profile=ns.profile)
    else:
        verdict = load_verdict()
    if ns.json:
        json.dump(verdict, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    for line in render_block(verdict, quiet=ns.quiet):
        print(line)
    for line in verdict.get("na_reasons") or []:
        print(f"  n/a  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
