"""tests/test_ci_status_script.py — the shell leg's HEAD-sha resolution.

`ci_head_sha` is the one piece of `ci_status.sh` with a real branch in it: it
prefers `gh` and falls back to an anonymous `git ls-remote` so a mobile/cloud
session with no `gh` still records a sha. Exercised the same way
`test_verify_install_script.py` drives its helpers — source the file, call the
one function, and put stub executables first on `PATH` so nothing here touches
the network or a real `gh`.
"""

import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "heart" / "checks" / "ci_status.sh"

GH_SHA = "1111111111111111111111111111111111111111"
LS_REMOTE_SHA = "2222222222222222222222222222222222222222"


def write_stub(bin_dir: Path, name: str, body: str) -> None:
    """Drop an executable stub named `name` into `bin_dir`."""
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def call_ci_head_sha(bin_dir: Path, repo: str = "PyAutoLabs/PyAutoFit", env=None):
    """Source ci_status.sh in a shell whose PATH starts with the stubs."""
    environ = {
        # A PATH holding only the stub dir plus the system dirs the script's own
        # `source` line needs. `gh` is absent unless a stub provides it, which is
        # exactly the mobile-session condition under test.
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(bin_dir),
        **(env or {}),
    }
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" >/dev/null 2>&1; ci_head_sha "$2"',
            "ci_head_sha",
            str(SCRIPT),
            repo,
        ],
        capture_output=True,
        text=True,
        env=environ,
    )


@pytest.fixture
def bin_dir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


def test_prefers_gh_when_available(bin_dir):
    """With `gh` working, its sha wins and the fallback never runs."""
    write_stub(bin_dir, "gh", f'echo "{GH_SHA}"\n')
    # A `git` that would fail the assertion if it were ever consulted.
    write_stub(bin_dir, "git", f'echo "{LS_REMOTE_SHA}\trefs/heads/main"\n')

    result = call_ci_head_sha(bin_dir)

    assert result.stdout == GH_SHA


def test_falls_back_to_ls_remote_when_gh_absent(bin_dir):
    """The mobile case: no `gh` on PATH at all, so ls-remote supplies the sha."""
    write_stub(bin_dir, "git", f'echo "{LS_REMOTE_SHA}\trefs/heads/main"\n')

    result = call_ci_head_sha(bin_dir)

    assert result.stdout == LS_REMOTE_SHA


def test_falls_back_when_gh_present_but_fails(bin_dir):
    """`gh` installed but unauthenticated/erroring is still a fallback case."""
    write_stub(bin_dir, "gh", 'echo "gh: not authenticated" >&2\nexit 1\n')
    write_stub(bin_dir, "git", f'echo "{LS_REMOTE_SHA}\trefs/heads/main"\n')

    result = call_ci_head_sha(bin_dir)

    assert result.stdout == LS_REMOTE_SHA


def test_empty_when_both_sources_fail(bin_dir):
    """Never fabricate a sha — an empty string is the honest answer."""
    write_stub(bin_dir, "gh", "exit 1\n")
    write_stub(bin_dir, "git", "exit 128\n")

    result = call_ci_head_sha(bin_dir)

    assert result.stdout == ""


def test_empty_when_ls_remote_prints_nothing(bin_dir):
    """A zero-exit ls-remote with no matching ref must not yield a blank sha."""
    write_stub(bin_dir, "git", "exit 0\n")

    result = call_ci_head_sha(bin_dir)

    assert result.stdout == ""


def test_stalled_ls_remote_is_bounded(bin_dir):
    """A hung fallback is capped by `timeout`, so it cannot eat the tick budget."""
    write_stub(bin_dir, "git", "sleep 30\n")

    result = call_ci_head_sha(bin_dir, env={"HEART_LS_REMOTE_TIMEOUT": "1"})

    assert result.stdout == ""


def test_fallback_skipped_when_no_timeout_binary(bin_dir):
    """No `timeout`/`gtimeout` (stock macOS): skip rather than run unbounded.

    A stalled ls-remote inside the <30s tick is a worse failure than the empty
    sha that is already today's answer, so the fallback is not attempted at all.
    """
    write_stub(bin_dir, "git", f'echo "{LS_REMOTE_SHA}\trefs/heads/main"\n')

    result = call_ci_head_sha(bin_dir, env={"HEART_TIMEOUT_BIN": ""})

    assert result.stdout == ""


def test_takes_first_line_only(bin_dir):
    """ls-remote can print more than one ref; only the first field of the first."""
    write_stub(
        bin_dir,
        "git",
        f'printf "{LS_REMOTE_SHA}\\trefs/heads/main\\n3333333333333333333333333333333333333333\\trefs/heads/mainline\\n"\n',
    )

    result = call_ci_head_sha(bin_dir)

    assert result.stdout == LS_REMOTE_SHA
