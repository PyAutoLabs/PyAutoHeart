# PyAutoBuild boundary audit — has health/readiness logic drifted?

The Health Agent task requires verifying that no health/readiness logic still
lives in PyAutoBuild (Hands), which must be a **pure executor**. This is the
audit. Performed against PyAutoBuild at `114ecec` (Merge #109,
build-pulse-agent-separation).

## Verdict: clean — no health/readiness *gating* logic in PyAutoBuild

Every actual gate has already moved to PyAutoHeart. What remains in Build is
executor primitives and documentation that correctly points at Heart.

### What was checked

Searched all `*.sh / *.py / *.yml / *.md` in PyAutoBuild for
`readiness | verify_workspace_version | health | version_skew | verify_install |
url_check`.

| Finding | Location | Assessment |
|---|---|---|
| `verify_workspace_versions.sh` | — | **Removed.** No longer present; its job is Heart's `version_skew` check. ✓ |
| readiness mentioned in comments | `release.yml`, `pre_build.sh` | Docs only — both explicitly state readiness is enforced upstream via `pyauto-pulse readiness`. ✓ |
| no `url_check.yml` workflow | `.github/workflows/` | URL hygiene fully owned by Heart's central `url-check.yml`. ✓ |
| no `readiness.py` / `version_skew.py` / `*health*` | repo-wide | No check modules drifted. ✓ |

### One nuance the agent must understand: `aggregate_results.py`

`autobuild/aggregate_results.py` builds a report titled **"Release Readiness
Report"** with a top-level `ready` boolean, and `create_analysis_issue.py` posts
it. This *looks* like a readiness gate but is **not**:

- `ready` is computed as `not has_failures` — purely "did the workspace scripts
  run without failures". It is a **script-run aggregation**, not a green/yellow/red
  release gate.
- It is one of Build's **executor primitives**. Heart's `workspace-validation.yml`
  checks these primitives out from Build and reuses them (by design — not
  duplicated), and Heart's `test_run` check *consumes* the resulting
  `report.json` as one YELLOW-capable input into the authoritative `readiness`
  verdict.

So the authoritative gate is `pyauto-heart readiness`; Build's `report.json`
`ready` flag is an *input* to it, named confusingly. **The Health Agent must
treat `pyauto-heart readiness` as the single source of truth and never read
Build's `report.json` directly for the verdict.**

## Recommendation

No migration required — the boundary is architecturally correct. The only
residual is **naming**: Build's "Release Readiness Report" / `ready` field shares
vocabulary with Heart's authoritative verdict and could mislead a future reader.

- **Low-risk, optional:** rename Build's report to "Workspace Validation Report"
  / `scripts_passed` (or similar) to remove the ambiguity. This is a
  documentation/naming change, not a logic move, so it does not block the Health
  Agent.
- A follow-up task capturing this is filed in PyAutoMind:
  `maintenance/autobuild/rename_release_readiness_report.md`.

## Boundary the agent enforces

```
PyAutoHeart  — owns health checks + the authoritative readiness verdict.
PyAutoBrain  — Health Agent reasons over Heart's outputs (owns no checks).
PyAutoBuild  — executor; acts only after a GREEN/YELLOW/RED decision; runs no checks.
```
