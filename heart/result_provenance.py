"""Reject foreign runner paths before result shards become validation evidence.

PYAUTO_RESULT_ROOT overrides GITHUB_WORKSPACE for replay/local collection. With
neither declared, historical local reports retain their existing interpretation.
CI must validate before aggregation: rejecting a shard invalidates that run's
completeness; it must not be reinterpreted as an all-passing smaller run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def trusted_results(data: Any, source: Path, root: str | Path | None = None) -> bool:
    root = root or os.environ.get("PYAUTO_RESULT_ROOT") or os.environ.get("GITHUB_WORKSPACE")
    if not root or not isinstance(data, dict) or "results" not in data:
        return True
    root_path = Path(root).resolve()
    results = data["results"]
    if not isinstance(results, list):
        print(f"WARNING: rejected result shard {source}: malformed results", file=sys.stderr)
        return False
    valid = True
    for row in results:
        file = row.get("file") if isinstance(row, dict) else None
        path = Path(file) if isinstance(file, str) and file else None
        if (path is None or not path.is_absolute() or ".." in path.parts
                or not path.resolve().is_relative_to(root_path)):
            print(f"WARNING: rejected result shard {source}: foreign file path {file!r} "
                  f"(runner root {root_path})", file=sys.stderr)
            valid = False
    return valid


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args(argv)
    invalid = False
    for path in sorted(args.directory.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            print(f"WARNING: unreadable result shard {path}: {exc}", file=sys.stderr)
            invalid = True
            continue
        if not trusted_results(data, path, args.root):
            invalid = True
    return int(invalid)


if __name__ == "__main__":
    raise SystemExit(main())
