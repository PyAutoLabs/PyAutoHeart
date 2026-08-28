<p align="center">
  <img src="logo.png" alt="PyAutoHeart" width="400">
</p>

# PyAutoHeart

[![PyAutoScientist GitHub](https://img.shields.io/badge/%F0%9F%94%AC%20PyAutoScientist-GitHub-181717?style=flat-square)](https://github.com/PyAutoLabs/PyAutoScientist) [![PyAutoScientist ReadTheDocs](https://img.shields.io/badge/%F0%9F%93%96%20PyAutoScientist-ReadTheDocs-8CA1AF?style=flat-square)](https://pyautoscientist.readthedocs.io)

[![health](https://img.shields.io/endpoint?url=https://pyautolabs.github.io/PyAutoHeart/badge.json)](https://pyautolabs.github.io/PyAutoHeart/)

**PyAutoHeart is the Heart of the PyAutoScientist** — the organism's health
authority. It continuously watches every repository (CI, branches, open PRs,
version skew, script and test timings, workspace validation) and rolls what it
sees into one authoritative verdict: **GREEN / STALE / YELLOW / RED**. GREEN
means it is safe to release.

See the **[PyAutoHeart Dashboard](https://pyautolabs.github.io/PyAutoHeart/)**
for the whole picture on one page — every check a
traffic-light row, and every red or yellow finding carrying links to the
failing run and a one-tap 📋 button that copies a ready-made Claude prompt
(`/bug …`), so going from "something is red" to "an agent is fixing it" is
copy → paste, on a laptop or a phone.

## Current health

<!-- The line below is auto-updated by .github/workflows/heart-health.yml (everything -->
<!-- between the heart:begin/heart:end markers is replaced with the rendered strip). -->
<!-- heart:begin -->
🔵 **STALE** · score 65 · [dashboard →](https://pyautolabs.github.io/PyAutoHeart/)
<!-- heart:end -->

## How PyAutoHeart works

1. **Observe.** `pyauto-heart tick` (or the 5-minute daemon) runs the cheap
   checks — repo state, CI conclusions, open PRs, worktree drift, timings —
   into one cached snapshot. Deep checks (install verification, workspace
   validation, URL hygiene) run on demand or on cloud schedules.
2. **Judge.** `pyauto-heart readiness` rolls the snapshot into the verdict and
   score. STALE means evidence is missing or expired, nothing known-bad — the
   remedy is re-running a check, never fixing code.
3. **Show.** One renderer projects the same snapshot everywhere, so the
   surfaces cannot disagree: the [Pages board](https://pyautolabs.github.io/PyAutoHeart/),
   the README strip above, the badge, the terminal board, and the JSON surface
   agents consume. A daily cloud run publishes the board and keeps a single
   `[heart-health]` tracking issue open while anything is degraded.
4. **Enrich.** The cloud can only see API-safe signals; checks needing a
   working tree are measured on the dev box, and `pyauto-heart publish` pushes
   a distilled observation so the same page fills in, age-stamped.
5. **Gate.** The Heart only observes — it never edits other repos and never
   triggers a build. The Brain reads the verdict (`/health`) and decides;
   releases require GREEN.

## CLI examples

```bash
pyauto-heart readiness       # GREEN / STALE / YELLOW / RED, a score, and the reasons
pyauto-heart tick            # one refresh cycle
pyauto-heart status          # pretty-print the cached state
pyauto-heart watch           # the daemon: tick every 5 min, live board on a tty
pyauto-heart dashboard       # the board (also --md, --md-brief, --html, --json, --oneline)
pyauto-heart publish         # push the dev-box observation to the live board
pyauto-heart fix ci <repo>   # bundle a failing topic into a paste-ready Claude prompt
pyauto-heart fix stale       # the evidence gaps + the one plan that clears them all
```

The full CLI surface, the run-from-checkout model, state layout, and verdict
semantics are in [REFERENCE.md](REFERENCE.md). How agents should operate this
repo is in [AGENTS.md](AGENTS.md). The organism this repo is the Heart of is
described once in
[PyAutoBrain/ORGANISM.md](https://github.com/PyAutoLabs/PyAutoBrain/blob/main/ORGANISM.md)
and documented in full at <https://pyautoscientist.readthedocs.io>.
