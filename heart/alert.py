"""heart/alert.py — a push notification when the Heart's status CHANGES.

The daily cloud job already keeps a ``[heart-health]`` issue open while
anything is degraded; that is a record, not a tap on the shoulder. This module
is the tap: it compares the previous published cockpit feed (``state.json`` on
Pages, PyAutoBrain#416) with the one just rendered and, only when the status
moved, POSTs one message to an ntfy topic.

Transition-only by design: a board that stays red for a week pages once, on
the day it went red, and once more on the day it recovers — never daily. Every
change between two KNOWN statuses counts, including stale↔green: an evidence
gap opening or closing is exactly the kind of drift a human wants to hear
about. An unknown previous (first run, a failed fetch, a grey/unparseable
feed) never alerts, so a Pages outage can not fire a false "→ red".

Observer boundary: this writes nothing; its one side effect is the POST to the
topic named by ``HEART_NTFY_URL`` (a repo secret — a private topic). Absent
that, it reports "skipped" and exits 0; alerting must never fail the job.

Usage:
    python -m heart.alert --previous prev.json --current _site/state.json [--url URL]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

KNOWN_STATUSES = ("green", "yellow", "red", "stale")
MAX_ITEMS = 5


def transition(previous_status: str | None, new_status: str) -> str | None:
    """``"green→red"`` when a known status changed, else None.

    None/unknown previous → None (first run never alerts); same status → None.
    A grey/unknown NEW status is also None: "we can not see" is not news.
    """
    if previous_status not in KNOWN_STATUSES or new_status not in KNOWN_STATUSES:
        return None
    if previous_status == new_status:
        return None
    return f"{previous_status}→{new_status}"


def render_message(kind: str, state: dict[str, Any]) -> tuple[str, str, int, str]:
    """(title, body, priority, tags) for a transition ``kind``.

    Priority 5 (urgent) when the Heart went red, 3 (default) otherwise. Tags
    are plain words (ntfy turns known ones into icons on the phone itself).
    """
    prev, _, new = kind.partition("→")
    title = f"Heart: {prev.upper()} → {new.upper()}"
    lines = [str(state.get("headline") or "")]
    for item in list(state.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        line = f"- [{item.get('severity', 'info')}] {item.get('text', '')}"
        if item.get("url"):
            line += f" {item['url']}"
        lines.append(line)
    if state.get("pages_url"):
        lines.append(str(state["pages_url"]))
    priority = 5 if new == "red" else 3
    tags = "heart,red" if new == "red" else f"heart,{new}"
    return title, "\n".join(lines), priority, tags


def _post(url: str, title: str, body: str, priority: int, tags: str,
          timeout: float) -> str | None:
    """POST one ntfy message; None on success, else a one-line reason.

    Headers must be latin-1, so the title's arrow is sent as ``->``; the body
    is UTF-8 and keeps it.
    """
    header_title = title.replace("\u2192", "->").encode("ascii", "replace").decode()
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST",
        headers={"Title": header_title, "Priority": str(priority), "Tags": tags},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(getattr(resp, "status", 200) or 200)
    except Exception as e:  # noqa: BLE001 — alerting must never break the job
        return f"{type(e).__name__}: {e}"
    return None if code < 400 else f"HTTP {code}"


def notify(url: str, title: str, body: str, priority: int, tags: str,
           timeout: float = 10) -> bool:
    """POST one ntfy message. True on success; prints a notice and returns
    False on any error — never raises."""
    reason = _post(url, title, body, priority, tags, timeout)
    if reason:
        print(f"notice: ntfy POST failed ({reason})")
    return reason is None


def _load_status(path: str | None) -> tuple[str | None, dict[str, Any]]:
    """(status, document) from a feed file; (None, {}) if unreadable/invalid."""
    if not path:
        return None, {}
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None, {}
    if not isinstance(doc, dict):
        return None, {}
    status = doc.get("status")
    return (status if isinstance(status, str) else None), doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m heart.alert")
    ap.add_argument("--previous", help="the previously published state.json")
    ap.add_argument("--current", required=True, help="the state.json just rendered")
    ap.add_argument("--url", default=None,
                    help="ntfy topic URL (default: $HEART_NTFY_URL)")
    ns = ap.parse_args(argv)

    prev, _ = _load_status(ns.previous)
    new, current = _load_status(ns.current)
    kind = transition(prev, new or "")
    if kind is None:
        print(f"alert: none ({prev or 'unknown'}→{new or 'unknown'})")
        return 0
    url = ns.url if ns.url is not None else os.environ.get("HEART_NTFY_URL", "")
    if not url:
        print(f"alert: {kind} skipped (no HEART_NTFY_URL)")
        return 0
    title, body, priority, tags = render_message(kind, current)
    reason = _post(url, title, body, priority, tags, timeout=10)
    print(f"alert: {kind} sent" if reason is None else f"alert: {kind} failed ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
