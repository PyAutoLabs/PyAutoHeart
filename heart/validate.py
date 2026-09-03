"""heart/validate.py — ingest release-validation artifacts into one report.

This is the **M2 foundation** (plus the M3 CI-side bridge) of the
Brain→Health→Heart release-validation redesign. It owns three things and
nothing else:

1. The **schema** of ``validation_report.json`` — the tracked artifact that
   answers *"was the exact source about to ship built, published to TestPyPI,
   installed from the wheel, and exercised at release fidelity — and did it
   pass?"*.
2. ``pyauto-heart validate --ingest <artifacts...>`` — **ingest-and-judge
   only**. It consumes the artifacts/conclusions the Brain Release Agent has
   already collected (the M1 TestPyPI rehearsal artifact, and — from M3 — the
   wheel-based integration ``report.json``), assembles the single
   ``validation_report.json``, computes ``release_ready``, persists it in Heart
   state, and archives a history copy.
3. ``to_stage_report`` / ``pyauto-heart validate --emit-stage-report`` — the M3
   bridge run **inside** the ``workspace-validation.yml`` body (the
   ``release-integrate.yml`` channel; still Heart's own code, in Heart's own
   CI, writing only to the job workspace — not a foreign repo, not a build
   dispatch). It reshapes Build's ``aggregate_results.py``
   report.json (Build's own vocabulary: ``ready``, ``file``, no ``stage``) into
   the ``{"stage": ..., "status": "pass"|"fail", ...}`` contract ``--ingest``
   expects, optionally folding in a ``verify_install`` sidecar. The Release
   Agent uploads/downloads this artifact and later feeds it to ``--ingest``
   alongside the ``commit_shas`` it read while orchestrating the build.

**Boundary (non-negotiable, mirrors AGENTS.md).** This module NEVER dispatches
a build, never talks to GitHub, never mutates any repo. All dispatching / polling
/ artifact download is the Brain Release Agent's job; Heart is spec + ingest +
verdict, credential-free. It writes ONLY under ``~/.pyauto-heart/`` (``--ingest``)
or an explicit ``--out`` path inside CI's own job workspace (``--emit-stage-report``).

Schema of ``validation_report.json`` (``schema_version`` 1)::

    {
      "schema_version": 1,
      "release_ready": true,            # legacy boolean, kept for compatibility
      "validation_outcome": "pass",     # pass | fail | incomplete — the real axis
      "testpypi_version": "2026.6.30.1.dev64501",
      "profile": "release",             # env profile the integration tier ran under
      "commit_shas": {                  # per-repo HEAD the rehearsal was built from
        "PyAutoNerves": "abc123...", "PyAutoFit": "...", "PyAutoArray": "...",
        "PyAutoGalaxy": "...", "PyAutoLens": "..."
      },
      "stages": {                       # per-stage status (pass|fail|skip)
        "unit":      {"status": "pass", "run_url": "..."},
        "rehearse":  {"status": "pass", "index": "testpypi", "version": "...",
                      "run_id": "645", "build_sha": "...", "packages": [...]},
        "integrate": {"status": "pass", "profile": "release", "run_url": "..."}
      },
      "totals": {"passed": N, "failed": N, "skipped": N, "timeout": N},
      "per_project": {                  # per-workspace pass/fail/skip/timeout
        "autolens_workspace":      {"passed": .., "failed": .., ...},
        "autolens_workspace_test": {"passed": .., "failed": .., ...}
      },
      "failures": [                     # failing entries, with logs / run URLs
        {"project": "...", "script": "...", "log_url": "..."}
      ],
      "run_urls": {"rehearse": "...", "integrate": "..."},
      "ts": "2026-06-30T12:00:00+00:00"
    }

``validation_outcome`` is the **pass/fail/incomplete** axis, and it is the one to
read:

- ``fail`` — something adverse was ingested: a stage reported ``fail``;
  ``failed``/``timeout`` is positive in ``totals`` **or in any ``per_project``
  entry**; ``failures`` is non-empty; the caller passed ``force_fail`` (e.g. the
  producing run's own conclusion was not ``success``, whatever its artifact
  claims); a merged base report said so explicitly; or the discriminator is
  malformed or contradicts the ``release_ready`` beside it.
- ``incomplete`` — nothing adverse and nothing contradictory, but the report
  cannot attest that everything ran: the ``rehearse`` evidence is absent, or a
  stage ran without passing (``skip`` — which is also where ``_norm_status``
  puts any token it does not recognise, so an unknown status can never read as a
  pass). This is an **evidence gap**, which the readiness gate grades STALE —
  *not* a failure.
- ``pass`` — no adverse evidence, every stage passed, and the rehearsal passed.

``release_ready`` is the older boolean, kept unchanged for compatibility. It
collapses ``fail`` and ``incomplete`` into a single ``false``, which is exactly
why it must not be used to decide RED: the tick's auto-ingest
(``heart/checks/release_run.py``) folds an **integrate-only** stage report, which
can never carry a ``rehearse`` stage, so it lands on ``false`` by construction
however green the run was. Consumers deciding severity read
``validation_outcome``; a report predating the field has no discriminator and is
treated as a failure (fail closed).

Release *fidelity* and *freshness* (``profile == release``, ``commit_shas``
matching the current ``main`` HEADs, age) are judged separately by the readiness
gate (``heart/readiness.py``) — a passing-but-stale or passing-but-wrong-profile
report is YELLOW there, not GREEN. Keeping the axes separate is what lets an M2
rehearsal-only report be faithfully ``pass`` yet still gate YELLOW until M3 wires
the release-fidelity integration.

Recognised input artifacts (files, or directories scanned for them):

- ``rehearsal.json`` / ``testpypi_version.txt`` — the M1 rehearsal artifact
  (``testpypi-rehearsal-version``). Its presence means all five wheels built,
  uploaded, and installed, so the ``rehearse`` stage is ``pass``.
- ``commit_shas.json`` — ``{repo: sha}`` (or ``{"commit_shas": {...}}``), the
  HEADs the Release Agent built from (it has the GitHub access to read them).
- a **stage report** — any JSON carrying a ``stage`` key (``unit`` /
  ``integrate`` from the ``release-integrate.yml`` channel): ``status``, ``profile``,
  ``summary``, ``per_project``, ``failures``, ``run_url``, ``commit_shas``.
- a full ``validation_report.json`` — merged as a base (idempotent re-ingest).
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from heart import freeze, state

SCHEMA_VERSION = 1

VALIDATION_REPORT_FILE = state.HEART_STATE_DIR / "validation_report.json"
VALIDATION_HISTORY_DIR = state.HEART_STATE_DIR / "validation_history"
# The install-verification sidecar readiness reads (see heart/state.py's
# snapshot). Written either by `verify_install --report-json` directly (a local
# run) or by `--ingest` folding the block out of a Stage 3 artifact.
VERIFY_INSTALL_FILE = state.HEART_STATE_DIR / "verify_install.json"

_COUNT_KEYS = ("passed", "failed", "skipped", "timeout")


def _now_iso(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.isoformat()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _counts_adverse(counts: Any) -> bool:
    """True if a counts block records failures or timeouts."""
    return bool(
        isinstance(counts, dict) and (counts.get("failed", 0) or counts.get("timeout", 0))
    )


def _parse_iso(value: Any) -> datetime.datetime | None:
    """Parse an ISO timestamp, or None. Used to order merged base reports."""
    try:
        t = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=datetime.timezone.utc) if t.tzinfo is None else t


def _norm_status(value: Any) -> str:
    """Map a stage/status token onto pass|fail|skip (unknown → ``skip``)."""
    s = str(value or "").strip().lower()
    if s in ("pass", "passed", "success", "succeeded", "ok", "green", "true"):
        return "pass"
    if s in ("fail", "failed", "failure", "timed_out", "timeout", "error", "red", "false"):
        return "fail"
    if s in ("skip", "skipped", "neutral", "cancelled", "canceled"):
        return "skip"
    return "skip"


def _iter_source_files(sources: Iterable[str | Path]) -> list[Path]:
    """Expand each source (file or dir) into concrete artifact file paths.

    Directories are scanned (one level, then recursively) for the known JSON
    filenames plus ``*version*.txt``; explicit file paths are used verbatim.
    Order is preserved and de-duplicated so a later, more-specific artifact can
    override an earlier one deterministically.
    """
    seen: set[Path] = set()
    out: list[Path] = []

    def _add(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)

    for src in sources:
        p = Path(src)
        if p.is_dir():
            # Scan every JSON (unknown kinds are ignored by _classify) plus any
            # version text file, so a directory of downloaded artifacts is picked
            # up whatever the stage report is named (rehearsal.json,
            # integrate.json, report.json, commit_shas.json, ...).
            for hit in sorted(p.rglob("*.json")):
                _add(hit)
            for txt in sorted(p.rglob("*version*.txt")):
                _add(txt)
        else:
            _add(p)
    return out


def _classify(name: str, data: Any) -> str:
    """Return the artifact kind for a loaded JSON body / filename."""
    if not isinstance(data, dict):
        return "unknown"
    if "release_ready" in data and "stages" in data:
        return "report"
    if data.get("mode") == "rehearsal" or data.get("index") == "testpypi":
        return "rehearsal"
    if "packages" in data and "version" in data:
        return "rehearsal"
    if "stage" in data:
        return "stage"
    if "commit_shas" in data:
        return "commit_shas"
    if name == "commit_shas.json":
        return "commit_shas"
    return "unknown"


class _Accumulator:
    """Mutable fold state while merging artifacts, distilled into a report."""

    def __init__(self) -> None:
        self.testpypi_version: str | None = None
        self.profile: str | None = None
        self.commit_shas: dict[str, str] = {}
        self.stages: dict[str, dict[str, Any]] = {}
        self.totals: dict[str, int] = {k: 0 for k in _COUNT_KEYS}
        self.per_project: dict[str, dict[str, int]] = {}
        self.failures: list[dict[str, Any]] = []
        self.run_urls: dict[str, str] = {}
        # The verify_install sidecar a stage artifact carried, if any. Kept out
        # of validation_report.json (it is not part of that schema) — `run()`
        # persists it to the separate sidecar path readiness reads.
        self.verify_install: dict[str, Any] | None = None
        self._explicit_ready: bool | None = None
        self._explicit_outcome: str | None = None
        self._outcome_invalid = False
        self._force_fail = False
        self._base_seen = False
        self._base_ts: datetime.datetime | None = None
        self._base_adverse = False
        # True once a real stage artifact (add_stage) has contributed counts.
        # add_report() consults this so merging an old validation_report.json
        # as a "base" never double-counts totals/per_project/failures that a
        # freshly-ingested stage artifact for the same run already supplied —
        # see add_report()'s docstring.
        self._stage_counts_seen: bool = False

    def _add_counts(self, target: dict[str, int], summary: dict[str, Any]) -> None:
        for k in _COUNT_KEYS:
            v = summary.get(k)
            if isinstance(v, (int, float)):
                target[k] += int(v)

    def _merge_per_project(self, per_project: dict[str, Any]) -> None:
        for proj, counts in (per_project or {}).items():
            if not isinstance(counts, dict):
                continue
            bucket = self.per_project.setdefault(proj, {k: 0 for k in _COUNT_KEYS})
            for k in _COUNT_KEYS:
                v = counts.get(k)
                if isinstance(v, (int, float)):
                    bucket[k] += int(v)

    def add_rehearsal(self, data: dict[str, Any]) -> None:
        version = data.get("version")
        if version and not self.testpypi_version:
            self.testpypi_version = str(version)
        stage = {
            "status": "pass",  # artifact presence == all 5 wheels built/installed
            "index": data.get("index", "testpypi"),
            "version": str(version) if version else self.testpypi_version,
        }
        for key in ("run_id", "run_attempt", "build_sha", "packages"):
            if data.get(key) is not None:
                stage[key] = data[key]
        self.stages["rehearse"] = stage
        if data.get("build_sha"):
            self.commit_shas.setdefault("PyAutoHands", str(data["build_sha"]))

    def add_stage(self, data: dict[str, Any]) -> None:
        self._stage_counts_seen = True
        name = str(data.get("stage") or "").strip() or "stage"
        entry: dict[str, Any] = {"status": _norm_status(data.get("status"))}
        if data.get("profile"):
            entry["profile"] = str(data["profile"])
            self.profile = str(data["profile"])
        if data.get("run_url"):
            entry["run_url"] = str(data["run_url"])
            self.run_urls[name] = str(data["run_url"])
        if data.get("version") and not self.testpypi_version:
            self.testpypi_version = str(data["version"])
        self.stages[name] = entry

        summary = data.get("summary")
        if isinstance(summary, dict):
            self._add_counts(self.totals, summary)
        self._merge_per_project(data.get("per_project", {}) or {})
        for f in data.get("failures", []) or []:
            if isinstance(f, dict):
                self.failures.append(f)
        self.add_commit_shas(data.get("commit_shas"))
        self.add_verify_install(data.get("verify_install"))

    def add_verify_install(self, sidecar: Any) -> None:
        """Keep the newest verify_install block seen across ingested artifacts.

        Newest-by-``ts`` so that re-ingesting an older artifact alongside a newer
        one cannot walk the readiness leg backwards. A block without a usable
        ``ts`` only seeds an empty slot; it never displaces a timestamped one.
        """
        vi = normalize_verify_install(sidecar)
        if vi is None:
            return
        if self.verify_install is None:
            self.verify_install = vi
            return
        new_ts, cur_ts = vi.get("ts"), self.verify_install.get("ts")
        if new_ts and (not cur_ts or str(new_ts) > str(cur_ts)):
            self.verify_install = vi

    def add_commit_shas(self, shas: Any) -> None:
        if not isinstance(shas, dict):
            return
        for repo, sha in shas.items():
            if sha:
                self.commit_shas[str(repo)] = str(sha)

    def add_report(self, data: dict[str, Any]) -> None:
        """Merge a previously-emitted full report as a base.

        Only a *seed* for whatever a fresh stage artifact in the same
        ``--ingest`` call hasn't already supplied: ``totals`` / ``per_project`` /
        ``failures`` are folded in ONLY when no ``add_stage`` call has
        contributed counts yet (``ingest`` also guarantees any "report" kind
        artifact is processed after every "stage"/"rehearsal" one, so this is
        order-independent). Otherwise counts would double — e.g. pointing
        ``--ingest`` at a directory containing both a prior
        ``validation_report.json`` AND the raw ``integrate.json`` that produced
        it — which would contradict the "idempotent re-ingest" claim below.
        When a fresh stage artifact HAS contributed counts, the base is
        subordinate in full — its stages and its verdict are skipped along with
        its counts, and only the scalar fields (version/profile/commit_shas/
        run_urls) seed through. Merging its stages anyway let a stale
        ``rehearse: pass`` from a superseded attempt combine with a fresh
        integrate-only artifact and read as complete evidence.
        """
        if data.get("testpypi_version") and not self.testpypi_version:
            self.testpypi_version = str(data["testpypi_version"])
        if data.get("profile") and not self.profile:
            self.profile = str(data["profile"])
        self.add_commit_shas(data.get("commit_shas"))
        base_stages = data.get("stages")
        base_stages = base_stages if isinstance(base_stages, dict) else {}
        for name, entry in ({} if self._stage_counts_seen else base_stages).items():
            if isinstance(entry, dict) and name not in self.stages:
                merged = dict(entry)
                # Normalise exactly as add_stage does. Without this a merged
                # report carrying a synonym ("failure", "timed_out") would keep
                # a status that is not literally "fail", and every downstream
                # "did a stage fail?" test would read it as not-a-failure.
                merged["status"] = _norm_status(entry.get("status"))
                self.stages[name] = merged
        if not self._stage_counts_seen:
            if isinstance(data.get("totals"), dict):
                self._add_counts(self.totals, data["totals"])
            self._merge_per_project(data.get("per_project", {}) or {})
            for f in data.get("failures", []) or []:
                if isinstance(f, dict):
                    self.failures.append(f)
        for k, v in (data.get("run_urls") or {}).items():
            self.run_urls.setdefault(str(k), str(v))
        # --- the base's own verdict --------------------------------------
        #
        # A base report is a SEED, not evidence. Three rules, in order:
        #
        # 1. If this ingest also folded first-hand stage artifacts, THEY are the
        #    evidence and the base's verdict is ignored outright — the same rule
        #    its counts already follow just above. Without this, a stale failed
        #    report left in a re-used artifacts directory permanently poisons
        #    every later attempt that writes into the same directory.
        # 2. Otherwise the NEWEST base wins, by `ts`. Ingest walks a directory,
        #    so which base is "last" is an accident of filename order; recency
        #    is the only defensible ordering, and it makes the result
        #    order-independent.
        # 3. On an equal or unparseable `ts` we cannot tell which came first, so
        #    a base may only ESCALATE to adverse, never soften. File order on
        #    disk must not be able to clear a recorded failure.
        has_outcome_key = "validation_outcome" in data
        outcome = data.get("validation_outcome") if has_outcome_key else None
        valid_outcome = outcome if outcome in ("pass", "fail", "incomplete") else None
        if has_outcome_key and valid_outcome is None:
            # Present but not a value we recognise — `null` included, which is
            # why this tests key PRESENCE rather than the value being non-None.
            # A malformed gate artifact must never read as a pass. Sticky.
            self._outcome_invalid = True
        ready = data["release_ready"] if isinstance(data.get("release_ready"), bool) else None
        # `false` alone is adverse only when we cannot see why; paired with an
        # explicit `incomplete` it just means "evidence missing", which is benign
        # and must not be sticky.
        adverse = valid_outcome == "fail" or (ready is False and not has_outcome_key)

        if self._stage_counts_seen:
            return

        ts = _parse_iso(data.get("ts"))
        strictly_newer = ts is not None and (self._base_ts is None or ts > self._base_ts)
        strictly_older = (
            ts is not None and self._base_ts is not None and ts < self._base_ts
        )
        if self._base_seen and strictly_older:
            # Superseded outright. Escalation is for the case where we CANNOT
            # order the two, not for one we can order and know to be older.
            return
        if self._base_seen and not strictly_newer:
            if adverse:
                self._base_adverse = True
                if valid_outcome == "fail":
                    self._explicit_outcome = "fail"
                if ready is False:
                    self._explicit_ready = False
            return

        self._base_seen = True
        if ts is not None:
            self._base_ts = ts
        self._explicit_outcome = valid_outcome
        self._explicit_ready = ready
        self._base_adverse = adverse

    _counts_adverse = staticmethod(_counts_adverse)

    def _has_adverse_evidence(self) -> bool:
        """True if anything ingested is actually *bad* (not merely missing).

        Deliberately wider than "a stage said fail". ``release_ready`` never
        consulted the counts at all, so an artifact claiming ``status: pass``
        while carrying failing ones slipped through the stage test — and an
        unrecognised status token normalises to ``skip``, never ``fail``.
        Per-project counts are checked too: they are merged independently of
        ``totals``, so a report can carry a failing project while its top-level
        totals read clean. Anything adverse here forces ``fail``.
        """
        if any(s.get("status") == "fail" for s in self.stages.values()):
            return True
        if self._counts_adverse(self.totals):
            return True
        if any(self._counts_adverse(c) for c in self.per_project.values()):
            return True
        return bool(self.failures)

    def validation_outcome(self) -> str:
        """``pass`` | ``fail`` | ``incomplete`` — the two axes, separated.

        ``release_ready`` is a single boolean that has to answer two different
        questions ("did anything fail?" and "is the evidence complete?"), so a
        report with nothing built is indistinguishable from a report where
        something broke. This is the discriminator; ``release_ready`` is kept
        beside it, unchanged, for compatibility.

        Fails closed at every step. ``incomplete`` is reserved for the single
        benign case: nothing adverse, nothing contradictory, and the rehearsal
        evidence is simply absent.
        """
        if self._force_fail or self._has_adverse_evidence():
            return "fail"
        # A malformed discriminator is untrustworthy evidence, not a gap.
        if self._outcome_invalid:
            return "fail"
        # An explicit `fail` from a merged base outranks everything below: a
        # base saying "this failed" must never be softened by what the stages
        # look like now.
        if self._base_adverse or self._explicit_outcome == "fail":
            return "fail"
        if self._explicit_outcome == "pass" and self._explicit_ready is False:
            # A base whose two fields contradict each other. Fails closed.
            return "fail"
        # Everything else is decided by the evidence actually folded in, NOT by
        # the base's explicit `pass`/`incomplete`. A base's stale `incomplete`
        # must not survive a rehearsal supplied in the same ingest, and its
        # `pass` must not stand in for rehearsal evidence that isn't here.
        #
        # A stage that RAN and did not pass is not evidence of passing. `skip`
        # covers both a deliberately skipped stage and any status token
        # `_norm_status` did not recognise, so neither can read as a pass.
        if any(s.get("status") != "pass" for s in self.stages.values()):
            return "incomplete"
        rehearse = self.stages.get("rehearse")
        if rehearse and rehearse.get("status") == "pass":
            return "pass"
        if self._explicit_outcome is None and self._explicit_ready is True:
            # Legacy compatibility: a report predating `validation_outcome`
            # states only `release_ready: true`, and the schema's idempotent
            # re-ingest promise means folding it back must not silently demote
            # it. A report that DOES carry the discriminator gets no such
            # benefit — an explicit `pass` never substitutes for the rehearsal
            # evidence it claims.
            return "pass"
        return "incomplete"

    def release_ready(self) -> bool:
        """The legacy boolean, now DERIVED from :meth:`validation_outcome`.

        It used to be computed independently, and the two could then disagree
        inside a single emitted report — an incomplete base upgraded by a fresh
        rehearsal produced ``release_ready: false`` beside
        ``validation_outcome: "pass"``, which every consumer then normalised
        back to ``fail``, manufacturing a RED out of a passing ingest. One
        source of truth removes that class of contradiction at the producer,
        so the reconciliation in :func:`report_outcome` only ever has to cope
        with hand-edited or foreign reports.

        It still cannot distinguish "failed" from "incomplete" — that is what
        ``validation_outcome`` is for.
        """
        return self.validation_outcome() == "pass"


def report_outcome(report: Any) -> str | None:
    """Severity of a *persisted* report: ``pass``/``fail``/``incomplete``/None.

    The single normaliser every consumer must use — the readiness gate, the
    dashboard, the ``validate`` CLI summary and the tick's status line. Each of
    them previously re-derived this inline, which is how they came to disagree:
    readiness rejected a malformed discriminator while the dashboard beside it
    still rendered a green ``release_ready`` row for the same report.

    Fails closed. ``None`` means "no report / nothing stated" — the caller
    decides what an absent verdict means in its own context.
    """
    if not isinstance(report, dict) or not report:
        return None
    outcome = report.get("validation_outcome")
    ready = report.get("release_ready")
    # A declaration never outranks the evidence beside it. Legacy reports carry
    # only `release_ready`, so a stale `true` sitting next to a failed stage
    # used to reach GREEN on the strength of the boolean alone.
    stages = report.get("stages")
    per_project = report.get("per_project")
    if (
        any(
            isinstance(v, dict) and _norm_status(v.get("status")) == "fail"
            for v in (stages if isinstance(stages, dict) else {}).values()
        )
        or _counts_adverse(report.get("totals"))
        or any(
            _counts_adverse(c)
            for c in (per_project if isinstance(per_project, dict) else {}).values()
        )
        or bool(report.get("failures"))
    ):
        return "fail"
    if outcome in ("pass", "fail", "incomplete"):
        # The two fields contradict each other; believe the pessimistic one.
        if outcome == "pass" and ready is False:
            return "fail"
        return str(outcome)
    if "validation_outcome" in report:
        # Present but unrecognised: malformed, never a pass.
        return "fail"
    if ready is True:
        return "pass"
    if ready is False:
        return "fail"
    return None


def _fold(
    sources: Sequence[str | Path],
    *,
    profile: str | None = None,
    testpypi_version: str | None = None,
    commit_shas: dict[str, str] | None = None,
    force_fail: bool = False,
) -> _Accumulator:
    """Fold the given artifacts into an accumulator (reads only; no writes).

    Split out of ``ingest`` so ``run`` can reach the fold state itself — the
    ``verify_install`` sidecar a stage artifact carries is deliberately NOT part
    of the ``validation_report`` schema, so ``ingest``'s return value cannot
    carry it and ``run`` persists it from here instead.
    """
    acc = _Accumulator()
    acc._force_fail = bool(force_fail)
    if commit_shas:
        acc.add_commit_shas(commit_shas)

    # "report" kind artifacts (a previously-emitted validation_report.json
    # used as a merge base) are deferred to a second pass, processed strictly
    # after every "rehearsal"/"stage"/"commit_shas" artifact — regardless of
    # the sources' original order — so add_report()'s count-dedup against
    # _stage_counts_seen can never depend on file-naming/glob-sort luck.
    deferred_reports: list[dict[str, Any]] = []

    for path in _iter_source_files(sources):
        if path.suffix == ".txt":
            if "version" in path.name.lower() and not acc.testpypi_version:
                txt = None
                try:
                    txt = path.read_text().strip()
                except OSError:
                    txt = None
                if txt:
                    acc.testpypi_version = txt.splitlines()[0].strip()
            continue
        data = _read_json(path)
        kind = _classify(path.name, data)
        if kind == "rehearsal":
            acc.add_rehearsal(data)
        elif kind == "stage":
            acc.add_stage(data)
        elif kind == "commit_shas":
            acc.add_commit_shas(data.get("commit_shas") if "commit_shas" in data else data)
        elif kind == "report":
            deferred_reports.append(data)
        # unknown → ignored

    for data in deferred_reports:
        acc.add_report(data)

    # Explicit overrides take precedence (Release-Agent-supplied truth).
    if testpypi_version:
        acc.testpypi_version = testpypi_version
    if profile:
        acc.profile = profile

    return acc


def _distill(acc: _Accumulator, now: datetime.datetime | None = None) -> dict[str, Any]:
    """Render an accumulator as a ``validation_report`` dict."""
    return {
        "schema_version": SCHEMA_VERSION,
        "release_ready": acc.release_ready(),
        "validation_outcome": acc.validation_outcome(),
        "testpypi_version": acc.testpypi_version,
        "profile": acc.profile,
        "commit_shas": dict(sorted(acc.commit_shas.items())),
        "stages": acc.stages,
        "totals": acc.totals,
        "per_project": acc.per_project,
        "failures": acc.failures,
        "run_urls": acc.run_urls,
        "ts": _now_iso(now),
    }


def ingest(
    sources: Sequence[str | Path],
    *,
    profile: str | None = None,
    testpypi_version: str | None = None,
    commit_shas: dict[str, str] | None = None,
    now: datetime.datetime | None = None,
    force_fail: bool = False,
) -> dict[str, Any]:
    """Fold the given artifacts into a single ``validation_report`` dict.

    Pure (no I/O side effects beyond reading the source files); ``run`` persists
    the result. Explicit ``profile`` / ``testpypi_version`` / ``commit_shas``
    override / seed whatever the artifacts carry — the Release Agent uses these
    to inject the HEADs it built from.
    """
    return _distill(
        _fold(
            sources,
            profile=profile,
            testpypi_version=testpypi_version,
            commit_shas=commit_shas,
            force_fail=force_fail,
        ),
        now=now,
    )


def normalize_verify_install(sidecar: Any) -> dict[str, Any] | None:
    """Normalize a ``verify_install.json`` sidecar into the block we carry/persist.

    Returns ``None`` for anything that isn't a usable sidecar, so a missing or
    malformed file leaves the readiness leg reporting "not run" — an absent
    result and a bad one must never read as a pass.

    ``index`` records which package index the wheels came from. A ``--testpypi``
    run proves the about-to-ship wheels install; it is not evidence about the
    current PyPI release. Sidecars written before this field existed carry no
    index, which stays ``None`` (unknown) rather than being guessed at. A
    ``find-links`` run is development-only evidence; preserving that provenance
    lets readiness keep a pass STALE instead of laundering it into release proof.
    """
    if not isinstance(sidecar, dict) or "ready" not in sidecar:
        return None
    checks: list[dict[str, Any]] = []
    for c in sidecar.get("checks") or []:
        if isinstance(c, dict):
            checks.append(
                {
                    "check": c.get("check"),
                    "status": c.get("status"),
                    "detail": c.get("detail"),
                }
            )
    index = sidecar.get("index")
    return {
        "ts": sidecar.get("ts"),
        "ready": sidecar.get("ready") is True,
        "version": sidecar.get("version"),
        "check_b_version": sidecar.get("check_b_version"),
        "index": str(index) if index else None,
        "checks": checks,
    }


def to_stage_report(
    aggregate: dict[str, Any],
    *,
    stage: str = "integrate",
    profile: str | None = None,
    version: str | None = None,
    commit_shas: dict[str, str] | None = None,
    run_url: str | None = None,
    extra_failures: Sequence[dict[str, Any]] | None = None,
    force_fail: bool = False,
    verify_install: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate a Build ``aggregate_results.py`` report.json into a stage report.

    This is the M3 bridge: the ``workspace-validation.yml`` body runs Build's
    ``aggregate_results.py`` (unmodified — Heart reuses the executor primitive,
    it does not reimplement it) to get a ``{ready, summary, per_project,
    failures, ...}`` blob keyed by Build's own vocabulary (``file`` not
    ``script``, no top-level ``stage``/``profile``). This function reshapes that
    into the ``{"stage": ..., "status": "pass"|"fail", ...}`` contract
    ``_Accumulator.add_stage`` (and the spec in the module docstring) expect, so
    it can be ingested by ``run``/``ingest`` unmodified. Pure — no I/O.

    ``force_fail`` lets the caller fold a result Build's aggregate step knows
    nothing about (e.g. ``verify_install`` A-F against the same wheels) into the
    stage's pass/fail axis without inventing a second stage.

    ``verify_install`` carries that same sidecar through as *evidence* rather
    than only as a veto. Before this existed the sidecar was consulted solely in
    the failure direction, so a **passing** run contributed nothing and the
    readiness leg it feeds reported "install verification not run" forever — the
    check ran in CI and its result was discarded. ``--ingest`` persists this
    block to the sidecar path ``heart.readiness`` reads.
    """
    summary_raw = aggregate.get("summary")
    summary_raw = summary_raw if isinstance(summary_raw, dict) else {}
    summary = {k: int(summary_raw.get(k, 0) or 0) for k in _COUNT_KEYS}

    per_project_raw = aggregate.get("per_project")
    per_project_raw = per_project_raw if isinstance(per_project_raw, dict) else {}
    per_project: dict[str, dict[str, int]] = {}
    for proj, counts in per_project_raw.items():
        if not isinstance(counts, dict):
            continue
        per_project[str(proj)] = {k: int(counts.get(k, 0) or 0) for k in _COUNT_KEYS}

    failures_raw = aggregate.get("failures")
    failures_raw = failures_raw if isinstance(failures_raw, list) else []
    failures: list[dict[str, Any]] = []
    for f in failures_raw:
        if not isinstance(f, dict):
            continue
        entry: dict[str, Any] = {"project": f.get("project"), "script": f.get("file")}
        if run_url:
            entry["log_url"] = run_url
        failures.append(entry)
    for f in extra_failures or []:
        if isinstance(f, dict):
            failures.append(dict(f))

    # Strict boolean check (not truthiness): this is a CI contract, so a
    # malformed/non-boolean "ready" (e.g. a stray string "false", which is
    # truthy in Python) must never be read as a pass.
    status = "pass" if (aggregate.get("ready") is True and not force_fail) else "fail"

    report: dict[str, Any] = {"stage": stage, "status": status}
    if profile:
        report["profile"] = profile
    if version:
        report["version"] = version
    if run_url:
        report["run_url"] = run_url
    if commit_shas:
        report["commit_shas"] = dict(commit_shas)
    report["summary"] = summary
    report["per_project"] = per_project
    report["failures"] = failures
    vi = normalize_verify_install(verify_install)
    if vi is not None:
        report["verify_install"] = vi
    return report


def _archive_name(report: dict[str, Any]) -> str:
    """A stable, sortable history filename for one ingested report."""
    ver = str(report.get("testpypi_version") or "unknown").replace("/", "_")
    ts = str(report.get("ts") or _now_iso()).replace(":", "").replace("/", "_")
    return f"{ts}__{ver}.json"


def run(
    sources: Sequence[str | Path],
    *,
    profile: str | None = None,
    testpypi_version: str | None = None,
    commit_shas: dict[str, str] | None = None,
    out: Path | None = None,
    now: datetime.datetime | None = None,
    force_fail: bool = False,
) -> dict[str, Any]:
    """Ingest, persist ``validation_report.json`` + a history copy, and return it.

    Persistence stays entirely inside ``~/.pyauto-heart/`` — the canonical report
    plus an append-only ``validation_history/`` archive so Heart tracks release
    health over time without ever mutating a source repo.

    Also persists any ``verify_install`` block an ingested stage artifact carried
    to ``VERIFY_INSTALL_FILE``, which is what actually feeds the readiness leg
    (``heart/state.py`` reads that path into the snapshot). Stage 3 has always
    run the check; before this it had nowhere to land, so the leg reported
    "install verification not run" no matter how many times it passed.
    """
    acc = _fold(
        sources,
        profile=profile,
        testpypi_version=testpypi_version,
        commit_shas=commit_shas,
        force_fail=force_fail,
    )
    report = _distill(acc, now=now)
    target = out or VALIDATION_REPORT_FILE
    state.atomic_write_json(target, report)
    try:
        state.atomic_write_json(VALIDATION_HISTORY_DIR / _archive_name(report), report)
    except OSError:
        pass  # history is best-effort; the canonical report is what matters
    # `out` redirects the report for inspection (tests, dry runs); the sidecar is
    # a live readiness input, so it is only written on a real ingest.
    if acc.verify_install is not None and out is None:
        state.atomic_write_json(VERIFY_INSTALL_FILE, acc.verify_install)
    return report


def load() -> dict[str, Any] | None:
    """Return the persisted ``validation_report.json`` (or None)."""
    return _read_json(VALIDATION_REPORT_FILE)


def _print_summary(report: dict[str, Any]) -> None:
    from heart.heart_color import (
        c_fail, c_info, c_meta, c_ok, c_warn, glyph_fail, glyph_ok, glyph_warn,
    )

    # Report the tri-state, not the legacy boolean: the two can legitimately
    # disagree (a stage saying pass while carrying failing counts is
    # `release_ready: true` but `validation_outcome: "fail"`), and printing the
    # boolean alone rendered a green tick over a failing report.
    outcome = report_outcome(report)
    if outcome == "pass":
        glyph, label = glyph_ok(), c_ok("release_ready")
    elif outcome == "fail":
        glyph, label = glyph_fail(), c_fail("NOT release_ready (validation FAILED)")
    elif outcome == "incomplete":
        glyph, label = glyph_warn(), c_warn("incomplete — no rehearsal evidence")
    else:
        glyph, label = glyph_warn(), c_warn("release_ready unknown")
    t = report.get("totals", {}) or {}
    _stages = report.get("stages")
    stages = ", ".join(
        f"{n}:{s.get('status', '?') if isinstance(s, dict) else '?'}"
        for n, s in (_stages if isinstance(_stages, dict) else {}).items()
    )
    version = report.get("testpypi_version") or "?"
    prof = report.get("profile") or "?"
    print(f"{glyph} {c_info('validate')} {label} {c_meta(f'v{version}  profile={prof}')}")
    print(
        c_meta(
            f"  stages: {stages or 'none'}  "
            f"totals: {t.get('passed', 0)}p/{t.get('failed', 0)}f/"
            f"{t.get('skipped', 0)}s/{t.get('timeout', 0)}t"
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pyauto-heart validate",
        description="Ingest release-validation artifacts into validation_report.json "
        "(ingest-and-judge only — never dispatches a build).",
    )
    ap.add_argument(
        "--ingest", nargs="+", metavar="PATH", default=None,
        help="artifact files/directories to ingest (rehearsal.json, commit_shas.json, stage report.json, ...)",
    )
    ap.add_argument(
        "--emit-stage-report", default=None, metavar="AGGREGATE_JSON",
        help="reshape a Build aggregate_results.py report.json into a stage report "
        "(writes it to --out and exits; does NOT ingest/persist validation_report.json)",
    )
    ap.add_argument("--stage", default="integrate", help="stage name for --emit-stage-report (default: integrate)")
    ap.add_argument(
        "--verify-install", default=None, metavar="FILE",
        help="verify_install.json sidecar; carried into the stage report as evidence "
        "(--ingest persists it for the readiness leg), and ready==false additionally "
        "forces --emit-stage-report to fail",
    )
    ap.add_argument("--run-url", default=None, help="CI run URL attached to the stage / its failures")
    ap.add_argument("--profile", default=None, help="override the env profile the integration tier ran under")
    ap.add_argument("--testpypi-version", default=None, help="override the rehearsed TestPyPI version")
    ap.add_argument("--commit-shas", default=None, metavar="FILE", help="JSON file of {repo: sha} HEADs built from")
    ap.add_argument("--out", default=None, help="write the report here instead of the default state path")
    ap.add_argument("--json", action="store_true", help="print the resulting report as JSON")
    ns = ap.parse_args(argv)

    commit_shas: dict[str, str] | None = None
    if ns.commit_shas:
        data = _read_json(Path(ns.commit_shas))
        if isinstance(data, dict):
            commit_shas = data.get("commit_shas") if "commit_shas" in data else data

    if ns.emit_stage_report is not None:
        aggregate = _read_json(Path(ns.emit_stage_report)) or {}
        force_fail = False
        extra_failures: list[dict[str, Any]] = []
        vi: Any = None
        if ns.verify_install:
            vi = _read_json(Path(ns.verify_install))
            if isinstance(vi, dict) and vi.get("ready") is False:
                force_fail = True
                extra_failures.append({
                    "project": None, "script": "verify_install",
                    "log_url": ns.run_url, "reason": "verify_install FAILED",
                })
        stage_report = to_stage_report(
            aggregate,
            stage=ns.stage,
            profile=ns.profile,
            version=ns.testpypi_version,
            commit_shas=commit_shas,
            run_url=ns.run_url,
            extra_failures=extra_failures,
            force_fail=force_fail,
            verify_install=vi,
        )
        out_path = Path(ns.out) if ns.out else Path("stage_report.json")
        out_path.write_text(json.dumps(stage_report, indent=2, sort_keys=True))
        if ns.json:
            json.dump(stage_report, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        else:
            print(
                f"stage report written to {out_path} "
                f"(stage={stage_report['stage']} status={stage_report['status']})"
            )
        return 0 if stage_report["status"] == "pass" else 1

    if ns.ingest is None:
        report = load()
        if report is None:
            print("validate: no validation_report.json yet (run with --ingest <artifacts>)", file=sys.stderr)
            return 1
    else:
        report = run(
            ns.ingest,
            profile=ns.profile,
            testpypi_version=ns.testpypi_version,
            commit_shas=commit_shas,
            out=Path(ns.out) if ns.out else None,
        )
        # The ingest of the validation evidence IS the end of the validation
        # window, so it is where the freeze flag comes off — not a separate
        # thing to remember. Only on a real ingest: `--out` redirects the
        # report for inspection and must not touch live state, exactly as the
        # verify_install sidecar above. Never on the read-only path.
        if ns.out is None:
            was = freeze.clear()
            if was["state"] != freeze.CLEAR:
                print(f"freeze cleared: {was['reason']} "
                      f"(set {was['set_at']} by {was['set_by'] or 'unknown'})")

    if ns.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        _print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
