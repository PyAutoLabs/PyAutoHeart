"""heart/checks/unit_timings.py — per-TEST and per-IMPORT timings from the
libraries' own CI (the unit half of the speed surface).

``smoke_timings`` ingests what the workspace gates measure; this is its twin for
the LIBRARIES. Their required gate is the reusable ``lib-tests.yml``, which since
#206 writes two things per python leg and uploads them as ``unit-timings-<py>``:

* ``junit.xml`` — pytest's own machine-readable per-test record (``--junitxml``),
  one ``<testcase>`` per test with ``time`` = setup+call+teardown. A console
  ``--durations`` tail would have to be scraped out of a log that expires;
* ``import_time.json`` — a FRESH-process ``import <pkg>``, timed on the runner.

Both surfaces already had a dev-box check (``unit_test_timing.py``,
``import_time.py``) whose numbers only ever existed on one developer's laptop.
This check ingests the CI dataset instead and writes *those same two summary
files*, so the board's "Unit-test timing" and "Import timing" rows come alive
without either section changing its shape (see ``legacy_summaries``).

Two modes, same module (the smoke_timings shape):

* **per-repo** (``--name/--group/--owner``) — the bash entry point hands us one
  repo's artifacts listing plus a directory of already-extracted artifacts, and
  we write ``$HEART_PER_REPO_DIR/<name>.unit_timings.json``.
* **aggregate** (``--aggregate``) — read every sidecar and write the global
  rollup at ``$HEART_STATE_DIR/unit_timings.json``, plus the two legacy
  summaries the existing board sections read.

There is also a **--plan** mode: the shell leg needs to know *which* artifact
ids to download before it can download them, and the selection rule lives here
in Python rather than being re-derived in `jq`.

Deliberate choices, each one a recorded lesson:

* **One selector, two callers.** ``smoke_timings.select_artifacts`` takes the
  name pattern as an argument and this module passes ``ARTIFACT_RE``. The rule
  (newest, non-expired, one per named leg) is the same rule; a second copy of it
  here would be a second thing to keep true.
* **The committed record is the ONLY baseline.** ``smoke_timings`` falls back to
  the previously published ``board.json``; this check does not, because the
  board never carried unit rows and never will carry them before the record
  does. ``timings/unit/<repo>.jsonl`` (see ``heart/timings.py``) is durable and
  keyed by ``(python, run_id)``; with no record every test row is ``ok`` and
  every import row is ``building``, which is the truth on day one.
* **Drift needs BOTH gates**, exactly as in ci_timing/smoke_timings, and by
  literally the same function (``classify_test_drift`` IS
  ``smoke_timings.classify_drift``). Unit tests run 0.1–30 s, so the absolute
  floor in ``config/repos.yaml`` is 2 s rather than smoke's 5 s.
* **A run is never compared against itself.** Every row carries its ``run_id``;
  a previous row from the SAME run yields ``ok`` with no ratio rather than a
  reassuring 1.0×.
* **Imports are judged by the median window, not by the previous run.** That is
  the ``import_time`` doctrine, kept so a ratio means the same thing from either
  vantage: the baseline is ``median`` of the last ``import_window`` recorded
  observations and nothing is judged before ``import_min_samples`` of them
  (state ``building``).
* **``seconds: null`` is never zero.** An import that failed or timed out has no
  number; the returncode travels with it so the board can say why. Counting it
  as 0 s would put a fabricated instant import into a timing dataset.
* **Only the ``top_n`` slowest tests per leg are kept.** A 1500-test suite whole
  would multiply the sidecar, the rollup and the committed record for rows
  nothing renders; the suite totals keep the coverage visible beside them.
* **Drift is advisory.** Rows are ``ok`` or ``warn``, never ``fail``; the
  readiness verdict and the badge are untouched — this is a dashboard leg.
* **A failed fetch is not a quiet repo.** ``--fetch-error`` records the reason
  and writes NO legs; a leg whose download or parse failed keeps its provenance
  with its own ``error`` and no rows. "We could not ask" must never render as
  "all quiet", and one leg's 403 must never lose the other leg's data.

Per-repo sidecar schema (``<name>.unit_timings.json``)::

    {
      "name": "RepoA", "group": "libraries", "owner": "OwnerX",
      "error": "",                       # non-empty => the listing fetch failed
      "ts": "...",
      "legs": [
        {"python": "3.12", "artifact_id": 42, "run_id": 7,
         "run_url": "https://github.com/OwnerX/RepoA/actions/runs/7",
         "head_branch": "feat/x", "head_sha": "abc...",
         "at": "2026-09-01T10:00:00Z",   # the artifact's created_at
         "error": "",
         "suite": {"tests": 1500, "failures": 0, "errors": 0, "skipped": 3,
                   "wall_s": 412.0},
         "slowest": [{"nodeid": "tests/foo/test_bar.py::test_x",
                      "seconds": 12.5}],
         "import": {"package": "pkg_a", "seconds": 3.6, "returncode": 0}}
      ]
    }

Global rollup schema (``unit_timings.json``)::

    {"ts",
     "repos":   [{repo, python, run_id, run_url, head_branch, head_sha, at,
                  tests, wall_s, import_s, package, error, suite, slowest}],
     "tests":   [{repo, python, nodeid, seconds, run_id, run_url, prev_s,
                  prev_run_id, ratio, delta_s, state, prompt}],
     "imports": [{repo, package, python, seconds, run_id, run_url, baseline_s,
                  samples, ratio, state, prompt}],
     "slowed_tests": [...], "slowed_imports": [...],
     "errors": [{repo, error}], "thresholds": {...}}

``repos[]`` carries ``suite`` and ``slowest`` whole so ``heart.timings`` can
build the committed record from the rollup alone, without re-reading a sidecar.

Every actionable row — a slowed test, a slowed import — carries its own
ready-to-paste prompt string. The producer writes the prompt; the renderer
never re-derives it.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# Reused, never duplicated: the both-gates drift rule and the artifact selector
# are one implementation with two callers.
from heart.checks.import_time import classify as _classify_ratio
from heart.checks.smoke_timings import classify_drift as classify_test_drift
from heart.checks.smoke_timings import select_artifacts as _select_artifacts

HEART_HOME = Path(__file__).resolve().parents[2]
CONFIG_PATH = HEART_HOME / "config" / "repos.yaml"

# The libraries' Tests gate publishes one artifact per python leg. Nothing else
# in a library repo is named this way, and the workspaces' `smoke-timings-<py>`
# deliberately does not match: a unit suite and a smoke script are different
# sizes of thing measured on different cadences.
ARTIFACT_RE = re.compile(r"^unit-timings-(\d+\.\d+)$")

JUNIT_FILENAME = "junit.xml"
IMPORT_FILENAME = "import_time.json"
IMPORT_SCHEMA = "import_time/1"

NO_DATASET_ERROR = "no unit-timings dataset in artifact"

# Fallback when config/repos.yaml is unreadable; kept in sync with the YAML.
DEFAULT_UNIT_TIMINGS_THRESHOLDS = {
    "slow_factor": 2.0,
    "min_delta_s": 2,
    "top_n": 25,
    "import_yellow_factor": 1.5,
    "import_red_factor": 3.0,
    "import_window": 7,
    "import_min_samples": 3,
}

# The legacy "Unit-test timing" section says "(>3× baseline)" / "(>1.5×)" in its
# own summary wording, which it inherited from the dev-box check's rolling
# median. These two constants exist ONLY to keep that wording truthful when the
# section is fed from CI rows; the rows' own advisory `warn` state is the
# both-gates rule above, and nothing here changes it.
LEGACY_RED_FACTOR = 3.0
LEGACY_YELLOW_FACTOR = 1.5

# What the ingested summaries put in the legacy `python` field: these numbers
# were measured on a CI runner, not by a `HYGIENE_PYTHON` on someone's laptop.
LEGACY_PYTHON = "ci"


# --- config ------------------------------------------------------------------
def _load_config(config_path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    try:
        import yaml

        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def load_thresholds(config_path: Path | str = CONFIG_PATH) -> dict[str, float]:
    """Return the ``thresholds.unit_timings`` block, defaulted key by key."""
    out = dict(DEFAULT_UNIT_TIMINGS_THRESHOLDS)
    block = (_load_config(config_path).get("thresholds") or {}).get("unit_timings") or {}
    if isinstance(block, dict):
        for key in out:
            if isinstance(block.get(key), (int, float)) and not isinstance(
                block.get(key), bool
            ):
                out[key] = block[key]
    return out


# --- pure helpers ------------------------------------------------------------
def _as_float(value: Any) -> float | None:
    """A float, or None. ``bool`` is not a number here, and neither is junk."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    num = _as_float(value)
    return int(num) if num is not None else None


def select_artifacts(listing: Any) -> list[dict[str, Any]]:
    """The newest non-expired ``unit-timings-<py>`` artifact per python leg.

    A thin binding of ``smoke_timings.select_artifacts`` to this check's own
    name pattern — same rule, one implementation.
    """
    return _select_artifacts(listing, ARTIFACT_RE)


def junit_nodeid(classname: Any, name: Any) -> str:
    """pytest's ``path::[Class::]test`` from a junit ``classname``/``name`` pair.

    junit has no node ids: pytest writes the DOTTED import path of the module
    (plus the test class, when there is one) into ``classname`` and the test
    name into ``name``. Reconstructing the node id is therefore a heuristic, and
    it is kept deliberately simple — the first segment that starts with an
    uppercase letter is taken as the test class, everything before it as the
    module path:

        ``tests.foo.test_bar`` + ``test_x``           -> ``tests/foo/test_bar.py::test_x``
        ``tests.foo.test_bar.TestThing`` + ``test_x`` -> ``tests/foo/test_bar.py::TestThing::test_x``

    When there is no module path to rebuild (a bare ``TestThing``, a plugin that
    writes something else entirely) the dotted form is carried through verbatim
    as ``classname::name`` rather than guessed at: an unrecognisable id that is
    stable still compares correctly against itself run to run, which is all the
    drift rule needs.
    """
    classname = str(classname or "").strip()
    name = str(name or "").strip()
    if not classname:
        return name
    parts = [p for p in classname.split(".") if p]
    module: list[str] = []
    classes: list[str] = []
    for index, part in enumerate(parts):
        if part[:1].isupper():
            classes = parts[index:]
            break
        module.append(part)
    if not module:
        return f"{classname}::{name}" if name else classname
    segments = ["/".join(module) + ".py", *classes]
    if name:
        segments.append(name)
    return "::".join(segments)


def parse_junit(text: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """One ``junit.xml`` → ``(tests, suite, error)``.

    ``tests`` is ``[{"nodeid", "seconds"}]`` for every ``<testcase>`` carrying a
    numeric ``time``; a testcase without one was never measured and is not a
    zero-second test. ``suite`` sums the ``<testsuite>`` attributes — a run can
    emit more than one — into ``{tests, failures, errors, skipped, wall_s}``, so
    the coverage behind the durations stays visible beside them.

    Malformed XML degrades to ``([], {}, "<reason>")``: this file is written by
    another organ on a runner we do not control, and an unattended check must
    never raise over it.
    """
    if not isinstance(text, str) or not text.strip():
        return [], {}, f"{JUNIT_FILENAME} was empty"
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [], {}, f"{JUNIT_FILENAME} was not valid XML"

    suites = list(root.iter("testsuite"))
    if not suites:
        return [], {}, f"{JUNIT_FILENAME} carried no <testsuite>"

    suite: dict[str, Any] = {"tests": 0, "failures": 0, "errors": 0,
                             "skipped": 0, "wall_s": 0.0}
    for element in suites:
        for key in ("tests", "failures", "errors", "skipped"):
            suite[key] += _as_int(element.get(key)) or 0
        suite["wall_s"] += _as_float(element.get("time")) or 0.0
    suite["wall_s"] = round(suite["wall_s"], 3)

    tests: list[dict[str, Any]] = []
    for case in root.iter("testcase"):
        seconds = _as_float(case.get("time"))
        if seconds is None:
            continue
        nodeid = junit_nodeid(case.get("classname"), case.get("name"))
        if not nodeid:
            continue
        tests.append({"nodeid": nodeid, "seconds": round(seconds, 3)})
    return tests, suite, ""


def parse_import_time(text: str) -> tuple[dict[str, Any] | None, str]:
    """One ``import_time.json`` → (row, "") or (None, reason).

    Normalised to exactly the five keys this check stores, every type coerced
    defensively. ``seconds`` stays ``None`` when the import failed or timed out
    — never 0.0 — and ``returncode`` travels with it so the board can say why
    there is no number.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None, f"{IMPORT_FILENAME} was not valid JSON"
    if not isinstance(data, dict):
        return None, f"{IMPORT_FILENAME} was not an object"
    if str(data.get("schema") or "") != IMPORT_SCHEMA:
        return None, f"not an {IMPORT_SCHEMA} dataset"
    return (
        {
            "package": str(data.get("package") or ""),
            "python": str(data.get("python") or ""),
            "seconds": _as_float(data.get("seconds")),
            "returncode": _as_int(data.get("returncode")),
            "ts": str(data.get("ts") or ""),
        },
        "",
    )


def read_downloaded_leg(
    directory: Path | str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None, str, dict[str, str]]:
    """One extracted artifact → ``(tests, suite, import_row, error, meta)``.

    The two halves are read independently ON PURPOSE. An install that died
    before pytest ran leaves the import file and no junit; an import that
    crashed leaves the junit and a null-seconds import row. Either half alone is
    still data, and losing it because its sibling is missing would be a hole in
    the record for no reason. Only when BOTH are absent is the artifact an
    honest ``no unit-timings dataset``.
    """
    root = Path(directory)
    tests: list[dict[str, Any]] = []
    suite: dict[str, Any] = {}
    import_row: dict[str, Any] | None = None
    errors: list[str] = []

    junit_paths = sorted(root.rglob(JUNIT_FILENAME))
    if junit_paths:
        try:
            text = junit_paths[0].read_text()
        except OSError:
            errors.append(f"{JUNIT_FILENAME} was unreadable")
        else:
            tests, suite, err = parse_junit(text)
            if err:
                errors.append(err)

    import_paths = sorted(root.rglob(IMPORT_FILENAME))
    if import_paths:
        try:
            text = import_paths[0].read_text()
        except OSError:
            errors.append(f"{IMPORT_FILENAME} was unreadable")
        else:
            import_row, err = parse_import_time(text)
            if err:
                errors.append(err)

    if not junit_paths and not import_paths:
        return [], {}, None, NO_DATASET_ERROR, {}
    meta = {"python": str((import_row or {}).get("python") or "")}
    return tests, suite, import_row, "; ".join(errors), meta


# --- prompts (written by the producer; never re-derived by a renderer) -------
def test_prompt(repo: str, nodeid: str, prev_s: Any, now_s: Any,
                prev_run_url: str, run_url: str) -> str:
    """The self-contained /hygiene prompt a slowed test copies."""
    prev = _as_float(prev_s) or 0.0
    now = _as_float(now_s) or 0.0
    return (
        f"/hygiene perf: {repo} {nodeid} {prev:.1f}s → {now:.1f}s "
        f"between runs {prev_run_url} → {run_url}"
    )


def import_prompt(repo: str, package: str, python: str, baseline_s: Any,
                  now_s: Any, run_url: str) -> str:
    """The self-contained /hygiene prompt a slowed import copies.

    ``repo`` is taken for symmetry with ``test_prompt`` (every caller has it)
    and deliberately not repeated in the string: the package name identifies the
    library unambiguously, and the run URL already names the repo.
    """
    base = _as_float(baseline_s) or 0.0
    now = _as_float(now_s) or 0.0
    return (
        f"/hygiene perf: import {package} {base:.2f}s → {now:.2f}s "
        f"(py{python}) — {run_url}"
    )


# --- per-repo sidecar --------------------------------------------------------
def build_sidecar(
    name: str,
    group: str,
    owner: str,
    legs: list[dict[str, Any]],
    ts: str,
    error: str = "",
    top_n: int = int(DEFAULT_UNIT_TIMINGS_THRESHOLDS["top_n"]),
) -> dict[str, Any]:
    """Construct one repo's unit_timings sidecar.

    ``legs`` is what the shell leg produced: one entry per selected artifact,
    ``{"artifact": <select_artifacts row>, "dir": <extracted dir or None>,
    "error": <download error or "">}``.

    ``error`` is the reason the artifacts listing could not be fetched. When set
    the sidecar carries NO legs at all: a dead query must never render as a repo
    with no slow tests. A single leg that failed to download keeps its
    provenance (run, python, url) with its own ``error`` and no rows.

    Only the ``top_n`` slowest tests survive into the sidecar (sorted by seconds
    descending, ties broken by node id so the file is stable): a 1500-test suite
    whole would multiply every artifact downstream — the sidecar, the rollup and
    the committed record — for rows nothing renders. ``suite`` keeps the
    coverage behind them visible.
    """
    if error:
        return {
            "name": name, "group": group, "owner": owner, "error": error,
            "ts": ts, "legs": [],
        }

    out_legs: list[dict[str, Any]] = []
    for leg in legs or []:
        if not isinstance(leg, dict):
            continue
        art = leg.get("artifact")
        art = art if isinstance(art, dict) else {}
        run_id = art.get("run_id")
        run_url = (
            f"https://github.com/{owner}/{name}/actions/runs/{run_id}"
            if owner and name and run_id is not None
            else ""
        )
        leg_error = str(leg.get("error") or "")
        tests: list[dict[str, Any]] = []
        suite: dict[str, Any] = {}
        import_row: dict[str, Any] | None = None
        meta: dict[str, str] = {}
        if not leg_error:
            directory = leg.get("dir")
            if directory:
                tests, suite, import_row, leg_error, meta = read_downloaded_leg(directory)
            else:
                leg_error = "artifact was not downloaded"
        slowest = sorted(tests, key=lambda row: (-row["seconds"], row["nodeid"]))
        out_legs.append(
            {
                "python": str(art.get("python") or meta.get("python") or ""),
                "artifact_id": art.get("id"),
                "run_id": run_id,
                "run_url": run_url,
                "head_branch": str(art.get("head_branch") or ""),
                "head_sha": str(art.get("head_sha") or ""),
                "at": str(art.get("created_at") or ""),
                "error": leg_error,
                "suite": suite,
                "slowest": slowest[: max(int(top_n or 0), 0)],
                "import": (
                    {
                        "package": import_row["package"],
                        "seconds": import_row["seconds"],
                        "returncode": import_row["returncode"],
                    }
                    if import_row
                    else None
                ),
            }
        )
    out_legs.sort(key=lambda leg: (str(leg.get("python") or ""),
                                   str(leg.get("artifact_id") or "")))
    return {
        "name": name, "group": group, "owner": owner, "error": "",
        "ts": ts, "legs": out_legs,
    }


# --- classification ----------------------------------------------------------
def classify_import(
    seconds: Any,
    history: list[Any] | None,
    thresholds: dict[str, float],
) -> tuple[str, float | None, float | None, int]:
    """``(state, baseline_s, ratio, samples)`` for one import measurement.

    The ``import_time`` doctrine, kept verbatim so a ratio means the same thing
    from either vantage: the baseline is the MEDIAN of the recorded window
    (never the single previous run — an import is a few seconds and jitters),
    and nothing is judged until ``import_min_samples`` prior observations exist.
    ``building`` is that honest "not yet" state, and it is also what a failed
    import gets: with no number there is no verdict to give.
    """
    samples = [
        value for value in (_as_float(v) for v in (history or []))
        if value is not None and value > 0
    ]
    baseline = round(statistics.median(samples), 3) if samples else None
    now = _as_float(seconds)
    minimum = int(thresholds.get("import_min_samples", 3) or 0)
    if now is None or now <= 0 or baseline is None or len(samples) < minimum:
        return "building", baseline, None, len(samples)
    ratio = round(now / baseline, 2)
    state = _classify_ratio(
        ratio,
        float(thresholds.get("import_yellow_factor", 1.5)),
        float(thresholds.get("import_red_factor", 3.0)),
    )
    return state, baseline, ratio, len(samples)


# --- aggregate ---------------------------------------------------------------
def aggregate(
    sidecars: list[dict[str, Any]],
    ts: str,
    thresholds: dict[str, float] | None = None,
    record_prev_rows: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    record_import_history: dict[tuple[str, str, str], list[float]] | None = None,
) -> dict[str, Any]:
    """Fold the per-repo sidecars + the committed record into the rollup.

    ``record_prev_rows`` is ``{(repo, python, nodeid): prev_row}`` and
    ``record_import_history`` is ``{(repo, package, python): [seconds]}``, both
    out of ``heart.timings`` (``timings/unit/<repo>.jsonl``). Unlike
    ``smoke_timings`` there is no published-board fallback: the board never
    carried these rows, so on the first run every test is ``ok`` and every
    import is ``building`` — which is exactly what "no baseline yet" means.
    """
    thr = dict(thresholds or DEFAULT_UNIT_TIMINGS_THRESHOLDS)
    prev_rows = dict(record_prev_rows or {})
    history = dict(record_import_history or {})

    repos: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    def _name_of(side: Any) -> str:
        return str(side.get("name") or "") if isinstance(side, dict) else ""

    for side in sorted(sidecars or [], key=_name_of):
        if not isinstance(side, dict):
            continue
        repo = str(side.get("name") or "")
        if side.get("error"):
            errors.append({"repo": repo, "error": str(side["error"])})
            continue
        legs = side.get("legs")
        if not isinstance(legs, list):
            continue
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            python = str(leg.get("python") or "")
            run_id = leg.get("run_id")
            run_url = str(leg.get("run_url") or "")
            at = str(leg.get("at") or "")
            leg_error = str(leg.get("error") or "")
            if leg_error:
                # Provenance survives: which run we could not read, and why.
                errors.append({"repo": repo, "error": f"{python or '?'}: {leg_error}"})

            suite = leg.get("suite") if isinstance(leg.get("suite"), dict) else {}
            slowest = [row for row in (leg.get("slowest") or [])
                       if isinstance(row, dict)]

            for row in slowest:
                nodeid = str(row.get("nodeid") or "")
                seconds = _as_float(row.get("seconds"))
                if not nodeid or seconds is None:
                    continue
                prev_row = prev_rows.get((repo, python, nodeid))
                state, ratio, delta = classify_test_drift(
                    seconds, prev_row, thr, run_id=run_id
                )
                prev_s = _as_float((prev_row or {}).get("seconds"))
                prev_run_id = (prev_row or {}).get("run_id")
                prev_run_url = str((prev_row or {}).get("run_url") or "")
                tests.append({
                    "repo": repo,
                    "python": python,
                    "nodeid": nodeid,
                    "seconds": seconds,
                    "run_id": run_id,
                    "run_url": run_url,
                    "prev_s": prev_s,
                    "prev_run_id": prev_run_id,
                    "ratio": ratio,
                    "delta_s": delta,
                    "state": state,
                    "prompt": (test_prompt(repo, nodeid, prev_s, seconds,
                                           prev_run_url, run_url)
                               if state == "warn" else None),
                })

            imported = leg.get("import") if isinstance(leg.get("import"), dict) else None
            package = str((imported or {}).get("package") or "")
            import_s = _as_float((imported or {}).get("seconds"))
            if imported is not None and package:
                state, baseline, ratio, samples = classify_import(
                    import_s, history.get((repo, package, python)), thr
                )
                imports.append({
                    "repo": repo,
                    "package": package,
                    "python": python,
                    "seconds": import_s,
                    "run_id": run_id,
                    "run_url": run_url,
                    "baseline_s": baseline,
                    "samples": samples,
                    "ratio": ratio,
                    "state": state,
                    "prompt": (import_prompt(repo, package, python, baseline,
                                             import_s, run_url)
                               if state in ("yellow", "red") else None),
                })

            repos.append({
                "repo": repo,
                "python": python,
                "run_id": run_id,
                "run_url": run_url,
                "head_branch": str(leg.get("head_branch") or ""),
                "head_sha": str(leg.get("head_sha") or ""),
                "at": at,
                "tests": _as_int(suite.get("tests")) or 0,
                "wall_s": _as_float(suite.get("wall_s")) or 0.0,
                "import_s": import_s,
                "package": package,
                "error": leg_error,
                # Carried whole so `heart.timings` can build the committed
                # record from the rollup alone, without re-reading a sidecar.
                "suite": dict(suite),
                "slowest": [{"nodeid": str(row.get("nodeid") or ""),
                             "seconds": _as_float(row.get("seconds"))}
                            for row in slowest],
            })

    repos.sort(key=lambda r: (str(r.get("repo") or ""), str(r.get("python") or "")))
    tests.sort(key=lambda r: (str(r.get("repo") or ""), str(r.get("python") or ""),
                              str(r.get("nodeid") or "")))
    imports.sort(key=lambda r: (str(r.get("repo") or ""), str(r.get("python") or ""),
                                str(r.get("package") or "")))
    return {
        "ts": ts,
        "repos": repos,
        "tests": tests,
        "imports": imports,
        "slowed_tests": [r for r in tests if r.get("state") == "warn"],
        "slowed_imports": [r for r in imports
                           if r.get("state") in ("yellow", "red")],
        "errors": errors,
        "thresholds": thr,
    }


# --- the two legacy summaries the board sections already read ----------------
def legacy_summaries(
    rollup: dict[str, Any], thresholds: dict[str, float] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(unit_test_timing summary, import_time summary)`` from the rollup.

    EXACTLY the shapes ``unit_test_timing.run()`` and ``import_time.run()``
    return, key for key, because the board's two sections read those files and
    this check is now their cloud source. Nothing in ``heart/dashboard.py``'s
    state logic changes: the same summary, measured somewhere better.

    ``python`` is ``"ci"`` — these numbers came off a CI runner, not off a
    ``HYGIENE_PYTHON`` on somebody's laptop, and the field is the honest place
    to say so.
    """
    thr = dict(thresholds or DEFAULT_UNIT_TIMINGS_THRESHOLDS)
    rollup = rollup if isinstance(rollup, dict) else {}
    repos = [r for r in (rollup.get("repos") or []) if isinstance(r, dict)]
    test_rows = [r for r in (rollup.get("tests") or []) if isinstance(r, dict)]
    import_rows = [r for r in (rollup.get("imports") or []) if isinstance(r, dict)]
    errors = [e for e in (rollup.get("errors") or []) if isinstance(e, dict)]

    # --- unit tests ---------------------------------------------------------
    measured = {str(r.get("repo") or "") for r in repos if not r.get("error")}
    unavailable = sorted({str(e.get("repo") or "") for e in errors
                          if str(e.get("repo") or "")} - measured)
    unit: dict[str, list[dict[str, Any]]] = {"red": [], "yellow": [], "green": []}
    new_tests = 0
    for row in test_rows:
        ratio = _as_float(row.get("ratio"))
        if ratio is None or _as_float(row.get("prev_s")) is None:
            new_tests += 1
            continue
        entry = {
            "repo": str(row.get("repo") or ""),
            "test": str(row.get("nodeid") or ""),
            "latest_seconds": round(_as_float(row.get("seconds")) or 0.0, 3),
            "baseline_seconds": round(_as_float(row.get("prev_s")) or 0.0, 3),
            "ratio": ratio,
            # One recorded previous observation is the whole baseline here (the
            # both-gates rule compares run to run), never a rolling window.
            "samples": 1,
        }
        if ratio > LEGACY_RED_FACTOR:
            unit["red"].append(entry)
        elif ratio > LEGACY_YELLOW_FACTOR:
            unit["yellow"].append(entry)
        else:
            unit["green"].append(entry)

    unit_summary = {
        "python": LEGACY_PYTHON,
        "repos_measured": len(measured),
        "repos_unavailable": unavailable,
        "new_tests_no_baseline": new_tests,
        "red_count": len(unit["red"]),
        "yellow_count": len(unit["yellow"]),
        "green_count": len(unit["green"]),
        "red": sorted(unit["red"], key=lambda x: -x["ratio"]),
        "yellow": sorted(unit["yellow"], key=lambda x: -x["ratio"]),
    }

    # --- imports ------------------------------------------------------------
    imports: dict[str, list[dict[str, Any]]] = {"red": [], "yellow": [], "green": []}
    packages_measured = 0
    packages_unavailable: list[str] = []
    new_packages = 0
    for row in import_rows:
        package = str(row.get("package") or "")
        seconds = _as_float(row.get("seconds"))
        if seconds is None:
            # No number is not a slow import: it is an import that did not
            # happen, and it says so in its own field rather than as a 0 s row.
            if package not in packages_unavailable:
                packages_unavailable.append(package)
            continue
        packages_measured += 1
        state = str(row.get("state") or "")
        if state == "building":
            new_packages += 1
            continue
        entry = {
            "package": package,
            "latest_seconds": round(seconds, 3),
            "baseline_seconds": round(_as_float(row.get("baseline_s")) or 0.0, 3),
            "ratio": _as_float(row.get("ratio")) or 0.0,
            "samples": _as_int(row.get("samples")) or 0,
        }
        # An unrecognised state degrades to green rather than vanishing: a row
        # that measured something must show up in exactly one bucket.
        imports.get(state, imports["green"]).append(entry)

    import_summary = {
        "python": LEGACY_PYTHON,
        "packages_measured": packages_measured,
        "packages_unavailable": packages_unavailable,
        "new_packages_no_baseline": new_packages,
        "red_count": len(imports["red"]),
        "yellow_count": len(imports["yellow"]),
        "green_count": len(imports["green"]),
        "red": sorted(imports["red"], key=lambda x: -x["ratio"]),
        "yellow": sorted(imports["yellow"], key=lambda x: -x["ratio"]),
    }
    return unit_summary, import_summary


def read_sidecars(per_repo_dir: Path) -> list[dict[str, Any]]:
    """Every ``*.unit_timings.json`` sidecar under ``per_repo_dir`` (I/O)."""
    out: list[dict[str, Any]] = []
    for path in sorted(Path(per_repo_dir).glob("*.unit_timings.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def read_downloads(
    artifacts: list[dict[str, Any]], downloads_dir: Path | str | None
) -> list[dict[str, Any]]:
    """Pair each selected artifact with its extracted dir (or its error).

    The shell leg writes ``<downloads>/<id>/`` for a download that worked and
    ``<downloads>/<id>.error`` (one line of text) for one that did not, so a
    403 on one python leg still leaves the other leg's data intact.
    """
    legs: list[dict[str, Any]] = []
    root = Path(downloads_dir) if downloads_dir else None
    for art in artifacts:
        directory = None
        error = ""
        if root is None:
            error = "no downloads directory"
        else:
            err_file = root / f"{art['id']}.error"
            art_dir = root / str(art["id"])
            if err_file.is_file():
                try:
                    error = " ".join(err_file.read_text().split())[:200]
                except OSError:  # pragma: no cover - unreadable marker file
                    error = ""
                error = error or "artifact download failed"
            elif art_dir.is_dir():
                directory = art_dir
            else:
                error = "artifact download failed"
        legs.append({"artifact": art, "dir": directory, "error": error})
    return legs


# --- summary lines -----------------------------------------------------------
def repo_summary_line(sidecar: dict[str, Any]) -> str:
    """Coloured one-line per-repo summary for the daemon log."""
    from heart.heart_color import (
        c_info, c_meta, c_ok, c_warn, glyph_ok, glyph_warn,
    )

    name = sidecar.get("name", "?")
    if sidecar.get("error"):
        return (f"{glyph_warn()} {c_info(name)} {c_warn('unit_timings UNAVAILABLE')} "
                f"{c_meta(str(sidecar['error']))}")
    legs = [leg for leg in (sidecar.get("legs") or []) if isinstance(leg, dict)]
    tracked = sum(len(leg.get("slowest") or []) for leg in legs)
    broken = [leg for leg in legs if leg.get("error")]
    if broken:
        return (f"{glyph_warn()} {c_info(name)} "
                f"{c_warn(f'{len(broken)} leg(s) unavailable')} "
                f"{c_meta(f'{tracked} test(s) tracked')}")
    return (f"{glyph_ok()} {c_info(name)} {c_ok(f'{tracked} test(s) tracked')} "
            f"{c_meta(f'{len(legs)} leg(s)')}")


def summary_line(rollup: dict[str, Any]) -> str:
    """Coloured one-line summary of the global rollup."""
    from heart.heart_color import (
        c_info, c_meta, c_ok, c_warn, glyph_ok, glyph_warn,
    )

    legs = len(rollup.get("repos") or [])
    tracked = len(rollup.get("tests") or [])
    slowed = len(rollup.get("slowed_tests") or [])
    imports = rollup.get("imports") or []
    red = len([r for r in imports if isinstance(r, dict) and r.get("state") == "red"])
    yellow = len([r for r in imports
                  if isinstance(r, dict) and r.get("state") == "yellow"])
    errors = len(rollup.get("errors") or [])
    body = (f"{legs} suites, {tracked} tests tracked, {slowed} slowed, "
            f"{len(imports)} imports ({red} red / {yellow} yellow)")
    if errors:
        body += f", {errors} unavailable"
    bad = slowed or red or yellow or errors
    glyph = glyph_warn() if bad else glyph_ok()
    tint = c_warn if bad else c_ok
    return (f"{glyph} {c_info('unit_timings:')} {tint(body)} "
            f"{c_meta(f'({legs} leg(s))')}")


# --- I/O shell ---------------------------------------------------------------
def _write(out_path: Path, payload: dict[str, Any]) -> None:
    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(out_path, payload)


def _read_listing(path: str) -> tuple[Any, str]:
    """The artifacts listing file → (payload, error)."""
    try:
        return json.loads(Path(path).read_text()), ""
    except (OSError, ValueError):
        return None, "artifacts listing was not valid JSON"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="heart.checks.unit_timings")
    ap.add_argument("--plan", action="store_true",
                    help="read an artifacts listing on stdin and print the "
                         "'<id> <name>' lines the shell leg should download")
    ap.add_argument("--aggregate", action="store_true",
                    help="fold the per-repo sidecars into the global rollup "
                         "and the two legacy summaries")
    ap.add_argument("--name", default="")
    ap.add_argument("--group", default="")
    ap.add_argument("--owner", default="")
    ap.add_argument("--ts", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--listing", default="",
                    help="file holding the artifacts listing JSON (per-repo mode)")
    ap.add_argument("--downloads", default="",
                    help="dir holding <artifact id>/ extractions and "
                         "<artifact id>.error markers (per-repo mode)")
    ap.add_argument("--per-repo-dir", default="",
                    help="where the sidecars live (default: $HEART_STATE_DIR/per-repo)")
    ap.add_argument("--record-dir", default="",
                    help="the committed timing record (default: HEART_HOME/timings); "
                         "its unit/ files are the ONLY baseline — with none, "
                         "every test is ok and every import is building")
    ap.add_argument("--legacy-dir", default="",
                    help="where unit_test_timing.json and import_time.json are "
                         "written (default: $HEART_STATE_DIR)")
    ap.add_argument("--fetch-error", default="",
                    help="reason the artifacts listing fetch failed; recorded "
                         "instead of a bogus quiet repo")
    ns = ap.parse_args(argv)

    if ns.plan:
        try:
            listing: Any = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            # Nothing to download, and nothing to say: the per-repo call reads
            # the same listing file and records the failure honestly there.
            return 0
        for art in select_artifacts(listing):
            print(f"{art['id']} {art['name']}")
        return 0

    if not ns.out:
        ap.error("--out is required outside --plan mode")
    ts = ns.ts or datetime.datetime.now(datetime.timezone.utc).isoformat()
    thresholds = load_thresholds()

    if ns.aggregate:
        sys.path.insert(0, str(HEART_HOME))
        from heart import state as _state
        from heart import timings as _timings

        per_repo = Path(ns.per_repo_dir) if ns.per_repo_dir else _state.HEART_PER_REPO_DIR
        record_dir = Path(ns.record_dir) if ns.record_dir else _timings.TIMINGS_DIR
        unit_dir = record_dir / "unit"
        rollup = aggregate(
            read_sidecars(per_repo), ts, thresholds,
            record_prev_rows=_timings.previous_unit_rows(unit_dir),
            record_import_history=_timings.import_history(
                unit_dir, int(thresholds.get("import_window", 7) or 7)
            ),
        )
        _write(Path(ns.out), rollup)
        # The two legacy summaries: the SAME files the dev-box checks write, so
        # the board's existing sections come alive without changing shape.
        legacy_dir = Path(ns.legacy_dir) if ns.legacy_dir else _state.HEART_STATE_DIR
        unit_summary, import_summary = legacy_summaries(rollup, thresholds)
        _write(legacy_dir / "unit_test_timing.json", unit_summary)
        _write(legacy_dir / "import_time.json", import_summary)
        print(summary_line(rollup))
        return 0

    if not ns.name or not ns.group:
        ap.error("--name and --group are required outside --aggregate mode")

    error = ns.fetch_error
    legs: list[dict[str, Any]] = []
    if not error:
        listing, listing_error = _read_listing(ns.listing)
        error = listing_error
        if not error:
            legs = read_downloads(select_artifacts(listing), ns.downloads or None)

    sidecar = build_sidecar(ns.name, ns.group, ns.owner, legs, ts, error=error,
                            top_n=int(thresholds.get("top_n", 25) or 25))
    _write(Path(ns.out), sidecar)
    print(repo_summary_line(sidecar))
    return 0


if __name__ == "__main__":
    sys.exit(main())
