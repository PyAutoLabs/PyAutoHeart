---
name: audit-docs
description: Audit docs/api/*.rst files across repos for broken module paths, missing classes, and stale autosummary entries.
---

Scan Sphinx API documentation across PyAutoFit, PyAutoGalaxy, and PyAutoLens for broken `.. currentmodule::` paths and stale `.. autosummary::` class references. Optionally auto-fix broken entries.

A **PyAutoHeart** check — documentation correctness is part of the validation surface Heart owns. The audit is read-only; auto-fix is opt-in and confined to the docs files it flags.

## Usage

```
$audit-docs                         # audit all three repos (default)
$audit-docs PyAutoLens              # audit one repo
$audit-docs PyAutoGalaxy PyAutoFit  # audit specific repos
$audit-docs --fix                   # audit all repos and auto-fix broken references
$audit-docs --fix PyAutoLens        # fix one repo
```

In Claude, invoke the same skill as `/audit-docs`.

## Repo Mapping

| Argument | Docs Directory |
|----------|---------------|
| `PyAutoFit` | `./PyAutoFit/docs/api/` |
| `PyAutoGalaxy` | `./PyAutoGalaxy/docs/api/` |
| `PyAutoLens` | `./PyAutoLens/docs/api/` |

All paths are relative to the workspace root.

## Steps

### 1. Determine which repos to audit

- **Default: audit ALL three repos.** Cross-package references (e.g. PyAutoLens docs referencing autoarray modules) mean a change in one library can break another library's docs.
- Only audit a subset if the user explicitly passes repo names as arguments.

### 2. Parse RST files

For each repo, glob `docs/api/*.rst` and extract:

- Every `.. currentmodule:: <module_path>` directive (record file, line number, module path)
- Every entry in `.. autosummary::` blocks (record file, line number, class/function name, and which `currentmodule` it falls under)

Build a list of `(file, line, module_path, class_name)` tuples.

### 3. Validate module paths

For each unique `currentmodule` path, test whether it is importable:

```bash
NUMBA_CACHE_DIR=/tmp/numba_cache MPLCONFIGDIR=/tmp/matplotlib python -c "import <module_path>"
```

Batch multiple imports into a single Python invocation for speed:

```python
import importlib, sys
modules = ["autoarray.inversion.mesh.image_mesh", "autofit", ...]
for m in modules:
    try:
        importlib.import_module(m)
        print(f"OK {m}")
    except ImportError as e:
        print(f"FAIL {m}: {e}")
```

Record which modules pass and which fail.

### 4. Validate class/function names

For each autosummary entry whose parent module is importable, verify the name exists:

```python
import importlib
mod = importlib.import_module("<module_path>")
if hasattr(mod, "<ClassName>"):
    print(f"OK <module_path>.<ClassName>")
else:
    print(f"MISSING <module_path>.<ClassName>")
```

Again, batch into a single Python invocation.

### 5. Discover undocumented exports

For each importable `currentmodule`, list what the module actually exports:

```python
mod = importlib.import_module("<module_path>")
exports = [name for name in dir(mod) if not name.startswith("_") and isinstance(getattr(mod, name), type)]
```

Compare against the documented autosummary entries. Flag any class that exists in the module but is NOT listed in the docs. These are **suggestions**, not errors — some classes are intentionally internal.

### 6. Attempt to suggest fixes for broken modules

When a module path fails to import, try common renames to find the correct path. For each broken module `a.b.c.d`:

1. Try importing `a.b.c` — if that works and has attribute `d`, the correct currentmodule is `a.b.c` (and `d` should be in the autosummary list)
2. Search for the leaf name `d` elsewhere in the package:
   ```python
   # Find modules containing the leaf name
   import pkgutil, importlib
   root = importlib.import_module(module_path.split(".")[0])
   for importer, modname, ispkg in pkgutil.walk_packages(root.__path__, root.__name__ + "."):
       try:
           mod = importlib.import_module(modname)
           if hasattr(mod, leaf_name):
               print(f"SUGGESTION: {modname}")
       except: pass
   ```
3. Present suggestions in the report.

### 7. Report results

Display a per-file summary table:

```
PyAutoLens/docs/api/pixelization.rst
  OK  line 16: currentmodule autolens
  OK  line 23: autolens.Pixelization
  X   BROKEN MODULE  line 28: autoarray.inversion.pixelization.image_mesh
      -> Suggested fix: autoarray.inversion.mesh.image_mesh
  X   MISSING CLASS  line 51: Voronoi (not found in autoarray.inversion.mesh.mesh)
  o   NOT DOCUMENTED autoarray.inversion.mesh.mesh.KNearestNeighbor

Summary:
  Modules checked: 12 (10 OK, 2 broken)
  Classes checked: 45 (43 OK, 2 missing)
  Undocumented exports: 8 (suggestions only)
```

### 8. Auto-fix (if --fix flag)

When `--fix` is specified:

1. **Broken module paths with a single suggestion**: Replace the `currentmodule` directive with the suggested path. Show the diff.
2. **Missing class references**: Remove the entry from the autosummary block. Show what was removed.
3. **Undocumented exports**: Do NOT auto-add these. Just list them as suggestions for the user to review.

After fixing, re-run the validation (steps 3-4) to confirm all references now resolve.

### 9. Post results to GitHub issue (optional)

If `PyAutoMind/active.md` contains an active issue URL, offer to post the audit summary as a comment. Use the format:

```bash
gh issue comment <number> --repo <owner/repo> --body "$(cat <<'AUDIT_EOF'
## Docs API Audit — <YYYY-MM-DD>

| Repo | Modules OK | Modules Broken | Classes OK | Classes Missing | Undocumented |
|------|-----------|---------------|-----------|----------------|-------------|
| PyAutoFit | X | Y | X | Y | Z |
| PyAutoGalaxy | X | Y | X | Y | Z |
| PyAutoLens | X | Y | X | Y | Z |

<details if any failures>
AUDIT_EOF
)"
```

## Notes

- Always set `NUMBA_CACHE_DIR=/tmp/numba_cache MPLCONFIGDIR=/tmp/matplotlib` before Python import checks to avoid cache permission errors.
- Top-level namespace packages (e.g. `autolens`, `autogalaxy`, `autofit`) re-export many classes from lower-level packages. A `currentmodule:: autolens` with `Pixelization` in the autosummary is valid if `autolens.Pixelization` resolves, even though the class is defined in `autoarray`.
- Some RST files legitimately reference modules from other packages (e.g. PyAutoLens docs referencing `autoarray.inversion.regularization`). This is expected and should not be flagged as an error — only flag it if the import actually fails.
- The `Voronoi` mesh class was removed from autoarray but may still appear in older docs. This is a known pattern — always remove dead references rather than leaving them.
- Duplicate entries in autosummary blocks (e.g. `Voronoi` listed twice) should be flagged and deduplicated.
