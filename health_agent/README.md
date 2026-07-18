# Heart capability manifest (for the Brain Health Agent)

This directory is **PyAutoHeart self-describing its health capabilities**, so the
PyAutoBrain Health Agent can reason over them without coupling to individual
check names. It holds the abstract provider manifest and supporting audits — it
does **not** hold the agent itself.

## Where the Health Agent lives

The **Health Agent** — the first PyAutoBrain specialist reasoning agent — now
lives in its canonical home, **PyAutoBrain**:

- Definition: `agents/health/AGENTS.md` in the PyAutoBrain checkout
  (`pyauto-brain help health`).
- Entrypoint: `agents/health/health.sh` (`pyauto-brain health`).

The agent reasons over PyAutoHeart's outputs and emits a **GREEN / YELLOW / RED**
decision (Summary / Warnings / Recommendations / Blocking Issues). It performs no
checks itself: `Mind -> Brain (reason) -> Heart (gate) -> Hands/Build (execute)`.

It was implemented and staged here first because PyAutoBrain was not yet in scope;
the agent definition (`health_agent.md`) has since been lifted into PyAutoBrain
and removed from this directory.

## Why these files stay in Heart

`capabilities.yaml` is Heart **self-describing its health surface**, which belongs
in Heart and keeps the agent decoupled from individual check names. The Brain
Health Agent reads this manifest (via the PyAutoHeart checkout / `pyauto-heart`
output) as an **abstract provider manifest** — it is never vendored or copied into
Brain. When Heart gains or renames a check, update the manifest here and the agent
adapts with no edits.

## Contents

| File | What it is |
|---|---|
| [`capabilities.yaml`](./capabilities.yaml) | Machine-readable manifest of every Heart capability — the abstract-provider self-description the Brain agent reads. |
| [`capabilities.md`](./capabilities.md) | Human-readable audit of Heart's full health surface (CLI, checks, readiness, workflows, state, docs). |
| [`pyautobuild_boundary_audit.md`](./pyautobuild_boundary_audit.md) | Audit confirming no health/readiness gating logic has drifted into PyAutoHands, with the one naming nuance and a follow-up. |

## Quick use (from the Brain agent)

```bash
pyauto-heart readiness --json   # authoritative verdict the agent adopts
pyauto-heart status --json      # detail for explanation / recommendations
```

Then produce the report in the schema defined in PyAutoBrain's
`agents/health/AGENTS.md`. The single most important output is the headline word:
**GREEN**, **YELLOW**, or **RED**.
