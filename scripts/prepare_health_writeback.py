"""Prepare generated Heart Health output for a normal push to fetched main."""

import subprocess
from pathlib import Path

BEGIN = b"<!-- heart:begin -->"
END = b"<!-- heart:end -->"


def git(*args):
    return subprocess.check_output(["git", *args])


def board_parts(readme):
    """Return the text around the managed strip, including a first-run slot."""
    if BEGIN not in readme and END not in readme:
        offset = 0
        for line in readme.splitlines(keepends=True):
            offset += len(line)
            if line.startswith(b"# "):
                return readme[:offset].rstrip(b"\r\n") + b"\n\n", None, b"\n" + readme[offset:]
    if readme.count(BEGIN) == readme.count(END) == 1:
        prefix, _, tail = readme.partition(BEGIN)
        block, marker, suffix = tail.partition(END)
        if marker:
            return prefix, block, suffix
    raise SystemExit("Invalid README board markers or missing title. Publication stopped.")


def main():
    tip = git("rev-parse", "FETCH_HEAD").decode().strip()
    subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", tip], check=True)
    changed = git("diff", "--name-only", "-z", "HEAD", tip).split(b"\0")
    if any(name not in (b"", b"README.md") for name in changed):
        raise SystemExit("Collector inputs may have changed. Dispatch Heart Health on current main.")
    if not git("ls-tree", tip, "README.md").startswith(b"100644 blob "):
        raise SystemExit("README is missing or changed type. Publication stopped.")
    original = board_parts(git("show", "HEAD:README.md"))
    prefix, block, suffix = board_parts(git("show", tip + ":README.md"))
    if original[1] != block:
        raise SystemExit("Another writer changed the board strip. Dispatch Heart Health on current main.")
    strip = Path("readme_strip.md").read_bytes().strip()
    if not strip or BEGIN in strip or END in strip:
        raise SystemExit("Rendered README board strip is empty or contains a marker.")
    # Git carries the collector's unstaged timing appends across this checkout.
    # Their committed baselines are identical at both commits (checked above).
    git("switch", "--detach", tip)
    Path("README.md").write_bytes(prefix + BEGIN + b"\n" + strip + b"\n" + END + suffix)


if __name__ == "__main__":
    main()
