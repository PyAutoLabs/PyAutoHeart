"""heart/checks/manifest_drift.py — body-map identity drift.

``PyAutoMind/repos.yaml`` is the single source of repo *identity* (GitHub
home, category, role). ``PyAutoMind/scripts/repos_sync.py --check`` verifies
the other repo lists against it — Heart's ``config/repos.yaml``, PyAutoBuild's
``pre_build.sh``, admin_jammy's ``ensure_workspace_labels.sh``, and the actual
``origin`` remote of every local checkout. That check only fires when someone
remembers to run it; this module makes it continuous by running it every tick
and parsing its report.

The drift logic itself stays in ``repos_sync.py`` (never duplicated here —
Heart observes, Mind owns the manifest). Cheap: local file parses plus one
``git remote get-url`` per checkout, well inside the tick budget.

Classification: drift is **YELLOW** — identity hygiene that will eventually
break a `gh` call or a label sweep, not an immediate release blocker (same
monitoring-not-gating stance as URL hygiene, but visible in readiness
cautions). A missing PyAutoMind checkout or manifest skips the check
(``available: false``) — normal for web/CI environments.

The result lands at ``$HEART_STATE_DIR/manifest_drift.json``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HEART_HOME = Path(__file__).resolve().parents[2]
_p3 = Path(__file__).resolve().parents[3]
PYAUTO_ROOT = Path(
    os.environ.get("PYAUTO_ROOT")
    or (_p3 if _p3.name == "PyAutoLabs" else Path.home() / "Code" / "PyAutoLabs")
)
HEART_STATE_DIR = Path(
    os.environ.get("HEART_STATE_DIR")
    or Path.home() / ".pyauto-heart"
)

_CHECK_LINE = re.compile(r"^check (?P<label>.+?): (?P<status>OK|\d+ mismatch\(es\))$")
_PROBLEM_LINE = re.compile(r"^\s+[✗x] (?P<problem>.+)$")


def parse_check_output(text: str) -> dict[str, dict[str, Any]]:
    """Parse repos_sync.py --check report lines into {label: {ok, problems}}."""
    checks: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        m = _CHECK_LINE.match(line)
        if m:
            current = {"ok": m.group("status") == "OK", "problems": []}
            checks[m.group("label")] = current
            continue
        m = _PROBLEM_LINE.match(line)
        if m and current is not None:
            current["problems"].append(m.group("problem"))
    return checks


def run() -> dict[str, Any]:
    script = PYAUTO_ROOT / "PyAutoMind" / "scripts" / "repos_sync.py"
    result: dict[str, Any]
    if not script.is_file():
        result = {"available": False, "reason": f"missing {script}", "checks": {}}
    else:
        proc = subprocess.run(
            [sys.executable, str(script), "--check", "--root", str(PYAUTO_ROOT)],
            capture_output=True,
            text=True,
        )
        checks = parse_check_output(proc.stdout)
        if not checks:
            # The script ran but produced nothing parseable — surface that
            # loudly rather than reporting a hollow green.
            result = {
                "available": False,
                "reason": f"unparseable output (exit {proc.returncode}): "
                          f"{(proc.stderr or proc.stdout).strip()[:200]}",
                "checks": {},
            }
        else:
            result = {
                "available": True,
                "checks": checks,
                "problem_count": sum(len(c["problems"]) for c in checks.values()),
            }
    sys.path.insert(0, str(HEART_HOME))
    from heart import state

    state.atomic_write_json(HEART_STATE_DIR / "manifest_drift.json", result)
    return result


def main(argv: list[str]) -> int:
    result = run()
    sys.path.insert(0, str(HEART_HOME))
    from heart.heart_color import c_ok, c_warn, c_info, c_meta, glyph_ok, glyph_warn

    if not result["available"]:
        print(f"{glyph_warn()} {c_info('manifest_drift')} {c_meta('skipped: ' + str(result.get('reason', '')))}")
        return 0
    n = result["problem_count"]
    surfaces = len(result["checks"])
    if n:
        print(f"{glyph_warn()} {c_info('manifest_drift')} {c_warn(f'{n} mismatch(es)')} {c_meta(f'({surfaces} surfaces vs repos.yaml)')}")
    else:
        print(f"{glyph_ok()} {c_info('manifest_drift')} {c_ok('identity in sync')} {c_meta(f'({surfaces} surfaces vs repos.yaml)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
