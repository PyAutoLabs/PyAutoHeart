# PyAutoHeart

🧬 **PyAutoScientist → <https://github.com/PyAutoLabs/PyAutoScientist>** — this repo is one organ of the PyAuto organism.

📖 **Full documentation → <https://pyautoscientist.readthedocs.io>** — the whole PyAutoScientist organism, including how to fork and run your own.

[![health](https://img.shields.io/endpoint?url=https://pyautolabs.github.io/PyAutoHeart/badge.json)](https://pyautolabs.github.io/PyAutoHeart/)

<!-- The block below is auto-updated by .github/workflows/heart-health.yml (everything -->
<!-- between the heart:begin/heart:end markers is replaced with the rendered board). -->
<!-- Live board: https://pyautolabs.github.io/PyAutoHeart/ -->
<!-- heart:begin -->
## 🟡 PyAuto health — **YELLOW** (score 60)

_snapshot `2026-07-14T07:09:41.974430+00:00` · just now_

**Warnings:** PyAutoMind: open PR 14d old

| | Check | Status |
|--|--|--|
| 🟢 | Libraries | 5 repos nominal |
| 🔵 | Workspaces | 9 repos nominal |
| ⚪ | Worktree drift | not observed here (dev-box only) |
| ⚪ | Script timing | not observed here (dev-box only) |
| ⚪ | Import timing | not observed here (dev-box only) |
| ⚪ | Unit-test timing | not observed here (dev-box only) |
| ⚪ | Profiling drift | not observed here (dev-box only) |
| ⚪ | Workspace test-mode timing | not observed here (dev-box only) |
| ⚪ | Test run | not observed here (dev-box only) |
| ⚪ | Version skew | not observed here (dev-box only) |

[Full board](https://pyautolabs.github.io/PyAutoHeart/)
<!-- heart:end -->

The health layer of the PyAuto organism. Heart continuously watches every
repo — branch state, CI, open PRs, version skew, script timing, workspace
validation — and rolls what it sees into one authoritative verdict:

```bash
pyauto-heart readiness       # GREEN / YELLOW / RED, a score, and the reasons
```

GREEN means it is safe to release. Heart is an observer: it never writes
into other repos and never triggers a build — the Brain reads the verdict
and decides what to do with it.

Daily driving:

```bash
pyauto-heart tick            # one refresh cycle
pyauto-heart status          # pretty-print the cached state
pyauto-heart watch           # the daemon: tick every 5 min, live board on a tty
pyauto-heart dashboard       # the board (also --md, --html, --json, --oneline)
```

Runs from its checkout (`PYTHONPATH` + `PATH`, no pip install); state lives
under `~/.pyauto-heart/`. Which repos are polled, and with what thresholds,
is `config/repos.yaml`. Tests: `pytest tests/`.

Boundary and agent guidance: [AGENTS.md](AGENTS.md). The organism:
[PyAutoBrain/ORGANISM.md](https://github.com/PyAutoLabs/PyAutoBrain/blob/main/ORGANISM.md),
documented in full at <https://pyautoscientist.readthedocs.io>.
