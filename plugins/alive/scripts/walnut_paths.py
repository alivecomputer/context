#!/usr/bin/env python3
"""ALIVE Context System -- walnut path helpers (vendored).

Public API for resolving and discovering bundles inside a walnut. Vendors the
v3-aware bundle resolution and scanning logic from
``plugins/alive/scripts/tasks.py`` (``_resolve_bundle_path`` / ``_find_bundles``)
and ``plugins/alive/scripts/project.py`` (``scan_bundles``) under stable public
names so external callers do not import underscored privates that may change
without notice across plugin updates.

This module exists per LD10 of the fn-7-7cw epic spec. ``alive-p2p.py`` (and any
future v3 P2P consumer) imports from here instead of from tasks.py / project.py
directly. The vendored implementations remain layout-agnostic: they handle v3
flat bundles at walnut root, v2 ``bundles/`` containers, and v1
``_core/_capsules/`` legacy capsules.

Stdlib only. No PyYAML. Type hints use the ``typing`` module (3.9 floor).
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple


# Directories that are skipped during bundle discovery. Mirrors the union of
# project.py::scan_bundles and tasks.py::_find_bundles skip lists, plus the
# obvious archive / build paths a v3 walnut may carry.
_SKIP_DIRS = {
    "_kernel",
    "_core",
    ".git",
    ".alive",
    "node_modules",
    "raw",
    "__pycache__",
    "dist",
    "build",
    ".next",
    "target",
    "_archive",
    "_references",
    "01_Archive",
}


def resolve_bundle_path(walnut, bundle):
    # type: (str, str) -> Optional[str]
    """Find a bundle directory by name. Returns absolute path or None.

    Layout fallback order:
        1. v3 flat:    ``{walnut}/{bundle}``
        2. v2 nested:  ``{walnut}/bundles/{bundle}``
        3. v1 legacy:  ``{walnut}/_core/_capsules/{bundle}``

    Returns None when none of the candidates exist on disk. Unlike the
    ``tasks.py`` private which returns a v3 placeholder for new-bundle creation,
    this function refuses to invent paths -- callers can decide how to handle
    "not found" themselves.
    """
    if not bundle:
        return None

    candidates = (
        os.path.join(walnut, bundle),
        os.path.join(walnut, "bundles", bundle),
        os.path.join(walnut, "_core", "_capsules", bundle),
    )
    for candidate in candidates:
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return None


def find_bundles(walnut):
    # type: (str) -> List[Tuple[str, str]]
    """Walk a walnut and return ``(bundle_relpath, abs_path)`` tuples.

    Discovery rules:
        - A directory is a bundle if it contains ``context.manifest.yaml``
          (v2/v3) or ``companion.md`` (v1 legacy).
        - ``bundle_relpath`` is POSIX-normalized (forward slashes), relative to
          ``walnut``. Top-level bundles report their bare directory name; nested
          bundles report e.g. ``archive/old/bundle-a``.
        - Hidden directories and entries in ``_SKIP_DIRS`` are pruned.
        - Nested walnut roots (any directory containing ``_kernel/key.md``) are
          treated as boundaries: their interior is NEVER scanned, so a parent's
          ``find_bundles`` does not bleed into a child walnut's bundles.

    Results are sorted by ``bundle_relpath`` for stable test fixtures.
    """
    walnut = os.path.abspath(walnut)
    bundles = []  # type: List[Tuple[str, str]]
    nested_walnut_roots = set()  # type: set

    for root, dirs, files in os.walk(walnut):
        rel = os.path.relpath(root, walnut)

        # Prune hidden + skip dirs in-place so os.walk does not descend into
        # them. The ``_SKIP_DIRS`` set is intentionally tight: anything outside
        # it is candidate ground for bundle discovery.
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d not in _SKIP_DIRS
        ]

        # If the current directory sits inside a nested walnut we already
        # detected, skip it entirely.
        if rel != ".":
            inside_nested = False
            for nested in nested_walnut_roots:
                if rel == nested or rel.startswith(nested + os.sep):
                    inside_nested = True
                    break
            if inside_nested:
                dirs[:] = []
                continue

        # Detect a nested walnut boundary: a non-root directory that contains
        # ``_kernel/key.md``. Mark the relpath as a boundary and stop descending.
        if rel != ".":
            kernel_key = os.path.join(root, "_kernel", "key.md")
            if os.path.isfile(kernel_key):
                nested_walnut_roots.add(rel)
                dirs[:] = []
                continue

        # Bundle detection. v2/v3 takes precedence; v1 only fires if a manifest
        # is absent (matches the ``elif`` order in tasks.py::_find_bundles).
        is_bundle = False
        if "context.manifest.yaml" in files:
            is_bundle = True
        elif "companion.md" in files:
            is_bundle = True

        if is_bundle:
            if rel == ".":
                # The walnut root itself is not a bundle even if a stray
                # manifest sits there. Skip it.
                continue
            relpath_posix = rel.replace(os.sep, "/")
            bundles.append((relpath_posix, os.path.abspath(root)))

    bundles.sort(key=lambda b: b[0])
    return bundles


def scan_bundles(walnut):
    # type: (str) -> Dict[str, Dict[str, Any]]
    """Return ``{bundle_relpath: parsed_manifest_dict}`` for every discoverable bundle.

    Uses ``find_bundles`` for discovery and a regex-only manifest parser for
    field extraction. Bundles whose manifest cannot be read or parsed are
    omitted from the result -- callers should treat absence as "no usable
    metadata", not "no bundle".

    The parsed manifest dict is intentionally minimal: it carries the same
    fields ``project.py::parse_manifest`` extracts (goal, status, updated, due,
    context, active_sessions). Future fields can be added without changing the
    public signature.
    """
    result = {}  # type: Dict[str, Dict[str, Any]]
    for relpath, abs_path in find_bundles(walnut):
        manifest_path = os.path.join(abs_path, "context.manifest.yaml")
        parsed = _parse_manifest_minimal(manifest_path)
        if parsed is not None:
            result[relpath] = parsed
    return result


def _parse_manifest_minimal(filepath):
    # type: (str) -> Optional[Dict[str, Any]]
    """Regex-only parse of ``context.manifest.yaml``. Returns dict or None.

    Mirrors the contract of ``project.py::parse_manifest``: stdlib only, no
    PyYAML, tolerates missing fields, returns None only on read error so the
    caller can distinguish "manifest unreadable" from "manifest empty".
    """
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, OSError, UnicodeDecodeError):
        return None

    result = {}  # type: Dict[str, Any]

    # Simple single-line scalar fields. The list mirrors project.py and adds a
    # few that bundle manifests commonly carry.
    for field in ("goal", "status", "updated", "due", "name", "outcome", "phase"):
        pattern = r"^{0}:\s*['\"]?(.*?)['\"]?\s*$".format(re.escape(field))
        m = re.search(pattern, content, re.MULTILINE)
        if m:
            result[field] = m.group(1).strip()

    # Multi-line context block (``context: |`` or ``context: >``). Falls back
    # to a single-line capture if the block form is absent.
    ctx_block = re.search(
        r"^context:\s*[|>]-?\s*\n((?:[ \t]+.+\n?)*)",
        content,
        re.MULTILINE,
    )
    if ctx_block:
        lines = ctx_block.group(1).split("\n")
        stripped = [ln.strip() for ln in lines if ln.strip()]
        result["context"] = "\n".join(stripped)
    else:
        ctx_simple = re.search(
            r"^context:\s*['\"]?(.*?)['\"]?\s*$",
            content,
            re.MULTILINE,
        )
        if ctx_simple:
            result["context"] = ctx_simple.group(1).strip()

    # active_sessions list (used by P2P stripping logic and project.py).
    sessions = []  # type: List[str]
    sq_match = re.search(
        r"^squirrels:\s*\n((?:[ \t]*-\s*.+\n?)*)",
        content,
        re.MULTILINE,
    )
    if sq_match:
        for item in re.finditer(r"-\s*(\S+)", sq_match.group(1)):
            sessions.append(item.group(1))
    result["active_sessions"] = sessions

    return result


__all__ = [
    "resolve_bundle_path",
    "find_bundles",
    "scan_bundles",
]
