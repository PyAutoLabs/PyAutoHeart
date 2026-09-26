"""tests/test_alert.py — the transition-only push alert (PyAutoBrain#416).

Hermetic: urllib.request.urlopen is replaced, so nothing leaves the machine.
The load-bearing properties are that a status CHANGE posts exactly once, a
steady status or an unknown previous posts nothing, and no failure mode —
network, missing secret, garbage previous feed — ever raises or exits non-zero.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from heart import alert

ARROW = "→"


def _state(status: str, **kw) -> dict:
    doc = {
        "schema_version": 1, "organ": "heart", "repo": "PyAutoHeart",
        "status": status, "headline": kw.get("headline", f"{status} headline"),
        "updated": "2026-09-26T05:31:00Z",
        "pages_url": "https://pyautolabs.github.io/PyAutoHeart/",
        "items": kw.get("items", []),
    }
    return doc


@pytest.mark.parametrize("prev,new,expected", [
    (None, "red", None),
    ("green", "green", None),
    ("green", "red", f"green{ARROW}red"),
    ("red", "green", f"red{ARROW}green"),
    ("stale", "green", f"stale{ARROW}green"),
    ("green", "stale", f"green{ARROW}stale"),
    ("bogus", "red", None),
    ("grey", "red", None),
    ("green", "grey", None),
])
def test_transition_table(prev, new, expected):
    assert alert.transition(prev, new) == expected


def test_render_message_shape():
    items = [{"severity": "red", "text": f"repo{i}: CI failure",
              "url": f"https://github.com/o/r{i}", "prompt": "/bug x"} for i in range(8)]
    title, body, priority, tags = alert.render_message(
        f"green{ARROW}red", _state("red", headline="PyAutoFit: CI failure", items=items))
    assert title == f"Heart: GREEN {ARROW} RED"
    assert body.splitlines()[0] == "PyAutoFit: CI failure"
    item_lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert len(item_lines) == alert.MAX_ITEMS
    assert "https://github.com/o/r0" in item_lines[0]
    assert priority == 5
    assert tags.isascii() and "red" in tags.split(",")
    _, _, p2, _ = alert.render_message(f"red{ARROW}green", _state("green"))
    assert p2 == 3


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def posts(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return _Resp()

    monkeypatch.setattr(alert.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("HEART_NTFY_URL", raising=False)
    return calls


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc) if not isinstance(doc, str) else doc)
    return str(p)


def test_cli_green_to_red_posts_once(tmp_path, posts, capsys):
    prev = _write(tmp_path, "prev.json", _state("green"))
    cur = _write(tmp_path, "cur.json", _state("red", headline="PyAutoFit: CI failure"))
    rc = alert.main(["--previous", prev, "--current", cur, "--url", "https://ntfy.example/t"])
    assert rc == 0
    assert len(posts) == 1
    req = posts[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://ntfy.example/t"
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["title"] == "Heart: GREEN -> RED"
    assert headers["priority"] == "5"
    assert "red" in headers["tags"]
    assert b"PyAutoFit: CI failure" in req.data
    assert f"alert: green{ARROW}red sent" in capsys.readouterr().out


def test_cli_url_defaults_to_env(tmp_path, posts, monkeypatch):
    monkeypatch.setenv("HEART_NTFY_URL", "https://ntfy.example/env")
    prev = _write(tmp_path, "prev.json", _state("red"))
    cur = _write(tmp_path, "cur.json", _state("green"))
    assert alert.main(["--previous", prev, "--current", cur]) == 0
    assert [r.full_url for r in posts] == ["https://ntfy.example/env"]


def test_cli_steady_status_posts_nothing(tmp_path, posts, capsys):
    prev = _write(tmp_path, "prev.json", _state("green"))
    cur = _write(tmp_path, "cur.json", _state("green"))
    assert alert.main(["--previous", prev, "--current", cur, "--url", "https://x/t"]) == 0
    assert posts == []
    assert f"alert: none (green{ARROW}green)" in capsys.readouterr().out


@pytest.mark.parametrize("prev_doc", [None, "{}", "not json", "[1, 2]"])
def test_cli_missing_or_invalid_previous_posts_nothing(tmp_path, posts, capsys, prev_doc):
    prev = (str(tmp_path / "absent.json") if prev_doc is None
            else _write(tmp_path, "prev.json", prev_doc))
    cur = _write(tmp_path, "cur.json", _state("red"))
    assert alert.main(["--previous", prev, "--current", cur, "--url", "https://x/t"]) == 0
    assert posts == []
    assert "alert: none" in capsys.readouterr().out


def test_cli_without_url_skips(tmp_path, posts, capsys):
    prev = _write(tmp_path, "prev.json", _state("green"))
    cur = _write(tmp_path, "cur.json", _state("red"))
    assert alert.main(["--previous", prev, "--current", cur]) == 0
    assert posts == []
    assert f"alert: green{ARROW}red skipped (no HEART_NTFY_URL)" in capsys.readouterr().out


def test_network_error_never_raises(tmp_path, monkeypatch, capsys):
    def boom(req, timeout=None):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(alert.urllib.request, "urlopen", boom)
    assert alert.notify("https://x/t", "Heart: A", "b", 3, "heart") is False
    prev = _write(tmp_path, "prev.json", _state("green"))
    cur = _write(tmp_path, "cur.json", _state("red"))
    assert alert.main(["--previous", prev, "--current", cur, "--url", "https://x/t"]) == 0
    out = capsys.readouterr().out
    assert f"alert: green{ARROW}red failed (URLError" in out


def test_notify_success_returns_true(posts):
    assert alert.notify("https://x/t", f"Heart: GREEN {ARROW} RED", "body", 5, "heart,red")
    assert len(posts) == 1
