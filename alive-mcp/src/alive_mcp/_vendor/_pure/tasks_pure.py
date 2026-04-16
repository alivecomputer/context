"""Pure helpers extracted from ``plugins/alive/scripts/tasks.py``.

UPSTREAM: ``claude-code/plugins/alive/scripts/tasks.py`` (see
``../VENDORING.md`` for the pinned commit hash). The upstream script is a
CLI whose helpers ``print()`` warnings on malformed files and ``sys.exit(1)``
on strict read errors. Those behaviors are lethal inside a long-lived stdio
JSON-RPC server, so this module extracts the pure task-collection logic
with the CLI shell discarded.

EXTRACTED FUNCTIONS (match upstream names where feasible):
    - ``_all_task_files(walnut)``  from tasks.py L72-L103 (private)
    - ``_read_tasks_json(path)``   from tasks.py L26-L49 (renamed to make
                                   the "tasks" key implicit + drop the CLI
                                   exit path; fails silently on malformed
                                   JSON via ``MalformedYAMLWarning``)
    - ``_collect_all_tasks(walnut)`` from tasks.py L149-L156 (private,
                                     re-exported as-is)
    - ``collect_all_tasks(walnut)`` public alias on the private above (the
                                    leading underscore in upstream is a
                                    side-effect of Python module privacy,
                                    not a semantic privacy signal for
                                    external callers)
    - ``summary_from_walnut(walnut, include_items=False)`` -- distilled
      from ``cmd_summary`` (tasks.py L424-L584), returns the same dict
      structure the CLI used to ``print(json.dumps(...))``. Callers get a
      proper Python dict instead of having to shell out.

DISCARDED from upstream (CLI-only or write-path):
    - ``cmd_add``, ``cmd_done``, ``cmd_drop``, ``cmd_edit`` -- write paths,
      out of scope for v0.1 read-only.
    - ``cmd_list`` -- duplicated trivially in Python by filtering
      ``_collect_all_tasks``; not worth a dedicated helper.
    - ``cmd_summary``'s ``print(json.dumps(...))`` -- replaced by returning
      the dict from ``summary_from_walnut``.
    - ``_atomic_write`` -- no write surface in v0.1.
    - ``main`` + argparse wiring -- this is a library.
    - The ``v2 tasks.md`` deprecation warning printed to stderr -- swapped
      for ``MalformedYAMLWarning`` so the MCP audit layer can observe it
      without touching stderr.

Stdlib only. No PyYAML. 3.10 floor (matches pyproject ``requires-python``).
"""
from __future__ import annotations

import json
import os
import re
import warnings
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import KernelFileError, MalformedYAMLWarning


# ---------------------------------------------------------------------------
# File discovery (respects nested walnut boundaries)
# ---------------------------------------------------------------------------

_TASK_FILE_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "dist", "build", ".next", "target",
    # Archive and reference directories contain legacy files that may not
    # conform to the v3 tasks.json schema. Never scan into them.
    "_archive", "_references", "01_Archive", "raw",
}


def _all_task_files(walnut: str) -> List[str]:
    """Return absolute paths of every ``tasks.json`` under ``walnut``.

    Stops at nested walnut boundaries (``_kernel/key.md``) so a parent walnut
    doesn't scan into child walnuts. Each walnut manages its own tasks.

    Mirrors ``tasks.py::_all_task_files`` exactly, except the v2 ``tasks.md``
    deprecation signal is delivered via ``warnings.warn(MalformedYAMLWarning)``
    instead of being printed to stderr.
    """
    results: List[str] = []
    walnut_abs = os.path.abspath(walnut)
    for root, dirs, files in os.walk(walnut):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _TASK_FILE_SKIP_DIRS]
        if os.path.abspath(root) != walnut_abs:
            kernel_key = os.path.join(root, "_kernel", "key.md")
            if os.path.isfile(kernel_key):
                dirs[:] = []
                continue
        if "tasks.json" in files:
            results.append(os.path.join(root, "tasks.json"))
        if "tasks.md" in files and "tasks.json" not in files:
            warnings.warn(
                "{}/tasks.md found (v2 format); "
                "run system-upgrade to migrate".format(root),
                MalformedYAMLWarning,
                stacklevel=2,
            )
    return results


# ---------------------------------------------------------------------------
# JSON reader (lenient -- upstream has strict+lenient; we only need lenient)
# ---------------------------------------------------------------------------

def _read_tasks_json(path: str) -> Optional[Dict[str, Any]]:
    """Read a ``tasks.json`` file. Returns dict or None.

    - File missing: returns ``{"tasks": []}`` (a walnut with no task file
      is legitimately empty; matches upstream ``_read_json``'s
      "create with defaults" path without actually creating).
    - Malformed JSON / missing ``tasks`` key: emits ``MalformedYAMLWarning``
      and returns ``None`` so the caller can skip the file (matches
      upstream's ``strict=False`` branch, which is what
      ``_collect_all_tasks`` uses).
    - Permission / encoding error: raises ``KernelFileError`` (stronger
      than upstream, which ``sys.exit(1)``'d in strict mode and warned in
      lenient mode; raising is the right call for a library).
    """
    if not os.path.exists(path):
        return {"tasks": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        warnings.warn(
            "malformed JSON in {}: {}".format(path, exc),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return None
    except (IOError, OSError, UnicodeDecodeError) as exc:
        raise KernelFileError(
            "cannot read {}: {}".format(path, exc)
        ) from exc

    if not isinstance(data, dict) or "tasks" not in data:
        warnings.warn(
            "malformed {} (missing 'tasks' key)".format(path),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return None
    if not isinstance(data["tasks"], list):
        # Guard against schema drift -- a scalar or dict under ``tasks``
        # would crash the caller's ``list.extend()`` downstream. Emit the
        # same warning as other shape failures and skip the file.
        warnings.warn(
            "malformed {} ('tasks' is {}, expected list)".format(
                path, type(data["tasks"]).__name__
            ),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return None
    return data


def _read_completed_json(path: str) -> Dict[str, Any]:
    """Read a ``completed.json`` file. Returns dict with ``completed`` list.

    Missing file returns ``{"completed": []}``. Malformed files emit a
    warning and return the empty shape (the summary continues without
    drift data rather than failing the whole projection).
    """
    if not os.path.exists(path):
        return {"completed": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        warnings.warn(
            "malformed JSON in {}: {}".format(path, exc),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return {"completed": []}
    except (IOError, OSError, UnicodeDecodeError) as exc:
        raise KernelFileError(
            "cannot read {}: {}".format(path, exc)
        ) from exc

    if not isinstance(data, dict) or "completed" not in data:
        warnings.warn(
            "malformed {} (missing 'completed' key)".format(path),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return {"completed": []}
    if not isinstance(data["completed"], list):
        warnings.warn(
            "malformed {} ('completed' is {}, expected list)".format(
                path, type(data["completed"]).__name__
            ),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return {"completed": []}
    return data


# ---------------------------------------------------------------------------
# Task collection
# ---------------------------------------------------------------------------

def _collect_all_tasks(walnut: str) -> List[Dict[str, Any]]:
    """Return every task from every ``tasks.json`` under ``walnut``.

    Private-by-convention to match the upstream underscored name. Use
    ``collect_all_tasks`` for the public alias.
    """
    all_tasks: List[Dict[str, Any]] = []
    for tf in _all_task_files(walnut):
        data = _read_tasks_json(tf)
        if data is not None:
            all_tasks.extend(data["tasks"])
    return all_tasks


def collect_all_tasks(walnut: str) -> List[Dict[str, Any]]:
    """Public alias for ``_collect_all_tasks``."""
    return _collect_all_tasks(walnut)


# ---------------------------------------------------------------------------
# Bundle discovery (tasks.py variant -- simpler than walnut_paths.find_bundles
# because it doesn't honor nested-walnut boundaries the same way; kept for
# faithfulness to upstream summary semantics)
# ---------------------------------------------------------------------------

_BUNDLE_DISCOVERY_SKIP_DIRS = {
    "_kernel", "_core", ".git", "node_modules", "raw", "__pycache__"
}


def _find_bundles(walnut: str) -> List[Tuple[str, str]]:
    """Return list of ``(bundle_name, bundle_abs_path)`` for all bundles."""
    bundles: List[Tuple[str, str]] = []
    for root, dirs, files in os.walk(walnut):
        dirs[:] = [
            d for d in dirs
            if d not in _BUNDLE_DISCOVERY_SKIP_DIRS and not d.startswith(".")
        ]
        if "context.manifest.yaml" in files:
            bundles.append((os.path.basename(root), root))
        elif "companion.md" in files:
            bundles.append((os.path.basename(root), root))
    return bundles


def _read_manifest_field(manifest_path: str, field: str) -> Optional[str]:
    """Read a single field from ``context.manifest.yaml`` using regex.

    Handles simple ``field: value`` and multi-line ``field: |`` blocks.
    Returns ``None`` on missing file or missing field. Read failures emit
    ``MalformedYAMLWarning`` and return ``None``.
    """
    if not os.path.exists(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, OSError, UnicodeDecodeError) as exc:
        warnings.warn(
            "cannot read manifest {}: {}".format(manifest_path, exc),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return None

    pattern_block = r"^{field}:\s*[|>]-?\s*\n((?:[ \t]+.+\n?)*)".format(
        field=re.escape(field)
    )
    m = re.search(pattern_block, content, re.MULTILINE)
    if m:
        lines = m.group(1).split("\n")
        stripped = [line.strip() for line in lines if line.strip()]
        return "\n".join(stripped)

    pattern_simple = r"^{field}:\s*[\"']?(.*?)[\"']?\s*$".format(
        field=re.escape(field)
    )
    m = re.search(pattern_simple, content, re.MULTILINE)
    if m:
        return m.group(1)

    return None


def _last_squirrel(bundle_path: str) -> Optional[Dict[str, str]]:
    """Find the most recent squirrel file in a bundle's ``_squirrels/`` dir."""
    sq_dir = os.path.join(bundle_path, "_squirrels")
    if not os.path.isdir(sq_dir):
        return None
    squirrels: List[Tuple[float, str]] = []
    try:
        entries = os.listdir(sq_dir)
    except OSError:
        return None
    for f in entries:
        fp = os.path.join(sq_dir, f)
        if os.path.isfile(fp):
            try:
                squirrels.append((os.path.getmtime(fp), f))
            except OSError:
                pass
    if not squirrels:
        return None
    squirrels.sort(reverse=True)
    mtime, name = squirrels[0]
    return {
        "squirrel": name,
        "date": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"),
    }


def _dir_last_touched(bundle_path: str) -> str:
    """Return ISO date of the most recently modified file in a bundle dir."""
    latest = 0.0
    for root, _dirs, files in os.walk(bundle_path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                mt = os.path.getmtime(fp)
                if mt > latest:
                    latest = mt
            except OSError:
                pass
    if latest == 0.0:
        return "1970-01-01"
    return datetime.fromtimestamp(latest).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Summary (distilled from upstream cmd_summary)
# ---------------------------------------------------------------------------

def summary_from_walnut(
    walnut: str,
    include_items: bool = False,
) -> Dict[str, Any]:
    """Return the walnut's task summary as a dict.

    Mirrors the JSON that upstream ``tasks.py summary`` printed to stdout
    (``cmd_summary`` at tasks.py L424-L584). Keeps the same ``bundles``
    (active / recent / summary) and ``unscoped`` shape so callers that
    previously parsed the CLI output can drop in this function without
    reshaping.

    ``include_items=True`` mirrors the ``--include-items`` flag: populates
    ``active_tier[bundle]['tasks']['urgent']`` and ``['active']`` with the
    full titles instead of just counts.
    """
    include_items = bool(include_items)
    thirty_days_ago = datetime.now() - timedelta(days=30)

    # Collect tasks grouped by bundle directory name.
    bundle_tasks: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for tf in _all_task_files(walnut):
        data = _read_tasks_json(tf)
        if data is None:
            continue
        parent = os.path.basename(os.path.dirname(tf))
        bundle_name = None if parent == "_kernel" else parent
        bundle_tasks.setdefault(bundle_name, []).extend(data["tasks"])

    completed_path = os.path.join(walnut, "_kernel", "completed.json")
    completed_data = _read_completed_json(completed_path)
    completed_by_bundle: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for ct in completed_data["completed"]:
        b = ct.get("bundle")
        completed_by_bundle.setdefault(b, []).append(ct)

    known_bundles = _find_bundles(walnut)

    active_tier: Dict[str, Any] = {}
    recent_tier: Dict[str, Any] = {}
    status_counts: Dict[str, int] = {"done": 0, "draft": 0, "prototype": 0, "published": 0}

    for bundle_name, bundle_path in known_bundles:
        manifest_path = os.path.join(bundle_path, "context.manifest.yaml")
        if not os.path.exists(manifest_path):
            manifest_path = os.path.join(bundle_path, "companion.md")
        goal = _read_manifest_field(manifest_path, "goal") or ""
        status = _read_manifest_field(manifest_path, "status") or "draft"
        context = _read_manifest_field(manifest_path, "context") or ""

        tasks = bundle_tasks.get(bundle_name, [])
        c_tasks = completed_by_bundle.get(bundle_name, [])

        counts = {"urgent": 0, "active": 0, "todo": 0, "blocked": 0, "done": 0}
        urgent_titles: List[str] = []
        active_titles: List[str] = []
        assignees: set = set()

        for t in tasks:
            p = t.get("priority", "todo")
            s = t.get("status", "todo")
            if p == "urgent":
                counts["urgent"] += 1
                urgent_titles.append(t.get("title", ""))
            if s == "active":
                counts["active"] += 1
                active_titles.append(t.get("title", ""))
            elif s == "todo":
                counts["todo"] += 1
            elif s == "blocked":
                counts["blocked"] += 1
            if t.get("assignee"):
                assignees.add(t["assignee"])

        done_count = sum(1 for ct in c_tasks if ct.get("status") == "done")
        counts["done"] = done_count

        if status in status_counts:
            status_counts[status] += 1

        has_urgent = any(t.get("priority") == "urgent" for t in tasks)
        has_active = any(t.get("status") == "active" for t in tasks)

        if has_urgent or has_active:
            entry: Dict[str, Any] = {
                "status": status,
                "goal": goal,
                "context": context,
                "tasks": {"counts": counts},
                "assignees": sorted(assignees),
            }
            if include_items:
                entry["tasks"]["urgent"] = urgent_titles
                entry["tasks"]["active"] = active_titles

            last_sq = _last_squirrel(bundle_path)
            if last_sq:
                entry["last_session"] = last_sq

            active_tier[bundle_name] = entry
        else:
            last_touched_str = _dir_last_touched(bundle_path)
            try:
                last_touched_dt = datetime.strptime(last_touched_str, "%Y-%m-%d")
            except ValueError:
                last_touched_dt = datetime.min

            if last_touched_dt >= thirty_days_ago:
                recent_tier[bundle_name] = {
                    "status": status,
                    "goal": goal,
                    "counts": counts,
                    "last_touched": last_touched_str,
                }

    summary_counts: Dict[str, Any] = dict(status_counts)
    summary_counts["total"] = len(known_bundles)

    unscoped_tasks = bundle_tasks.get(None, [])
    unscoped: Dict[str, Any] = {
        "urgent": [],
        "active": [],
        "todo": [],
        "counts": {"urgent": 0, "active": 0, "todo": 0, "blocked": 0},
    }
    for t in unscoped_tasks:
        p = t.get("priority", "todo")
        s = t.get("status", "todo")
        title = t.get("title", "")
        if p == "urgent":
            unscoped["urgent"].append(title)
            unscoped["counts"]["urgent"] += 1
        if s == "active":
            unscoped["active"].append(title)
            unscoped["counts"]["active"] += 1
        elif s == "todo":
            unscoped["todo"].append(title)
            unscoped["counts"]["todo"] += 1
        elif s == "blocked":
            unscoped["counts"]["blocked"] += 1

    return {
        "bundles": {
            "active": active_tier,
            "recent": recent_tier,
            "summary": summary_counts,
        },
        "unscoped": unscoped,
    }


__all__ = [
    "_all_task_files",
    "_collect_all_tasks",
    "collect_all_tasks",
    "summary_from_walnut",
]
