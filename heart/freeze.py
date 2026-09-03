"""heart/freeze.py — the release-validation freeze flag.

A release validation is a window in which the library ``main`` branches must
not move: a merge landing mid-validation invalidates the evidence and restales
the whole rehearsal (measured at ~75 minutes, 2026-08-29). Heart already knows
that window exists — it is the organ that ingests the evidence — but nothing
*said so* to the surfaces that merge things. This module is that sentence:

    FROZEN: release validation until 2026-09-03T19:30:00+00:00

The flag lives at ``$HEART_STATE_DIR/freeze.json`` (``~/.pyauto-heart/`` by
default), beside the other Heart sidecars, and carries four fields::

    {"reason": "release validation",
     "set_at": "2026-09-03T18:00:00+00:00",
     "until":  "2026-09-03T19:30:00+00:00",
     "set_by": "release-validate"}

Three properties are deliberate:

- **Heart owns it, everything else reads it.** Only Heart's own CLI writes the
  file. PyAutoBrain's ``vitals`` faculty, ``/prm`` and the batch conductor read
  it and say what it says; none of them writes.
- **It expires.** ``until`` is mandatory — there is no way to set a freeze that
  outlives its window by accident. Past ``until`` the flag reads as *expired*,
  which is treated exactly like clear by every consumer, and ``--show`` says
  "expired" so a forgotten set is visible rather than invisible.
- **It does not touch the readiness verdict.** ``heart/readiness.py`` is
  untouched by this module: a freeze is not a health problem, and folding it
  into the verdict would make every ship and release gate in the organism
  block on it. It is advice — with teeth in exactly one place, ``/prm``'s
  merge gate for library repos.

Who sets and clears it (the call sites, spelled once here and pointed at from
``REFERENCE.md``):

- ``PyAutoHands/skills/pre_build/pre_build.md`` sets it before dispatching the
  release workflow — that is the moment the window opens.
- ``pyauto-heart validate --ingest`` clears it: the ingest of the validation
  evidence *is* the end of the window.
- ``PyAutoHeart/skills/review_release/review_release.md`` checks it while
  triaging the run and clears a freeze the ingest did not.
- Expiry clears it on its own if none of the above happens.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from typing import Any

from heart import state

FREEZE_FILE = state.HEART_STATE_DIR / "freeze.json"

#: ``--until`` shorthand: ``90m`` / ``2h`` / ``1d``, relative to now. Spelled
#: out because a wall-clock timestamp is the thing people get wrong at 2am,
#: and a duration cannot be off by a timezone.
DURATION_RE = re.compile(r"^\+?(\d+)\s*([mhd])$", re.I)
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}

#: The three readings. ``expired`` is a *clear* freeze that still has a file:
#: consumers treat it as clear, and only ``--show`` distinguishes it, so that a
#: forgotten set is reported rather than silently discarded.
CLEAR, ACTIVE, EXPIRED = "clear", "active", "expired"

#: ``--show`` exit code while a freeze is active, so a shell gate is one call
#: and no JSON parse. Not 1: that is "the command failed".
RC_FROZEN = 3


def _now(now: datetime.datetime | None = None) -> datetime.datetime:
    return now or datetime.datetime.now(datetime.timezone.utc)


def _iso(ts: datetime.datetime) -> str:
    return ts.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat()


def parse_until(value: str, now: datetime.datetime | None = None) -> datetime.datetime:
    """``90m``/``2h``/``1d`` or an ISO-8601 timestamp → an aware datetime.

    A naive timestamp is read as UTC — Heart's sidecars are UTC throughout, and
    guessing the local zone here would make the same string mean two things on
    two machines.
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("--until is required: a freeze with no expiry never clears")
    m = DURATION_RE.match(v)
    if m:
        return _now(now) + datetime.timedelta(**{_UNITS[m.group(2).lower()]: int(m.group(1))})
    try:
        ts = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"--until: {v!r} is neither a duration (90m / 2h / 1d) nor an "
            "ISO-8601 timestamp"
        ) from e
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def _read_file() -> dict[str, Any] | None:
    if not FREEZE_FILE.is_file():
        return None
    try:
        data = json.loads(FREEZE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read(now: datetime.datetime | None = None) -> dict[str, Any]:
    """The current reading: ``{"state": clear|active|expired, ...fields}``.

    Never raises and never writes — an unreadable or malformed file reads as
    clear, because a freeze nobody can parse must not be able to block a merge.
    """
    data = _read_file()
    if not data:
        return {"state": CLEAR}
    until = data.get("until")
    try:
        expires = parse_until(str(until), now=now) if until else None
    except ValueError:
        expires = None
    if expires is None:
        # A file with no parseable expiry is not a licence to freeze forever.
        return {"state": CLEAR, "malformed": True}
    out = {
        "state": ACTIVE if _now(now) < expires else EXPIRED,
        "reason": str(data.get("reason") or "unspecified"),
        "set_at": str(data.get("set_at") or ""),
        "until": _iso(expires),
        "set_by": str(data.get("set_by") or ""),
    }
    return out


def is_frozen(now: datetime.datetime | None = None) -> bool:
    """True only while a freeze is *active* — expired reads as thawed."""
    return read(now=now)["state"] == ACTIVE


def render_line(rec: dict[str, Any]) -> str:
    """The one wording, shared by every surface that shows the flag.

    ``FROZEN: <reason> until <ts>`` while active, ``""`` otherwise — so a
    caller can `if line:` rather than re-deciding what counts as frozen.
    """
    if rec.get("state") != ACTIVE:
        return ""
    return f"FROZEN: {rec['reason']} until {rec['until']}"


def set_freeze(reason: str, until: str, set_by: str = "",
               now: datetime.datetime | None = None) -> dict[str, Any]:
    """Write the flag. ``until`` is mandatory (see ``parse_until``)."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("--set needs a reason: an unexplained freeze cannot be judged")
    expires = parse_until(until, now=now)
    ts = _now(now)
    if expires <= ts:
        raise ValueError(f"--until {until!r} is already in the past")
    record = {
        "reason": reason,
        "set_at": _iso(ts),
        "until": _iso(expires),
        "set_by": set_by or os.environ.get("USER") or "unknown",
    }
    state.atomic_write_json(FREEZE_FILE, record)
    return read(now=now)


def clear(now: datetime.datetime | None = None) -> dict[str, Any]:
    """Remove the flag; return what was there (``state`` as it read before)."""
    was = read(now=now)
    try:
        FREEZE_FILE.unlink()
    except FileNotFoundError:
        pass
    return was


# ------------------------------------------------------------------- CLI ---
def _render(rec: dict[str, Any]) -> list[str]:
    st = rec.get("state")
    if st == ACTIVE:
        return [render_line(rec),
                f"  set {rec['set_at']} by {rec['set_by'] or 'unknown'}"]
    if st == EXPIRED:
        return [f"expired: {rec['reason']} — expiry {rec['until']} has passed "
                "(reads as clear; `freeze --clear` removes the file)"]
    return ["not frozen"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pyauto-heart freeze",
        description="The release-validation freeze flag: library mains should "
                    "not move while it is set.",
    )
    ap.add_argument("--set", dest="set_reason", metavar="REASON",
                    help="set the flag with this reason (needs --until)")
    ap.add_argument("--until", default="",
                    help="expiry: a duration (90m / 2h / 1d) or an ISO-8601 "
                         "timestamp. Mandatory with --set.")
    ap.add_argument("--set-by", default="",
                    help="who set it (default: $USER)")
    ap.add_argument("--clear", action="store_true", help="remove the flag")
    ap.add_argument("--show", action="store_true",
                    help="print the current reading (the default action)")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="print the reading as JSON")
    ns = ap.parse_args(argv)

    if ns.set_reason and ns.clear:
        print("freeze: --set and --clear are opposites; pick one", file=sys.stderr)
        return 2

    try:
        if ns.set_reason:
            rec = set_freeze(ns.set_reason, ns.until, set_by=ns.set_by)
        elif ns.clear:
            was = clear()
            if ns.as_json:
                json.dump({"cleared": was}, sys.stdout, indent=2, sort_keys=True)
                sys.stdout.write("\n")
            else:
                print(f"cleared: {was['reason']}" if was["state"] != CLEAR
                      else "nothing to clear")
            return 0
        else:
            rec = read()
    except ValueError as e:
        print(f"freeze: {e}", file=sys.stderr)
        return 2

    if ns.as_json:
        json.dump(rec, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        for line in _render(rec):
            print(line)
    # A *read* reports RC_FROZEN while active, so `pyauto-heart freeze --show`
    # is a one-call shell gate. A --set always exits 0: it is a successful
    # command, and a driver running under `set -e` must not abort on the freeze
    # it just took out itself.
    if ns.set_reason:
        return 0
    return RC_FROZEN if rec.get("state") == ACTIVE else 0


if __name__ == "__main__":
    sys.exit(main())
