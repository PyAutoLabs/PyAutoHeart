"""heart/_workspace.py — where the workspace root is, asked in one place.

The answer belongs to the organism, not to this organ: it lives in
``PyAutoBrain/agents/_pyauto_root.py`` (mirrored by ``bin/_pyauto_root.sh``,
and by ``heart/_workspace.sh`` on this side). This module is the Heart's
door to it — every Heart module that needs the root imports from here, so the
rule can move once, in the Brain, and the Heart follows.

Heart used to roll its own answer in five places, and they disagreed: some
walked up from ``__file__`` and accepted the parent only when it was literally
named after this workspace's own directory, others fell back to a hard-coded
path under ``$HOME``. Both are instance facts — a task bundle is not named
after the workspace it belongs to, and a remote session's ``$HOME`` is not the
developer's — so both spellings resolved into directories that do not exist,
silently, because every consumer here degrades rather than raises.

**Two concepts, not one.** "Where am I" and "which tree does the Heart grade"
are different questions, and conflating them relaxes a release gate by accident:

``workspace_root()``  where this checkout is — siblings, config, the Brain, the
                      shared theme. Follows a ``.pyauto-root`` marker, so inside
                      a task bundle it is the bundle. Right for locating things.
``canonical_root()``  the MAIN checkout — the tree health grades. Never a task
                      bundle: a bundle's repos are git worktrees OF the
                      canonical repos, so git itself names the main checkout
                      (``rev-parse --git-common-dir``), whatever the cwd.
``wt_root()``         where task worktrees live — beside the canonical root,
                      never beside a bundle.

Health is a release gate, and the documented way to grade a branch is to opt in
with ``$PYAUTO_ROOT``, which still wins over everything below. Without that
split, running the Heart from the bundle you happen to be working in graded the
bundle — the more flattering answer, reached by cwd alone.

**No hard dependency on PyAutoBrain.** The Heart runs with no Brain in reach —
a standalone ``pip install pyauto-heart``, a single-repo session — which is why
``dashboard.theme()`` degrades instead of importing unconditionally. So does
this: when the Brain's resolver cannot be found, the rule below is applied
locally and the reason string says so, rather than a wrong root passing for a
right one. The local copy is deliberate duplication of a documented rule, kept
as short as it can be; the Brain's module is the one that explains it.

Stdlib only; the Brain module is loaded by file path rather than by pushing a
directory onto ``sys.path``, so importing the Heart never changes what any
other ``import`` in this process resolves to.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

__all__ = [
    "workspace_root",
    "workspace_root_reason",
    "canonical_root",
    "canonical_root_reason",
    "wt_root",
    "ROOT_MARKER",
]

HEART_HOME = Path(__file__).resolve().parents[1]

# Mirrors PyAutoBrain/agents/_pyauto_root.py. Only the degraded path below
# reads these; when the Brain is in reach its own values are used.
ROOT_MARKER = ".pyauto-root"
_SIBLING_ORGANS = (
    "PyAutoMind",
    "PyAutoCortex",
    "PyAutoMemory",
    "PyAutoHeart",
    "PyAutoHands",
    "PyAutoNerves",
    "PyAutoGut",
)

_DEGRADED = " (no PyAutoBrain in reach)"

_shared_cache: object | None = None


def _shared():
    """The Brain's resolver module, or None when no Brain checkout is in reach.

    Candidate order mirrors ``dashboard.theme()``: the explicit ``$PYAUTO_BRAIN``
    first, then a Brain beside this checkout (where CI puts it — heart-tests.yml
    and heart-health.yml check PyAutoBrain out next to PyAutoHeart), then a
    vendored one inside it.
    """
    global _shared_cache
    if _shared_cache is not None:
        return _shared_cache or None
    for cand in (
        os.environ.get("PYAUTO_BRAIN"),
        HEART_HOME.parent / "PyAutoBrain",
        HEART_HOME / "PyAutoBrain",
    ):
        if not cand:
            continue
        module_path = Path(cand) / "agents" / "_pyauto_root.py"
        if not module_path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "_pyauto_root", module_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:  # a broken/partial checkout must not break the Heart
            continue
        if not callable(getattr(module, "workspace_root_reason", None)):
            continue  # a resolver older than this API: degrade, do not raise
        _shared_cache = module
        return module
    _shared_cache = False
    return None


def _marked_root(start: Path) -> Path | None:
    """The nearest ANCESTOR of `start` holding the marker, or None."""
    for candidate in start.parents:
        if (candidate / ROOT_MARKER).is_file():
            return candidate
    return None


def workspace_root_reason() -> tuple[Path, str]:
    """``(root, why)`` — the workspace root and the rule that produced it.

    Callers that degrade should report the reason: "resolved to a directory
    that does not exist" must not read the same as "resolved fine, found
    nothing".
    """
    shared = _shared()
    if shared is not None:
        return shared.workspace_root_reason()
    # Degraded: the same order as the Brain's module, applied here. The
    # environment override comes first, so an operator's word still wins.
    env = os.environ.get("PYAUTO_ROOT")
    if env:
        if (Path(env) / ROOT_MARKER).is_file():
            return Path(env), "PYAUTO_ROOT" + _DEGRADED
        return (
            Path(env),
            f"PYAUTO_ROOT (unverified - no {ROOT_MARKER} marker)" + _DEGRADED,
        )
    marked = _marked_root(HEART_HOME)
    if marked is not None:
        return marked, f"{ROOT_MARKER} marker" + _DEGRADED
    parent = HEART_HOME.parent
    if any((parent / organ).is_dir() for organ in _SIBLING_ORGANS):
        return parent, "beside this checkout" + _DEGRADED
    return parent, "unverified (no sibling organ beside this checkout)" + _DEGRADED


def workspace_root() -> Path:
    """The workspace root — the directory holding the organ checkouts."""
    return Path(workspace_root_reason()[0])


_canonical_cache: tuple[Path, str] | None = None


def _canonical_from_git(start: Path) -> Path | None:
    """The main checkout's workspace root, as git reports it, or None.

    A task bundle's repos are linked git worktrees of the canonical repos, and
    a linked worktree's *common* dir is the main repo's ``.git`` — so git
    already knows where the main checkout is, and says so from inside a bundle
    as readily as from the canonical tree. From the canonical checkout the
    answer comes back relative (``.git``), which resolves to the same place.

    ``<root>/<repo>/.git`` -> ``<root>``.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # no git at all
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = start / common
    return common.resolve().parent.parent


def canonical_root_reason() -> tuple[Path, str]:
    """``(root, why)`` — the MAIN checkout's workspace root, for grading.

    ``$PYAUTO_ROOT`` first, so the documented "grade this branch" opt-in still
    works exactly as it does today; then git; then the workspace root as a last
    resort, with a reason that says the main checkout could not be named.
    """
    global _canonical_cache
    env = os.environ.get("PYAUTO_ROOT")
    if env:
        return Path(env), "PYAUTO_ROOT"
    if _canonical_cache is not None:
        return _canonical_cache
    from_git = _canonical_from_git(HEART_HOME)
    if from_git is not None:
        _canonical_cache = (from_git, "main checkout (git --git-common-dir)")
    else:
        root, why = workspace_root_reason()
        _canonical_cache = (root, f"workspace root — git could not name the main checkout ({why})")
    return _canonical_cache


def canonical_root() -> Path:
    """The MAIN checkout's workspace root — the tree the Heart grades."""
    return Path(canonical_root_reason()[0])


def wt_root() -> Path:
    """Where task worktrees live: beside the CANONICAL root, suffixed ``-wt``.

    Never beside a task bundle — a bundle has no ``-wt`` sibling of its own,
    and the drift check that reads this compares what is on disk against the
    Mind's claims, which are claims on the one worktree area.
    """
    env = os.environ.get("PYAUTO_WT_ROOT")
    if env:
        return Path(env)
    root = canonical_root()
    return root.parent / f"{root.name}-wt"
