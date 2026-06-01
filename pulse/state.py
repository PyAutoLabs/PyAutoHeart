"""pulse/state.py — JSON cache I/O for Pulse state.

Each tick produces a set of per-repo + global JSONs under
~/.pyauto-pulse/. `aggregate()` reads them and emits a single
state.json snapshot. `load()` reads that snapshot for the status
renderer / `fix` command / external consumers.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Any

PULSE_STATE_DIR = Path(os.environ.get("PULSE_STATE_DIR", str(Path.home() / ".pyauto-pulse")))
PULSE_PER_REPO_DIR = PULSE_STATE_DIR / "per-repo"
PULSE_STATE_FILE = PULSE_STATE_DIR / "state.json"


def _ensure_dirs() -> None:
    PULSE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    PULSE_PER_REPO_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON to ``path`` atomically via tempfile + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _read_json_or_default(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def aggregate() -> dict[str, Any]:
    """Collapse per-repo JSON sidecars into one state.json snapshot."""
    _ensure_dirs()
    repos: dict[str, dict[str, Any]] = {}
    for entry in sorted(PULSE_PER_REPO_DIR.glob("*.json")):
        # Filenames: <name>.<check_kind>.json. Group by repo name.
        # e.g. PyAutoFit.repo_state.json, PyAutoFit.ci_status.json, ...
        parts = entry.name.split(".")
        if len(parts) < 3 or parts[-1] != "json":
            continue
        name = parts[0]
        check_kind = ".".join(parts[1:-1])
        data = _read_json_or_default(entry, {})
        repos.setdefault(name, {})[check_kind] = data

    snapshot = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "repos": repos,
        "worktree_drift": _read_json_or_default(PULSE_STATE_DIR / "worktree_drift.json", {}),
        "script_timing": _read_json_or_default(PULSE_STATE_DIR / "script_timing.json", {}),
        "test_run": _read_json_or_default(PULSE_STATE_DIR / "test_run.json", {}),
        "version_skew": _read_json_or_default(PULSE_STATE_DIR / "version_skew.json", {}),
    }
    atomic_write_json(PULSE_STATE_FILE, snapshot)
    return snapshot


def load() -> dict[str, Any] | None:
    """Return the aggregated state.json snapshot, or None if missing."""
    return _read_json_or_default(PULSE_STATE_FILE, None)


def age_seconds() -> float | None:
    """Seconds since the last `state.json` was written, or None if missing."""
    if not PULSE_STATE_FILE.is_file():
        return None
    mtime = PULSE_STATE_FILE.stat().st_mtime
    return datetime.datetime.now().timestamp() - mtime
