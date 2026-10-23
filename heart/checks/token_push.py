"""Can the release identity push to the five release libraries?

The PyAutoHands release job tags each library as the org-owned
``pyautolabs-bot`` GitHub App. This check proves that credential BEFORE a
release needs it: ``.github/workflows/token_push_check.yml`` mints an App
token, installs it as git's credential helper (``gh auth setup-git``) and runs
this module, which for each library does

- ``git ls-remote`` (the repo answers), and
- ``git push --dry-run <url> HEAD:refs/heads/token-push-check-dryrun`` — the
  receive-pack handshake GitHub only grants to a writer. ``--dry-run`` sends
  no pack and creates no ref, so nothing is ever pushed.

The result is the committed sidecar ``state/token_push.json``, which
``state.aggregate`` folds into the snapshot as ``token_push`` and
``readiness`` grades (a refused push is RED; a missing or aged sidecar is an
evidence gap). Read-only on every foreign repo; never prints a credential.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

HEART_HOME = Path(__file__).resolve().parents[2]
SIDECAR = HEART_HOME / "state" / "token_push.json"
SCHEMA = "token_push/1"
PROBE_REF = "refs/heads/token-push-check-dryrun"
# Older than this, a passing sidecar no longer vouches for the credential.
STALE_HOURS = 48

Runner = Callable[[Sequence[str], str], "subprocess.CompletedProcess[str]"]


# Repo identity comes from the declared config surface (config/repos.yaml),
# never from literals — the tenant firewall keeps instance facts out of organ
# code. Imported lazily: readiness imports this module.
def release_repos() -> list[str]:
    """The release-gating libraries (the release job's library set)."""
    from heart.readiness import load_release_gate_names

    return load_release_gate_names()


def repo_owners() -> dict:
    """{repo name: GitHub owner}, read the way the dashboard reads it."""
    from heart.dashboard import _repo_owners

    return _repo_owners()


def _run(cmd: Sequence[str], cwd: str) -> "subprocess.CompletedProcess[str]":
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(list(cmd), cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=60)


def _tail(proc: "subprocess.CompletedProcess[str]") -> str:
    lines = [ln.strip() for ln in (proc.stderr or proc.stdout or "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else f"exit {proc.returncode}"


def probe(repos: Sequence[str] | None = None, run: Runner = _run,
          owners: dict | None = None) -> dict[str, dict[str, Any]]:
    """{repo: {ls_remote, push, error}} for each repo; never raises."""
    repos = list(repos) if repos is not None else release_repos()
    owners = owners if owners is not None else repo_owners()
    out: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="token-push-") as tmp:
        # A throwaway repo with one empty commit: HEAD must name a commit for
        # the dry-run refspec, and nothing about it ever leaves this machine.
        for cmd in (["git", "init", "-q"],
                    ["git", "-c", "user.name=token-push-check",
                     "-c", "user.email=token-push-check@invalid",
                     "commit", "-q", "--allow-empty", "-m", "token push probe"]):
            run(cmd, tmp)
        for repo in repos:
            row: dict[str, Any] = {"ls_remote": False, "push": False, "error": None}
            owner = owners.get(repo)
            if not owner:
                row["error"] = "no owner for this repo in config/repos.yaml"
                out[repo] = row
                continue
            url = f"https://github.com/{owner}/{repo}.git"
            try:
                ls = run(["git", "ls-remote", "--heads", url, "main"], tmp)
                row["ls_remote"] = ls.returncode == 0
                if ls.returncode:
                    row["error"] = "ls-remote: " + _tail(ls)
                push = run(["git", "push", "--dry-run", url, f"HEAD:{PROBE_REF}"], tmp)
                row["push"] = push.returncode == 0
                if push.returncode:
                    row["error"] = "push --dry-run: " + _tail(push)
            except (OSError, subprocess.TimeoutExpired) as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"[:300]
            out[repo] = row
    return out


def build(results: dict[str, dict[str, Any]], identity: str, run_url: str = "",
          now: datetime.datetime | None = None) -> dict[str, Any]:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {
        "schema": SCHEMA,
        "checked_at": now.isoformat(timespec="seconds"),
        "identity": identity,
        "run_url": run_url,
        "repos": results,
    }


def load(path: Path = SIDECAR) -> dict[str, Any]:
    """The committed sidecar, or {} when absent/unreadable."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=SIDECAR)
    ap.add_argument("--identity", default=os.environ.get("APP_SLUG") or "pyautolabs-bot")
    ap.add_argument("--print-repos", action="store_true",
                    help="print the comma-separated repo list (the App token scope) and exit")
    ns = ap.parse_args(argv)
    if ns.print_repos:
        print(",".join(release_repos()))
        return 0
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
                   f"{os.environ['GITHUB_RUN_ID']}")
    payload = build(probe(), ns.identity, run_url)
    ns.out.parent.mkdir(parents=True, exist_ok=True)
    ns.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    for repo, row in payload["repos"].items():
        print(f"{repo}: ls_remote={row['ls_remote']} push={row['push']}"
              + (f" ({row['error']})" if row["error"] else ""))
    # Exit 0 either way: the sidecar is the result and readiness grades it; a
    # failing job would leave no sidecar, which reads as "never checked".
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
