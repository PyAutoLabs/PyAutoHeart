# Health Agent (PyAutoBrain specialist)

This directory holds the **Health Agent** — the first PyAutoBrain specialist
agent. It decides whether the PyAuto organism is healthy enough to proceed, by
reasoning over PyAutoHeart's outputs and emitting a **GREEN / YELLOW / RED**
decision. It performs no health checks itself.

## Why this lives in PyAutoHeart (for now)

The agent's canonical home is **PyAutoBrain** (`Mind -> Brain -> Heart -> Hands`;
Brain reasons, Heart checks). PyAutoBrain was not available in the environment
that implemented this task, and the agent is tightly coupled to Heart's
capability manifest, so it is staged here and written to lift into PyAutoBrain
unchanged. The migration follow-up is filed in PyAutoMind at
`feature/pyautobrain/health_agent_migrate_to_brain.md`.

Co-locating it with Heart has one durable benefit regardless of where the agent
ends up: `capabilities.yaml` is Heart **self-describing its health capabilities**,
which belongs in Heart and keeps the agent decoupled from individual check names.

## Contents

| File | What it is |
|---|---|
| [`health_agent.md`](./health_agent.md) | The agent definition: role, how to invoke Heart, reasoning procedure, output schema, gate semantics, hard boundaries. |
| [`capabilities.yaml`](./capabilities.yaml) | Machine-readable manifest of every Heart capability — the abstract-provider self-description the agent reads. |
| [`capabilities.md`](./capabilities.md) | Human-readable audit of Heart's full health surface (CLI, checks, readiness, workflows, state, docs). |
| [`pyautobuild_boundary_audit.md`](./pyautobuild_boundary_audit.md) | Audit confirming no health/readiness gating logic has drifted into PyAutoBuild, with the one naming nuance and a follow-up. |

## Quick use

```bash
pyauto-heart readiness --json   # authoritative verdict the agent adopts
pyauto-heart status --json      # detail for explanation / recommendations
```

Then produce the report in the schema defined in `health_agent.md`. The single
most important output is the headline word: **GREEN**, **YELLOW**, or **RED**.
