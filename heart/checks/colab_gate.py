#!/usr/bin/env python3
"""heart/checks/colab_gate.py — the Colab package-set gate behind verify_install check F.

Check F used to *emulate* Colab by running ``pip install autolens jax`` WITH
dependencies and then running the injected setup cell on top. That install is
the bug the gate exists to remove: it leaves the venv holding every declared
dependency (corner, optax, xxhash, blackjax, ...) before the setup cell ever
runs, so the setup cell's real ``pip install ... --no-deps`` can never be seen
to miss anything. A notebook that dies on Colab at the first post-fit plot
still passed check F.

This module rebuilds the environment from the package set Google actually ships
(``googlecolab/backend-info``'s ``pip-freeze.txt``) and then audits what the
``--no-deps`` bootstrap left behind.

Two subcommands, both designed to run with the *simulated venv's* interpreter
(``"$venv/bin/python" colab_gate.py ...``) so ``importlib.metadata`` and the
import probe see that venv and nothing else:

``seed``
    Runs BEFORE the setup cell. Resolves the with-deps closure of the PyAuto
    stack with ``pip install --dry-run --report -``, intersects it with the
    Colab manifest, and installs ONLY that intersection, at Colab's pinned
    versions, ``--no-deps``. Everything in the closure that Colab does not ship
    is deliberately left absent — that absence is what the gate measures.

``verify``
    Runs AFTER the setup cell has done its real ``--no-deps`` bootstrap and
    workspace clone. Walks the installed PyAuto distributions' declared
    requirements, AST-scans every ``import`` in their source, probes each
    third-party module for real, and constructs the four headline searches.

Exit codes: 0 pass, 1 fail, 2 tool error.

Dependencies: the standard library plus ``packaging`` (pip vendors it, but the
gate installs it into the venv explicitly). PyYAML is used for the optional
config file when importable and a minimal fallback parser covers its absence.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

MANIFEST_URL = (
    "https://raw.githubusercontent.com/googlecolab/backend-info/main/pip-freeze.txt"
)

CHECKS_DIR = Path(__file__).resolve().parent
SNAPSHOT_PATH = CHECKS_DIR / "colab_pip_freeze.snapshot.txt"
CONFIG_PATH = CHECKS_DIR.parent / "config" / "colab_gate.yaml"

#: The PyAuto distributions. Never seeded from Colab (Colab has none of them),
#: never import-probed (they are the thing under test), and the roots of the
#: requirement walk.
PYAUTO_PACKAGES = ("autonerves", "autofit", "autoarray", "autogalaxy", "autolens")

#: Distributions whose import name is not derivable from the distribution name.
#: Used only to decide whether a *missing* distribution (so one with no local
#: metadata to read the mapping from) is one the libraries actually import.
DIST_MODULE_ALIASES = {
    "scikit-learn": ["sklearn"],
    "scikit-image": ["skimage"],
    "pyyaml": ["yaml"],
    "pillow": ["PIL"],
    "opencv-python": ["cv2"],
    "nautilus-sampler": ["nautilus"],
    "timeout-decorator": ["timeout_decorator"],
    "zeus-mcmc": ["zeus"],
    "attrs": ["attr"],
    "protobuf": ["google"],
    "typing-extensions": ["typing_extensions"],
    "beautifulsoup4": ["bs4"],
    "python-dateutil": ["dateutil"],
    "astropy-iers-data": ["astropy_iers_data"],
}

SEARCH_CONSTRUCTORS = ("Emcee", "DynestyStatic", "Nautilus", "LBFGS")


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


def normalise(name: str) -> str:
    """PEP 503 normalisation: ``SQLAlchemy`` -> ``sqlalchemy``, ``_`` -> ``-``."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def parse_manifest(text: str) -> dict[str, str]:
    """Parse a ``pip freeze`` manifest into ``{normalised name: version}``.

    Only ``name==version`` pins are kept. Direct references (``name @ url``),
    VCS installs, editable installs, comments and blank lines are skipped —
    none of them names a version the gate can install by pin. Extras are
    stripped from the name.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if "@" in line or line.startswith(("git+", "hg+", "svn+", "bzr+")):
            continue
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        name = name.split("[", 1)[0].strip()
        version = version.strip()
        if not name or not version:
            continue
        out[normalise(name)] = version
    return out


def _fetch_live(url: str = MANIFEST_URL, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read().decode("utf-8", "replace")


def _snapshot_date(text: str) -> str | None:
    match = re.search(r"fetched\s+(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else None


def load_manifest(
    cache_path: Path | None,
    snapshot_path: Path | None = None,
    fetcher: Callable[[], str] | None = None,
) -> tuple[dict[str, str], str, str | None, list[str]]:
    """Load the Colab manifest, live -> cache -> vendored snapshot.

    Returns ``(packages, source, date, notes)`` where ``source`` is one of
    ``live``, ``cache`` or ``snapshot``. A live fetch refreshes the cache so a
    later offline run degrades to yesterday's real manifest rather than to the
    vendored snapshot, which ages with the repo.
    """
    snapshot_path = snapshot_path or SNAPSHOT_PATH
    fetcher = fetcher or _fetch_live
    notes: list[str] = []

    try:
        text = fetcher()
        packages = parse_manifest(text)
        if not packages:
            raise ValueError("live manifest parsed to zero pins")
    except Exception as exc:  # network, DNS, proxy, HTTP error, empty body
        notes.append(f"live fetch failed: {exc.__class__.__name__}: {exc}")
    else:
        if cache_path is not None:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(text)
            except OSError as exc:
                notes.append(f"cache write failed: {exc}")
        today = datetime.date.today().isoformat()
        return packages, "live", today, notes

    if cache_path is not None and cache_path.is_file():
        try:
            text = cache_path.read_text()
            packages = parse_manifest(text)
            if packages:
                date = datetime.date.fromtimestamp(
                    cache_path.stat().st_mtime
                ).isoformat()
                return packages, "cache", date, notes
            notes.append("cache parsed to zero pins")
        except OSError as exc:
            notes.append(f"cache read failed: {exc}")

    text = Path(snapshot_path).read_text()
    return parse_manifest(text), "snapshot", _snapshot_date(text), notes


# --------------------------------------------------------------------------
# pip
# --------------------------------------------------------------------------


def _pip(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True,
        text=True,
        **kwargs,
    )


def parse_dry_run_report(stdout: str) -> dict[str, str]:
    """Extract ``{normalised name: version}`` from a ``pip --report -`` payload.

    ``--quiet`` is not a guarantee of a clean stdout (warnings from the
    resolver reach it), so the JSON document is located rather than assumed to
    start at byte zero.
    """
    start = stdout.find("{")
    if start < 0:
        raise ValueError("no JSON object in pip --report output")
    report = json.loads(stdout[start:])
    out: dict[str, str] = {}
    for item in report.get("install", []):
        metadata = item.get("metadata") or {}
        name = metadata.get("name")
        version = metadata.get("version")
        if name:
            out[normalise(name)] = str(version or "")
    return out


def resolve_closure(targets: Sequence[str], index_args: Sequence[str]) -> dict[str, str]:
    """The full with-deps resolution of ``targets``, without installing it."""
    result = _pip(
        [
            "install",
            "--dry-run",
            "--quiet",
            "--report",
            "-",
            *index_args,
            *targets,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(
            "pip install --dry-run failed:\n" + (result.stderr or result.stdout)[-2000:]
        )
    return parse_dry_run_report(result.stdout)


def colab_intersection(
    closure: Iterable[str], manifest: dict[str, str]
) -> list[str]:
    """Closure names Colab also ships, PyAuto packages excluded, sorted."""
    pyauto = {normalise(p) for p in PYAUTO_PACKAGES}
    return sorted(
        name for name in set(closure) if name in manifest and name not in pyauto
    )


# --------------------------------------------------------------------------
# requirement walk
# --------------------------------------------------------------------------


def _marker_mentions_extra(marker: Any) -> bool:
    return bool(re.search(r"\bextra\b", str(marker)))


def _installed_version(name: str) -> str | None:
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _requirements(dist_name: str) -> list[str]:
    from importlib import metadata

    try:
        return list(metadata.distribution(dist_name).requires or [])
    except metadata.PackageNotFoundError:
        return []


def walk_requirements(
    roots: Sequence[str],
    manifest: dict[str, str],
    installed_version: Callable[[str], str | None] = _installed_version,
    requirements: Callable[[str], list[str]] = _requirements,
    install: Callable[[list[str]], tuple[bool, str]] | None = None,
    max_rounds: int = 8,
) -> dict[str, Any]:
    """Walk declared requirements from ``roots``, healing what Colab provides.

    An unmet requirement Colab ships is installed at Colab's pinned version
    (``--no-deps``) and the walk continues through it — that is what a user's
    Colab session already has. An unmet requirement Colab does NOT ship is a
    real hole in the ``--no-deps`` bootstrap and is recorded in
    ``missing_declared``. An installed version outside a declared specifier is
    a ``version_conflict`` (reported, never blocking: Colab pins what it pins).
    """
    from packaging.requirements import InvalidRequirement, Requirement

    missing: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, dict[str, Any]] = {}
    colab_installed: list[dict[str, str]] = []
    unparsed: list[str] = []
    seen: set[str] = set()
    rounds = 0

    queue = list(roots)
    while queue and rounds < max_rounds:
        rounds += 1
        to_install: list[str] = []
        pending: list[tuple[str, str]] = []  # (name, required_by)
        next_queue: list[str] = []

        while queue:
            dist = queue.pop(0)
            key = normalise(dist)
            if key in seen:
                continue
            seen.add(key)

            for raw in requirements(dist):
                try:
                    req = Requirement(raw)
                except InvalidRequirement:
                    unparsed.append(f"{dist}: {raw}")
                    continue
                if req.marker is not None:
                    if _marker_mentions_extra(req.marker):
                        continue
                    try:
                        if not req.marker.evaluate():
                            continue
                    except Exception:
                        continue

                name = req.name
                key_req = normalise(name)
                version = installed_version(name)
                if version is None:
                    if key_req in manifest:
                        pin = f"{name}=={manifest[key_req]}"
                        if pin not in to_install:
                            to_install.append(pin)
                            pending.append((name, dist))
                    else:
                        entry = missing.setdefault(
                            key_req,
                            {
                                "name": name,
                                "required_by": [],
                                "specifier": str(req.specifier),
                            },
                        )
                        if dist not in entry["required_by"]:
                            entry["required_by"].append(dist)
                    continue

                if str(req.specifier) and version not in req.specifier:
                    entry = conflicts.setdefault(
                        key_req,
                        {
                            "name": name,
                            "installed": version,
                            "specifier": str(req.specifier),
                            "required_by": [],
                        },
                    )
                    if dist not in entry["required_by"]:
                        entry["required_by"].append(dist)

                if key_req not in seen:
                    next_queue.append(name)

        if to_install and install is not None:
            ok, output = install(to_install)
            # pip wrote new .dist-info dirs from a subprocess; importlib caches
            # directory listings by mtime, so a same-second install can stay
            # invisible to metadata lookups without this.
            import importlib

            importlib.invalidate_caches()
            for pin, (name, required_by) in zip(to_install, pending):
                version = installed_version(name)
                if version is None:
                    missing.setdefault(
                        normalise(name),
                        {
                            "name": name,
                            "required_by": [required_by],
                            "specifier": "",
                            "note": "Colab pin failed to install: "
                            + (output[-200:] if not ok else "(no metadata after install)"),
                        },
                    )
                else:
                    colab_installed.append(
                        {"name": name, "version": version, "required_by": required_by}
                    )
                    next_queue.append(name)
        elif to_install:
            # No installer wired (unit tests): record the intent, don't walk on.
            for pin, (name, required_by) in zip(to_install, pending):
                colab_installed.append(
                    {"name": name, "version": pin.split("==", 1)[1],
                     "required_by": required_by, "requested": True}
                )

        queue = next_queue

    return {
        "rounds": rounds,
        "missing_declared": sorted(missing.values(), key=lambda e: e["name"].lower()),
        "version_conflict": sorted(conflicts.values(), key=lambda e: e["name"].lower()),
        "colab_installed": colab_installed,
        "unparsed_requirements": unparsed,
        "walked": sorted(seen),
    }


# --------------------------------------------------------------------------
# AST import scan
# --------------------------------------------------------------------------


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    if caught is None:  # bare `except:`
        return True
    nodes = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    for node in nodes:
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name in {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}:
            return True
    return False


def scan_imports_source(source: str, relpath: str) -> list[tuple[str, int, bool]]:
    """Every absolute import in ``source`` as ``(top-level name, line, guarded)``.

    Imports at any depth are collected — a dependency imported inside a
    function is exactly the case that survives ``import autolens`` and only
    detonates at the line that reaches it. ``guarded`` means the import sits in
    the ``body`` of a ``try`` whose handlers catch ImportError (so the library
    already has a fallback); the ``except``/``else``/``finally`` arms do not
    count as guarded.
    """
    try:
        tree = ast.parse(source, filename=relpath)
    except SyntaxError:
        return []

    found: list[tuple[str, int, bool]] = []

    def record(name: str, lineno: int, guarded: bool) -> None:
        top = name.split(".", 1)[0]
        if top:
            found.append((top, lineno, guarded))

    def visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                record(alias.name, node.lineno, guarded)
            return
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                record(node.module, node.lineno, guarded)
            return
        if isinstance(node, ast.Try) or (
            hasattr(ast, "TryStar") and isinstance(node, getattr(ast, "TryStar"))
        ):
            body_guarded = guarded or any(
                _handler_catches_import_error(h) for h in node.handlers
            )
            for child in node.body:
                visit(child, body_guarded)
            for handler in node.handlers:
                for child in handler.body:
                    visit(child, guarded)
            for child in list(node.orelse) + list(node.finalbody):
                visit(child, guarded)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    visit(tree, False)
    return found


def scan_package_imports(roots: dict[str, Path]) -> dict[str, list[dict[str, Any]]]:
    """AST-scan every ``.py`` under ``roots`` -> ``{module: [site, ...]}``.

    Standard-library modules, the PyAuto packages themselves and ``__future__``
    are dropped: none of them can be missing on Colab.
    """
    excluded = set(sys.stdlib_module_names) | {"__future__"}
    excluded |= {p for p in PYAUTO_PACKAGES}
    sites: dict[str, list[dict[str, Any]]] = {}

    for package, root in sorted(roots.items()):
        base = root.parent
        for path in sorted(root.rglob("*.py")):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                relpath = str(path.relative_to(base))
            except ValueError:
                relpath = str(path)
            for name, lineno, guarded in scan_imports_source(source, relpath):
                if name in excluded or name.startswith("_"):
                    continue
                sites.setdefault(name, []).append(
                    {"file": relpath, "line": lineno, "guarded": guarded}
                )
    return sites


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------

_PROBE_SOURCE = r"""
import json, sys
results = {}
for name in json.loads(sys.argv[1]):
    try:
        __import__(name)
    except BaseException as exc:
        results[name] = f"{exc.__class__.__name__}: {exc}"
    else:
        results[name] = None
sys.stdout.write("COLAB_GATE_PROBE " + json.dumps(results))
"""


def _run_probe(names: Sequence[str], timeout: float) -> dict[str, str | None]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE_SOURCE, json.dumps(list(names))],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    marker = result.stdout.find("COLAB_GATE_PROBE ")
    if marker < 0:
        raise RuntimeError(result.stderr[-500:] or "probe produced no result")
    return json.loads(result.stdout[marker + len("COLAB_GATE_PROBE ") :])


def probe_imports(names: Sequence[str]) -> dict[str, str | None]:
    """Import each name for real, in a subprocess. ``None`` means it imported.

    One batched subprocess keeps interpreter startup off the per-name bill; a
    module that takes the whole process down with it (segfault, ``os._exit``)
    falls back to one subprocess per name so a single bad actor cannot hide the
    rest of the results.
    """
    names = list(names)
    if not names:
        return {}
    try:
        return _run_probe(names, timeout=900)
    except Exception:
        pass

    results: dict[str, str | None] = {}
    for name in names:
        try:
            results.update(_run_probe([name], timeout=180))
        except subprocess.TimeoutExpired:
            results[name] = "TimeoutExpired: import did not finish in 180s"
        except Exception as exc:
            results[name] = f"{exc.__class__.__name__}: {exc}"
    return results


_CONSTRUCTOR_SOURCE = r"""
import json, sys
results = {}
try:
    import autofit as af
except BaseException as exc:
    results["import autofit"] = f"{exc.__class__.__name__}: {exc}"
else:
    for name in json.loads(sys.argv[1]):
        try:
            getattr(af, name)()
        except BaseException as exc:
            results[name] = f"{exc.__class__.__name__}: {exc}"
        else:
            results[name] = None
sys.stdout.write("COLAB_GATE_SEARCH " + json.dumps(results))
"""


def probe_constructors(
    names: Sequence[str] = SEARCH_CONSTRUCTORS,
) -> dict[str, str | None]:
    """Construct the headline ``autofit`` searches; ``None`` means it built."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", _CONSTRUCTOR_SOURCE, json.dumps(list(names))],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return {"autofit searches": "TimeoutExpired: constructors did not finish"}
    marker = result.stdout.find("COLAB_GATE_SEARCH ")
    if marker < 0:
        return {
            "autofit searches": (result.stderr[-500:] or "no result from constructor probe")
        }
    return json.loads(result.stdout[marker + len("COLAB_GATE_SEARCH ") :])


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def _parse_config_fallback(text: str) -> dict[str, Any]:
    """Minimal reader for the one shape colab_gate.yaml is allowed to take.

    ``verify`` runs inside the simulated venv, which is not guaranteed to hold
    PyYAML. The file is a list of ``name``/``reason`` mappings, which is small
    enough to read without it.
    """
    entries: list[dict[str, str]] = []
    in_list = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^accepted_missing\s*:", line):
            in_list = True
            continue
        if not line.startswith((" ", "-")) and ":" in line:
            in_list = False
            continue
        if not in_list:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            entries.append({})
            stripped = stripped[2:].strip()
        if ":" in stripped and entries:
            key, _, value = stripped.partition(":")
            entries[-1][key.strip()] = value.strip().strip("'\"")
    return {"accepted_missing": [e for e in entries if e.get("name")]}


def load_config(path: Path | None) -> dict[str, Any]:
    path = Path(path) if path else CONFIG_PATH
    if not path.is_file():
        return {"accepted_missing": []}
    text = path.read_text()
    try:
        import yaml
    except ImportError:
        return _parse_config_fallback(text)
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        return {"accepted_missing": []}
    accepted = data.get("accepted_missing") or []
    return {"accepted_missing": [e for e in accepted if isinstance(e, dict) and e.get("name")]}


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------


def module_dist_candidates(module: str) -> set[str]:
    """Distribution names a top-level module could be published under.

    The inverse of :data:`DIST_MODULE_ALIASES`, used to ask the Colab manifest
    "does Colab ship the thing this import needs?" for a module that is not
    installed (so has no metadata to read the mapping from).
    """
    candidates = {normalise(module)}
    for dist, modules in DIST_MODULE_ALIASES.items():
        if module in modules:
            candidates.add(normalise(dist))
    return candidates


def dist_module_candidates(dist_name: str) -> set[str]:
    key = normalise(dist_name)
    candidates = {key, key.replace("-", "_"), dist_name, dist_name.lower()}
    candidates.update(DIST_MODULE_ALIASES.get(key, []))
    return candidates


def verdict(
    *,
    missing_declared: list[dict[str, Any]],
    version_conflict: list[dict[str, Any]],
    import_failures_unguarded: list[dict[str, Any]],
    import_failures_guarded: list[dict[str, Any]],
    constructor_failures: list[dict[str, Any]],
    closure_not_on_colab: list[str] | None = None,
    imported_modules: Iterable[str] = (),
    accepted_missing: Iterable[dict[str, str]] = (),
) -> dict[str, Any]:
    """Apply the gate's rules and return ``{ok, fails, warns, accepted}``.

    FAIL is reserved for the three shapes that break a real notebook: an
    unguarded import of a module Colab will not have, a headline search that
    cannot be constructed, and a declared dependency that is both missing and
    actually imported. Everything else is reported and non-blocking — a
    ``version_conflict`` is usually Colab pinning what Colab pins, a guarded
    import already has a fallback, and a never-imported missing declaration
    costs a notebook nothing.
    """
    accepted_index = {
        normalise(entry["name"]): entry.get("reason", "")
        for entry in accepted_missing
        if entry.get("name")
    }
    imported = {m for m in imported_modules}
    imported_norm = {normalise(m) for m in imported}

    fails: list[str] = []
    warns: list[str] = []
    accepted: list[dict[str, str]] = []

    def is_accepted(name: str) -> str | None:
        key = normalise(name)
        if key in accepted_index:
            return accepted_index[key]
        return None

    for entry in import_failures_unguarded:
        module = entry["module"]
        reason = is_accepted(module)
        site = entry.get("sites", [{}])[0]
        where = f"{site.get('file', '?')}:{site.get('line', '?')}"
        if reason is not None:
            accepted.append({"name": module, "kind": "unguarded import", "reason": reason})
            warns.append(f"accepted unguarded import {module} ({where}): {reason}")
        else:
            fails.append(f"{module} ({where})")

    for entry in constructor_failures:
        name = entry["name"]
        reason = is_accepted(name)
        if reason is not None:
            accepted.append({"name": name, "kind": "constructor", "reason": reason})
            warns.append(f"accepted constructor failure af.{name}: {reason}")
        else:
            fails.append(f"af.{name}() {entry.get('error', '')}".strip())

    for entry in missing_declared:
        name = entry["name"]
        candidates = dist_module_candidates(name)
        is_imported = bool(
            candidates & imported
            or {normalise(c) for c in candidates} & imported_norm
        )
        entry["imported"] = is_imported
        if not is_imported:
            warns.append(
                f"declared dependency {name} absent on Colab but never imported "
                f"(required by {', '.join(entry.get('required_by', [])) or '?'})"
            )
            continue
        reason = is_accepted(name)
        if reason is not None:
            accepted.append({"name": name, "kind": "missing dependency", "reason": reason})
            warns.append(f"accepted missing dependency {name}: {reason}")
        else:
            fails.append(
                f"{name} (declared by {', '.join(entry.get('required_by', [])) or '?'}, "
                "absent on Colab, imported)"
            )

    for entry in version_conflict:
        warns.append(
            f"{entry['name']} {entry.get('installed')} outside "
            f"{entry.get('specifier')} required by "
            f"{', '.join(entry.get('required_by', [])) or '?'}"
        )

    for entry in import_failures_guarded:
        warns.append(
            f"guarded import {entry['module']} unavailable "
            f"({len(entry.get('sites', []))} site(s))"
        )

    for name in closure_not_on_colab or []:
        warns.append(f"declared in the with-deps closure but not shipped by Colab: {name}")

    return {"ok": not fails, "fails": fails, "warns": warns, "accepted": accepted}


def format_detail(fails: list[str], limit: int = 3) -> str:
    """The RESULTS detail string for a failing gate."""
    head = "; ".join(fails[:limit])
    if len(fails) > limit:
        head += f"; +{len(fails) - limit} more"
    return f"colab gate: {head}"


# --------------------------------------------------------------------------
# report writing
# --------------------------------------------------------------------------


def _write_report(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(target)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------


def cmd_seed(ns: argparse.Namespace) -> int:
    index_args = list(ns.index_args or [])
    manifest, source, date, notes = load_manifest(
        Path(ns.manifest_cache) if ns.manifest_cache else None,
        Path(ns.snapshot) if ns.snapshot else None,
    )
    print(f"colab_gate seed: manifest {source} ({date or 'date unknown'}), "
          f"{len(manifest)} pinned packages")
    for note in notes:
        print(f"  note: {note}")

    targets = list(ns.targets) + (["jax"] if ns.include_jax else [])
    report: dict[str, Any] = {
        "phase": "seed",
        "ts": _now(),
        "manifest_source": source,
        "manifest_date": date,
        "manifest_url": MANIFEST_URL,
        "manifest_packages": len(manifest),
        "manifest_notes": notes,
        "targets": targets,
        "python": sys.version.split()[0],
    }

    try:
        closure = resolve_closure(targets, index_args)
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)
        report["detail"] = f"colab gate: closure resolution failed ({exc.__class__.__name__})"
        _write_report(ns.report_json, report)
        print(f"colab_gate seed: FAILED — {exc}", file=sys.stderr)
        return 1

    pyauto = {normalise(p) for p in PYAUTO_PACKAGES}
    wanted = colab_intersection(closure, manifest)
    not_on_colab = sorted(
        name for name in closure if name not in manifest and name not in pyauto
    )
    print(f"colab_gate seed: closure {len(closure)} packages; "
          f"{len(wanted)} also shipped by Colab; "
          f"{len(not_on_colab)} not on Colab (deliberately NOT installed)")

    pins = [f"{name}=={manifest[name]}" for name in wanted]
    seeded: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    if pins:
        result = _pip(["install", "--no-deps", "--quiet", *index_args, *pins])
        if result.returncode != 0:
            # One unavailable pin fails the whole batch; retry singly so the
            # report names the packages Colab pins to something this
            # interpreter cannot install rather than losing all 50.
            print("colab_gate seed: batch install failed, retrying per package")
            for pin in pins:
                single = _pip(["install", "--no-deps", "--quiet", *index_args, pin])
                name, _, version = pin.partition("==")
                if single.returncode == 0:
                    seeded.append({"name": name, "version": version})
                else:
                    failures.append({
                        "name": name,
                        "version": version,
                        "error": (single.stderr or single.stdout)[-300:],
                    })
        else:
            seeded = [
                {"name": p.split("==")[0], "version": p.split("==")[1]} for p in pins
            ]

    # --- complete the seeded set to a self-consistent Colab subset ----------
    #
    # The intersection is installed --no-deps, so a seeded package's OWN runtime
    # dependencies only land if they happen to be in the PyAuto closure too.
    # They usually are not: Colab ships IPython 7.34.0, which needs pickleshare,
    # and nothing in the autolens closure requires pickleshare — so `import jax`
    # (which reaches IPython through its Colab debugger) died on a package Colab
    # has had all along. A real Colab session is internally consistent, so the
    # simulation must be too: walk the seeded packages' requirements and install
    # anything COLAB ALSO SHIPS that is still missing, at Colab's pin. Nothing
    # absent from the manifest is ever installed here — that absence is the
    # whole measurement.
    def _install(pins: list[str]) -> tuple[bool, str]:
        result = _pip(["install", "--no-deps", "--quiet", *index_args, *pins])
        return result.returncode == 0, (result.stderr or result.stdout)

    completion = walk_requirements(
        [entry["name"] for entry in seeded], manifest, install=_install
    )
    for entry in completion["colab_installed"]:
        seeded.append({"name": entry["name"], "version": entry["version"]})
    if completion["colab_installed"]:
        print(f"colab_gate seed: completed the Colab subset with "
              f"{len(completion['colab_installed'])} further Colab-shipped "
              f"dependencies of the seeded packages")

    report.update({
        "ok": True,
        "closure": closure,
        "seeded": seeded,
        "seed_failures": failures,
        "seed_completion": {
            "rounds": completion["rounds"],
            "installed": completion["colab_installed"],
            # Dependencies of Colab's own packages that Colab does not ship.
            # Informational: Colab lives with them, so the gate does too.
            "not_on_colab": completion["missing_declared"],
        },
        "closure_not_on_colab": [
            {"name": name, "closure_version": closure[name]} for name in not_on_colab
        ],
    })
    _write_report(ns.report_json, report)
    print(f"colab_gate seed: installed {len(seeded)} Colab-pinned packages"
          + (f", {len(failures)} could not be installed" if failures else ""))
    for entry in failures:
        print(f"  seed failure: {entry['name']}=={entry['version']}")
    return 0


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def _package_roots() -> dict[str, Path]:
    import importlib.util

    roots: dict[str, Path] = {}
    for package in PYAUTO_PACKAGES:
        try:
            spec = importlib.util.find_spec(package)
        except Exception:
            continue
        if spec is None or not spec.submodule_search_locations:
            continue
        roots[package] = Path(list(spec.submodule_search_locations)[0])
    return roots


def _installed_non_pyauto() -> dict[str, str]:
    from importlib import metadata

    pyauto = {normalise(p) for p in PYAUTO_PACKAGES}
    out: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        key = normalise(name)
        if key in pyauto:
            continue
        out[key] = dist.version
    return out


def cmd_verify(ns: argparse.Namespace) -> int:
    index_args = list(ns.index_args or [])
    manifest, source, date, notes = load_manifest(
        Path(ns.manifest_cache) if ns.manifest_cache else None,
        Path(ns.snapshot) if ns.snapshot else None,
    )
    config = load_config(Path(ns.config) if ns.config else None)

    closure_not_on_colab: list[str] = []
    seed_report_path = getattr(ns, "seed_report", None)
    if seed_report_path and Path(seed_report_path).is_file():
        try:
            seed_data = json.loads(Path(seed_report_path).read_text())
        except (OSError, ValueError):
            seed_data = {}
        closure_not_on_colab = [
            entry["name"]
            for entry in seed_data.get("closure_not_on_colab", [])
            if isinstance(entry, dict) and entry.get("name")
        ]

    report: dict[str, Any] = {
        "phase": "verify",
        "ts": _now(),
        "manifest_source": source,
        "manifest_date": date,
        "manifest_packages": len(manifest),
        "manifest_notes": notes,
        "python": sys.version.split()[0],
    }

    installed_pyauto = {
        package: _installed_version(package) for package in PYAUTO_PACKAGES
    }
    report["packages"] = installed_pyauto
    print("colab_gate verify: installed PyAuto stack — " + ", ".join(
        f"{k}={v}" for k, v in installed_pyauto.items()
    ))

    def install(pins: list[str]) -> tuple[bool, str]:
        result = _pip(["install", "--no-deps", "--quiet", *index_args, *pins])
        return result.returncode == 0, (result.stderr or result.stdout)

    # --- 1. declared-requirement walk ---------------------------------------
    walk = walk_requirements(
        [p for p in PYAUTO_PACKAGES if installed_pyauto.get(p)],
        manifest,
        install=install,
    )
    report["walk"] = {
        "rounds": walk["rounds"],
        "colab_installed": walk["colab_installed"],
        "unparsed_requirements": walk["unparsed_requirements"],
    }
    report["missing_declared"] = walk["missing_declared"]
    report["version_conflict"] = walk["version_conflict"]
    print(f"colab_gate verify: requirement walk visited {len(walk['walked'])} "
          f"distributions in {walk['rounds']} round(s); "
          f"{len(walk['colab_installed'])} healed from Colab's pins, "
          f"{len(walk['missing_declared'])} declared-but-absent, "
          f"{len(walk['version_conflict'])} version conflict(s)")

    # --- 2. import probe -----------------------------------------------------
    roots = _package_roots()
    sites = scan_package_imports(roots)
    probe = probe_imports(sorted(sites))

    # An import can fail here for a module Colab ships perfectly well: the seed
    # installs the closure of the PyAuto stack, and a library may import
    # something Colab has but nothing in that closure declares (numba is the
    # live example). On Colab that import succeeds, so reporting it as a miss
    # would be the gate lying about the platform. Install Colab's pin and probe
    # again — the same rule the requirement walk applies, reached by a
    # different route.
    healable: dict[str, str] = {}
    for module, error in probe.items():
        if error is None:
            continue
        for candidate in module_dist_candidates(module):
            if candidate in manifest:
                healable[module] = f"{candidate}=={manifest[candidate]}"
                break
    colab_healed: list[dict[str, str]] = []
    if healable:
        print(f"colab_gate verify: {len(healable)} failed import(s) are shipped by "
              f"Colab — installing Colab's pins and re-probing")
        pins = sorted(set(healable.values()))
        ok, output = install(pins)
        # --no-deps again, so the package's own Colab-shipped dependencies have
        # to be completed the same way the seed completes its set — `numba`
        # without `llvmlite` imports no better than no numba at all.
        walk_requirements(
            [pin.split("==", 1)[0] for pin in pins], manifest, install=install
        )
        reprobe = probe_imports(sorted(healable))
        for module, pin in sorted(healable.items()):
            probe[module] = reprobe.get(module, probe[module])
            if probe[module] is None:
                colab_healed.append({"module": module, "pin": pin})
        if not ok:
            report["manifest_notes"] = list(notes) + [
                f"installing Colab pins for failed imports reported: {output[-200:]}"
            ]
    report["imports_provided_by_colab"] = colab_healed

    unguarded: list[dict[str, Any]] = []
    guarded: list[dict[str, Any]] = []
    for module in sorted(sites):
        error = probe.get(module)
        if error is None:
            continue
        entry = {
            "module": module,
            "error": error,
            "sites": sites[module][:10],
            "n_sites": len(sites[module]),
        }
        if any(not site["guarded"] for site in sites[module]):
            entry["sites"] = [s for s in sites[module] if not s["guarded"]][:10]
            unguarded.append(entry)
        else:
            guarded.append(entry)
    report["imports_probed"] = len(sites)
    report["import_failures_unguarded"] = unguarded
    report["import_failures_guarded"] = guarded
    print(f"colab_gate verify: probed {len(sites)} third-party imports — "
          f"{len(unguarded)} unguarded failure(s), {len(guarded)} guarded")

    # --- 3. search constructors ---------------------------------------------
    constructors = probe_constructors()
    constructor_failures = [
        {"name": name, "error": error}
        for name, error in sorted(constructors.items())
        if error is not None
    ]
    report["constructor_failures"] = constructor_failures
    print(f"colab_gate verify: constructed {len(constructors) - len(constructor_failures)}"
          f"/{len(constructors)} searches")

    # --- 4. extras the bootstrap added on top of Colab ------------------------
    installed = _installed_non_pyauto()
    extras = sorted(name for name in installed if name not in manifest)
    report["extras_not_on_colab"] = [
        {"name": name, "version": installed[name]} for name in extras
    ]
    colab_provided = sorted(name for name in installed if name in manifest)
    report["colab_provided"] = len(colab_provided)

    # --- 5. verdict -----------------------------------------------------------
    result = verdict(
        missing_declared=report["missing_declared"],
        version_conflict=report["version_conflict"],
        import_failures_unguarded=unguarded,
        import_failures_guarded=guarded,
        constructor_failures=constructor_failures,
        closure_not_on_colab=closure_not_on_colab,
        imported_modules=sites.keys(),
        accepted_missing=config["accepted_missing"],
    )
    report.update(result)

    if result["ok"]:
        report["detail"] = (
            f"Colab manifest {source} {date or 'date unknown'}; "
            f"{len(colab_provided)} Colab-provided, {len(extras)} extras, "
            f"{len(sites)} imports probed"
        )
    else:
        report["detail"] = format_detail(result["fails"])

    _write_report(ns.report_json, report)

    print()
    print("colab_gate verify: " + ("PASS" if result["ok"] else "FAIL"))
    for line in result["fails"]:
        print(f"  FAIL  {line}")
    for line in result["warns"]:
        print(f"  WARN  {line}")
    print(f"  {report['detail']}")
    return 0 if result["ok"] else 1


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="colab_gate",
        description="Colab package-set gate for verify_install check F.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--report-json", help="write the machine-readable report here")
        p.add_argument(
            "--manifest-cache",
            help="path the live Colab manifest is cached to / read back from",
        )
        p.add_argument(
            "--snapshot",
            help=f"vendored manifest fallback (default: {SNAPSHOT_PATH})",
        )

    seed = sub.add_parser("seed", help="install Colab's package set before the setup cell")
    common(seed)
    seed.add_argument(
        "--targets", nargs="+", required=True,
        help="the PyAuto install targets whose with-deps closure is resolved",
    )
    seed.add_argument(
        "--no-jax", dest="include_jax", action="store_false",
        help="do not add jax to the closure targets (Colab ships jax)",
    )
    seed.add_argument(
        "--index-args", nargs=argparse.REMAINDER, default=[],
        help="everything after this flag is passed straight to pip (must be last)",
    )
    seed.set_defaults(func=cmd_seed, include_jax=True)

    verify = sub.add_parser("verify", help="audit the environment the setup cell left")
    common(verify)
    verify.add_argument("--config", help=f"accepted-miss config (default: {CONFIG_PATH})")
    verify.add_argument(
        "--seed-report",
        help="the seed phase report, whose closure_not_on_colab list is folded in as WARNs",
    )
    verify.add_argument(
        "--index-args", nargs=argparse.REMAINDER, default=[],
        help="everything after this flag is passed straight to pip (must be last)",
    )
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    ns = build_parser().parse_args(list(argv) if argv is not None else sys.argv[1:])
    try:
        return ns.func(ns)
    except Exception as exc:  # tool error, distinct from a gate failure
        print(f"colab_gate: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
