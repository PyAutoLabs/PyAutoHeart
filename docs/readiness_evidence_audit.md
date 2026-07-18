# The evidence chain behind Heart's release verdict — audit

**Status:** audit deliverable for PyAutoHeart#83 (build-chain campaign
PyAutoBuild#155, Phase 2). **Method:** measured 2026-07-16 against the live
state dir, the real repos, and the GitHub API; every number is re-measurable
and should be re-measured before acting. Companion:
`PyAutoHands/docs/pre_build_failure_audit.md` (Phase 1).

## 1. Per-leg evidence map (deliverable 1)

Legs in `heart/readiness.py::compute()`, in order:

| leg | artifact / writer | writer last ran | satisfiable? | fails safe? |
|---|---|---|---|---|
| library gates (CI / branch / dirty / behind) | `state.json.repos` ← tick bash checks | daily tick | **live** | yes — firing correctly today |
| workspace CI gate (required workflows) | same | daily tick | live | yes |
| `test_run` | `test_run.json` ← check `main()` | daily tick — **but see Finding A** | degraded | mostly (see §3 hole) |
| `version_skew` | `version_skew.json` ← check `main()` | daily tick | **cannot fire on releases** (Finding B) | **no — fails permissive** |
| `manifest_drift` | `manifest_drift.json` | daily tick | live | yes — firing today |
| `verify_install` | `verify_install.json` ← local run or `validate --ingest` | **never post-fix** (Finding C) | yes, unexercised | yes — absent blocks GREEN |
| `validation_report` | `validation_report.json` ← `validate --ingest` | 2026-07-15 | live | yes — SHA staleness firing today |
| `script_timing` / `profiling_drift` | sidecars ← tick | daily tick | live | yes |

## 2. The three integrity findings (measured)

**A — `test_run`'s server-first path is unreachable from every real
entrypoint.** `run()` fetches the cloud verdict only when `results_dir is
None`, but `main()` always passes it (`Path(argv[1]) if len(argv) > 1 else
TEST_RESULTS_LATEST`) — so the tick (`python -m heart.checks.test_run`) and
the mobile/MCP path both run with `fetch_cloud=False`. The exact `gh run list`
subprocess succeeds from a shell; today's tick-written sidecar is
`source=report` with zero cloud fields, pinned to the **2026-07-09** local
`test_results/latest` while three newer cloud runs exist. `tick.sh`'s comment
claims the step "runs ALWAYS — it is server-first"; the code disagrees.
*Merged ≠ ever ran*, in the check that exists to catch exactly that.

**B — `version_skew` is structurally unfailable by the release process.**
8/8 MATCH, comparing workspace pins against library stamps — two artifacts
releases stopped writing under the floor model (#120/#121). It can fire only
on hand-edits. A gate that cannot fire on the events it nominally guards is a
permanently-lit green light. Worse: the invariant that *should* be guarded —
**a floor must name an installable (non-yanked) version** — is guarded by
nothing. (Phase 4 of the campaign owns the rework; the prompt already exists:
`draft/feature/pyautoheart/version_skew_floor_rework.md`.)

**C — `verify_install` has never been satisfied since its fix.** PR#77
(persist the sidecar) merged 2026-07-15T13:08Z; the day's only `validate
--ingest` ran 10:29Z — pre-merge, so its verify evidence was discarded by the
old code. No ingest since; the file is absent; the leg reads "install
verification not run", exactly as it did before the fix. The fix is merged and
unexercised.

## 3. Finding 3 resolved: the smoke surface depended on leaked artifacts

Measured in HowToLens: dataset files were committed by `pre build` commits
(2026-05-01/14/29 — the `git add -f dataset/` era), **purged 2026-07-13**
("chore: purge force-committed simulated datasets [#151]"), and today the repo
tracks **zero** dataset files while ~30 tutorials `Imaging.from_fits` paths
like `dataset/imaging/simple__no_lens_light/`. A `scripts/simulator/` dir
exists that can generate them — but nothing in the validation path runs it
before the chapters. The tutorials never simulated for themselves; the leak
was the only thing feeding them.

**Classification: a pre-existing falsehood newly revealed, not a regression.**
The smoke gate's green history validated a repo state only leaked release
artifacts ever provided. The 07-15 "3 failed → 30 failed" delta is a surface
change (scripts+notebooks, plus the dataset absence), not 27 regressions.
**Remediation belongs in the HowTo* validation path** (run `scripts/simulator/`
before the chapters, or simulate-on-missing), not in Heart and not by
restoring datasets. Filed as a follow-up prompt (campaign satellite).

The comparability half of the question: the leg's history cannot be compared
across runs because the report does not record its own surface definition
(which repos, scripts-only vs scripts+notebooks, which env profile). Design
requirement below.

## 4. What is the verdict worth today? (design question 1)

The honest summary: **the gate holds, but partly by paralysis rather than by
trustworthy evidence.**

- It cannot currently go GREEN wrongly, because every degraded leg happens to
  fail safe: `test_run` non-True blocks; `verify_install` absent blocks;
  `validation_report` SHA-staleness blocks. The one permissive failure
  (`version_skew`) guards a retired invariant, so its silence admits no bad
  release by itself — but the invariant that replaced it (installable floors)
  is guarded by nothing.
- It also can barely go GREEN at all: GREEN needs a fresh verify_install
  ingest, a fresh release rehearsal matching current HEADs, and a passing
  test_run — the first has never happened post-fix, and the second expires on
  every source move. Safety-by-unreachability is still safety, but it makes
  the GREEN-gated nightly grant *vacuous* rather than *validated*.
- One narrow wrong-GREEN hole exists in `test_run`: a fresh, passing **local**
  `report.json` sets `ready=True` from the local surface alone (Finding A
  keeps the cloud verdict out), so a dev-box `run_all` pass can green the leg
  while the server surface fails. Narrow, but real.

**Recommendation on the nightly's standing grant:** keep it formally (its
GREEN condition fails safe), but treat it as inoperative until Findings A and
C are fixed and one verify_install ingest has actually run — then re-judge
with evidence that GREEN is *reachable and meaningful*. Do not widen the grant
while `test_run`'s local/cloud disagreement is silent.

## 5. Target design points (for the fix PRs, each behind plan approval)

1. **Fix the entrypoint wiring (A):** `fetch_cloud` decided explicitly at the
   entrypoints, not inferred from `results_dir` defaulting; the tick passes
   `fetch_cloud=True` always. When both local and cloud evidence exist and
   disagree, the leg must surface the disagreement, never prefer either
   silently (closes the §4 hole).
2. **Exercise C, then keep it exercised:** run one `verify_install` /
   `validate --ingest` post-fix to prove the leg can be satisfied; the
   evidence map above becomes the regression test ("has the writer ever run"
   should be checkable from the artifact, so a leg that has never been
   satisfiable is loud).
3. **Record the surface definition** in the test_run report (repos, script
   set, notebook inclusion, env profile) so two runs are comparable — a gate
   needs a stable denominator (§3).
4. **Retire or re-point version_skew (B)** per Phase 4's floor rework: the leg
   should guard "every floor names an installable release" (checkable against
   tags/PyPI), which is the invariant the 07-13 hand-bump actually violated.
5. **HowTo* validation runs simulators before chapters** (§3 remediation) —
   a workspace/PyAutoHands-runner change, not a Heart change.

## 6. Rejected / open decisions

- **Rejected: re-running legs until green.** A verdict change on re-run is a
  finding about the evidence (the brief's constraint held: the 07-15 re-run's
  30f *was* the finding).
- **Rejected: treating "the gate never wrongly greened" as vindication.** Two
  of the three examined legs were unsatisfiable and one was unfailable — the
  gate's record says little either way; the safe failures are partly luck
  (cf. the fixture-`ts` accident in the state-clobber bug, #78).
- **Open (human):** whether the nightly grant should be formally paused until
  §5.1–.2 land, vs. left standing on the fails-safe argument. Recommendation
  above; the call is a release-policy decision.
- **Open (human):** where the HowTo* simulator-before-chapters step runs
  (workspace-validation runner vs per-repo conftest-style bootstrap).

## Trust nothing here

Same authorship caveat as the campaign's other documents. During *this* audit,
one instrument error was made and caught (a piped exit code that read the
pager's status, not the command's — third occurrence this campaign; the
correct forms are recorded in the issue thread). Findings A–C each carry the
exact command or timestamp pair that establishes them — re-run those, not the
prose.
