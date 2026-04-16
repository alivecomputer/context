"""Pure helpers extracted from ``plugins/alive/scripts/project.py``.

UPSTREAM: ``claude-code/plugins/alive/scripts/project.py`` (see
``../VENDORING.md`` for the pinned commit hash). The upstream script is a
CLI that ``print()``s and ``sys.exit()``s. This module lifts the logic into
library form so the MCP server can call it without corrupting stdio framing
or killing the server process.

EXTRACTED FUNCTIONS (match upstream names + signatures where possible):
    - ``parse_log(walnut)``              from project.py L23-L199
    - ``scan_bundles(walnut)``           from project.py L206-L254
    - ``parse_manifest(filepath)``       from project.py L257-L304
    - ``read_unscoped_tasks(walnut)``    from project.py L351-L361
    - ``find_world_root(start)``         from project.py L368-L379
    - ``read_squirrel_sessions(walnut)`` from project.py L382-L422
    - ``scan_nested_walnuts(walnut)``    from project.py L498-L546
    - ``assemble(walnut)``               from project.py L553-L722

DIVERGENCES from upstream (documented + intentional):
    1. ``find_world_root`` raises ``WorldNotFoundError`` instead of returning
       ``None``. Downstream callers in this module catch it to preserve
       upstream semantics ("no world, empty list").
    2. ``parse_log`` raises ``KernelFileError`` on I/O error. Missing file
       still returns the empty projection (upstream behavior preserved --
       a new walnut legitimately has no log yet).
    3. ``read_squirrel_sessions`` / ``scan_nested_walnuts`` emit
       ``MalformedYAMLWarning`` via ``warnings.warn`` instead of silently
       swallowing parse failures. Callers that don't care get the same
       observable behavior; callers that do can filter warnings.
    4. ``get_task_data`` (upstream L311-L328) is NOT extracted -- it shells
       out to ``tasks.py`` via subprocess. The MCP server uses
       ``tasks_pure._collect_all_tasks`` directly instead.
    5. ``assemble`` here wires through the local ``read_unscoped_tasks``
       fallback but skips the subprocess ``tasks.py`` summary call -- the
       MCP server composes task data in its tool layer using
       ``tasks_pure.summary_from_walnut``.
    6. ``write_now_json`` (upstream L729-L742) is NOT extracted -- v0.1 is
       read-only; writing the projection is the plugin's job, not the MCP
       server's.
    7. No ``print()``; no ``sys.exit()``; no ``argparse``; no CLI entry
       point. Importing this module has zero side effects (import order
       verified by ``tests/test_vendor_smoke.py``).

Stdlib only. No PyYAML. 3.10 floor (matches pyproject ``requires-python``).
"""
from __future__ import annotations

import json
import os
import re
import warnings
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import KernelFileError, MalformedYAMLWarning, WorldNotFoundError


# ---------------------------------------------------------------------------
# 1. Log Parser
# ---------------------------------------------------------------------------

def _empty_log_data() -> Dict[str, Any]:
    return {
        "context": "",
        "phase": "unknown",
        "next": None,
        "bundle": None,
        "squirrel": None,
    }


def parse_log(walnut: str) -> Dict[str, Any]:
    """Read ``_kernel/log.md`` and extract the most recent entry.

    Returns a dict with keys: ``context``, ``phase``, ``next``, ``bundle``,
    ``squirrel``. A missing log file is NOT an error -- a fresh walnut
    legitimately has no entries yet -- and returns the empty projection.

    Raises:
        KernelFileError: the log exists but cannot be read (permission
            denied, bad encoding, OS error other than ``ENOENT``).
    """
    log_path = os.path.join(walnut, "_kernel", "log.md")
    if not os.path.isfile(log_path):
        return _empty_log_data()

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, OSError, UnicodeDecodeError) as exc:
        raise KernelFileError(
            "cannot read {}: {}".format(log_path, exc)
        ) from exc

    # Skip YAML frontmatter
    body = content
    fm_match = re.match(r"^---\s*\n.*?\n---\s*\n", content, re.DOTALL)
    if fm_match:
        body = content[fm_match.end():]

    # Find the first ## YYYY-MM-DD heading
    entry_pattern = re.compile(
        r"^## (\d{4}-\d{2}-\d{2}[^\n]*)", re.MULTILINE
    )
    matches = list(entry_pattern.finditer(body))
    if not matches:
        return _empty_log_data()

    first = matches[0]
    start = first.start()
    # End at the next ## heading or ~200 lines
    if len(matches) > 1:
        end = matches[1].start()
    else:
        # Take up to ~200 lines from the heading
        lines_from_start = body[start:].split("\n")
        end = start + len("\n".join(lines_from_start[:200]))

    entry_text = body[start:end].strip()
    heading_line = first.group(0)

    # Extract squirrel from heading (e.g. "squirrel:55ad7f1c")
    sq_match = re.search(r"squirrel[:\s]*([a-f0-9]{8})", heading_line, re.IGNORECASE)
    squirrel = sq_match.group(1) if sq_match else None

    # Build context: strip heading, signed lines, and markdown section headers
    context_lines = entry_text.split("\n")
    if context_lines:
        context_lines = context_lines[1:]
    context_lines = [
        ln for ln in context_lines
        if not re.match(r"^\s*signed:\s", ln)
        and not re.match(r"^###\s", ln)
        and not re.match(r"^\*\*Type:\*\*", ln)
    ]
    cleaned = []
    prev_blank = False
    for ln in context_lines:
        is_blank = ln.strip() == ""
        if is_blank and prev_blank:
            continue
        cleaned.append(ln)
        prev_blank = is_blank
    context_text = "\n".join(cleaned).strip()

    # Extract phase
    phase = "unknown"
    phase_match = re.search(r"phase:\s*(.+)", entry_text, re.IGNORECASE)
    if phase_match:
        phase = phase_match.group(1).strip()
    else:
        phase_keywords = {
            "launching": r"\blaunch(?:ing|ed)?\b",
            "building": r"\bbuilding\b",
            "planning": r"\bplanning\b",
            "research": r"\bresearch(?:ing)?\b",
            "designing": r"\bdesign(?:ing|ed)?\b",
            "shipping": r"\bshipp(?:ing|ed)\b",
            "maintaining": r"\bmaintain(?:ing)?\b",
            "paused": r"\bpaused?\b",
        }
        for pname, ppat in phase_keywords.items():
            if re.search(ppat, entry_text, re.IGNORECASE):
                phase = pname
                break

    # Extract next action
    next_info: Optional[Dict[str, Any]] = None
    next_section_match = re.search(
        r"### Next\s*\n(.*?)(?=\n### |\n## |\Z)", entry_text, re.DOTALL
    )
    if next_section_match:
        next_text = next_section_match.group(1).strip()
        next_lines = [
            ln for ln in next_text.split("\n")
            if not re.match(r"^\s*signed:\s", ln, re.IGNORECASE)
        ]
        next_text = "\n".join(next_lines).strip()

        sentences = re.split(r"(?<=\.)\s+", next_text, maxsplit=1)
        action = sentences[0].strip() if sentences else next_text
        why = sentences[1].strip() if len(sentences) > 1 else None

        next_info = {"action": action, "bundle": None, "why": why}

        bundle_ref = re.search(
            r"(?:^|\s)bundle:\s*([a-z0-9_-]+(?:/[a-z0-9_-]+)*)",
            next_text, re.IGNORECASE
        )
        if bundle_ref:
            next_info["bundle"] = bundle_ref.group(1)
    else:
        next_line_match = re.search(
            r"(?:^|\n)\s*(?:\*\*)?next(?:\*\*)?[:\s]+(.+)",
            entry_text, re.IGNORECASE
        )
        if next_line_match:
            next_info = {
                "action": next_line_match.group(1).strip(),
                "bundle": None,
                "why": None,
            }

    # Extract bundle reference from "What Was Built" or bundle: mentions
    bundle: Optional[str] = None
    bundle_match = re.search(r"bundle:\s*(\S+)", entry_text, re.IGNORECASE)
    if bundle_match:
        bundle = bundle_match.group(1).strip()
    else:
        built_match = re.search(
            r"### What Was Built\s*\n(.*?)(?=\n### |\n## |\Z)",
            entry_text, re.DOTALL
        )
        if built_match:
            path_match = re.search(
                r"`?(?:bundles/)?([a-z0-9_-]+)/`?",
                built_match.group(1)
            )
            if path_match:
                bundle = path_match.group(1)

    return {
        "context": context_text,
        "phase": phase,
        "next": next_info,
        "bundle": bundle,
        "squirrel": squirrel,
    }


# ---------------------------------------------------------------------------
# 2. Bundle Scanner
# ---------------------------------------------------------------------------

def scan_bundles(walnut: str) -> Dict[str, Dict[str, Any]]:
    """Walk walnut recursively finding ``context.manifest.yaml`` files.

    Returns a dict keyed by bundle path relative to walnut. Skips
    ``_kernel/``, ``raw/``, ``.git``, hidden dirs, ``node_modules``, and
    directories inside nested walnuts. Malformed manifests are dropped from
    the result with a ``MalformedYAMLWarning``.
    """
    bundles: Dict[str, Dict[str, Any]] = {}
    skip_dirs = {"_kernel", "raw", ".git", "node_modules", "__pycache__",
                 "dist", "build", ".next", "target"}
    nested_walnut_roots: set = set()

    for root, dirs, files in os.walk(walnut):
        rel = os.path.relpath(root, walnut)

        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d not in skip_dirs
        ]

        inside_nested = False
        for nw in nested_walnut_roots:
            if rel.startswith(nw + os.sep) or rel == nw:
                inside_nested = True
                break
        if inside_nested:
            continue

        if rel != "." and "_kernel" in dirs:
            kernel_key = os.path.join(root, "_kernel", "key.md")
            if os.path.isfile(kernel_key):
                nested_walnut_roots.add(rel)
                dirs[:] = []
                continue

        if "context.manifest.yaml" in files:
            manifest_path = os.path.join(root, "context.manifest.yaml")
            bundle_name = os.path.relpath(root, walnut)
            parsed = parse_manifest(manifest_path)
            if parsed is not None:
                bundles[bundle_name] = parsed

    return bundles


def parse_manifest(filepath: str) -> Optional[Dict[str, Any]]:
    """Parse ``context.manifest.yaml`` using regex only. Returns dict or None.

    Returns ``None`` only when the file cannot be read; an empty-but-readable
    file returns ``{"active_sessions": []}``. Read failures emit
    ``MalformedYAMLWarning`` so callers can observe drift without crashing.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, OSError, UnicodeDecodeError) as exc:
        warnings.warn(
            "cannot read manifest {}: {}".format(filepath, exc),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return None

    result: Dict[str, Any] = {}

    for field in ("goal", "status", "updated", "due"):
        m = re.search(
            r"^{field}:\s*['\"]?(.*?)['\"]?\s*$".format(field=re.escape(field)),
            content, re.MULTILINE
        )
        if m:
            result[field] = m.group(1).strip()

    ctx_block = re.search(
        r"^context:\s*[|>]-?\s*\n((?:[ \t]+.+\n?)*)",
        content, re.MULTILINE
    )
    if ctx_block:
        lines = ctx_block.group(1).split("\n")
        stripped = [ln.strip() for ln in lines if ln.strip()]
        result["context"] = "\n".join(stripped)
    else:
        ctx_simple = re.search(
            r"^context:\s*['\"]?(.*?)['\"]?\s*$",
            content, re.MULTILINE
        )
        if ctx_simple:
            result["context"] = ctx_simple.group(1)

    sessions: List[str] = []
    sq_match = re.search(
        r"^squirrels:\s*\n((?:[ \t]*-\s*.+\n?)*)",
        content, re.MULTILINE
    )
    if sq_match:
        for item in re.finditer(r"-\s*(\S+)", sq_match.group(1)):
            sessions.append(item.group(1))
    result["active_sessions"] = sessions

    return result


# ---------------------------------------------------------------------------
# 3. Unscoped task reader (fallback for assemble)
# ---------------------------------------------------------------------------

def read_unscoped_tasks(walnut: str) -> List[Dict[str, Any]]:
    """Read ``_kernel/tasks.json`` directly as the unscoped-task fallback.

    Returns an empty list when the file is absent (new walnut) or malformed.
    Malformed files emit ``MalformedYAMLWarning`` so the caller can observe
    drift. Unlike upstream, permission errors propagate as
    ``KernelFileError`` -- the file exists but we can't read it, which the
    caller needs to know about.
    """
    tasks_path = os.path.join(walnut, "_kernel", "tasks.json")
    if not os.path.isfile(tasks_path):
        return []
    try:
        with open(tasks_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        warnings.warn(
            "malformed tasks.json at {}: {}".format(tasks_path, exc),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return []
    except (IOError, OSError, UnicodeDecodeError) as exc:
        raise KernelFileError(
            "cannot read {}: {}".format(tasks_path, exc)
        ) from exc

    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    return list(tasks) if isinstance(tasks, list) else []


# ---------------------------------------------------------------------------
# 4. World-root discovery
# ---------------------------------------------------------------------------

def find_world_root(start: str) -> str:
    """Walk UP from ``start`` to find the directory containing ``.alive/``.

    Returns the absolute path of the World root.

    Raises:
        WorldNotFoundError: no ancestor of ``start`` contains ``.alive/``.

    Upstream returns ``None`` when no World is found; we raise so callers
    can surface ``ERR_NO_WORLD`` per the v0.1 error taxonomy.
    ``read_squirrel_sessions`` and ``scan_nested_walnuts`` below catch this
    internally to preserve their upstream "return empty on miss" semantics.
    """
    current = os.path.abspath(start)
    while True:
        alive_dir = os.path.join(current, ".alive")
        if os.path.isdir(alive_dir):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise WorldNotFoundError(
        "no ancestor of {} contains .alive/".format(os.path.abspath(start))
    )


# ---------------------------------------------------------------------------
# 5. Squirrel sessions
# ---------------------------------------------------------------------------

def read_squirrel_sessions(walnut: str) -> List[Dict[str, Any]]:
    """Read up to 5 most-recent squirrel sessions for ``walnut``.

    Looks inside ``<world>/.alive/_squirrels/``. Sessions that can't be
    parsed emit ``MalformedYAMLWarning`` and are skipped. Returns ``[]``
    if no World is found -- upstream-compatible behavior.
    """
    try:
        world_root = find_world_root(walnut)
    except WorldNotFoundError:
        return []

    sq_dir = os.path.join(world_root, ".alive", "_squirrels")
    if not os.path.isdir(sq_dir):
        return []

    walnut_name = os.path.basename(os.path.abspath(walnut))

    session_files: List[tuple] = []
    try:
        for fname in os.listdir(sq_dir):
            if not fname.endswith(".yaml"):
                continue
            fpath = os.path.join(sq_dir, fname)
            if os.path.isfile(fpath):
                try:
                    mtime = os.path.getmtime(fpath)
                    session_files.append((mtime, fpath))
                except OSError:
                    pass
    except OSError:
        return []

    session_files.sort(reverse=True)

    sessions: List[Dict[str, Any]] = []
    for _mtime, fpath in session_files:
        if len(sessions) >= 5:
            break
        parsed = _parse_squirrel_yaml(fpath, walnut_name)
        if parsed is not None:
            sessions.append(parsed)

    return sessions


def _parse_squirrel_yaml(filepath: str, walnut_name: str) -> Optional[Dict[str, Any]]:
    """Parse a squirrel YAML file using regex. Returns dict or None."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, OSError, UnicodeDecodeError) as exc:
        warnings.warn(
            "cannot read squirrel entry {}: {}".format(filepath, exc),
            MalformedYAMLWarning,
            stacklevel=2,
        )
        return None

    walnut_field = _extract_yaml_field(content, "walnut")
    alive_field = _extract_yaml_field(content, "alive")

    matched_walnut = walnut_field or alive_field
    if not matched_walnut or matched_walnut == "null":
        return None
    if matched_walnut != walnut_name:
        return None

    session_id = _extract_yaml_field(content, "session_id") or ""
    started = _extract_yaml_field(content, "started") or ""
    bundle = _extract_yaml_field(content, "bundle") or ""
    recovery_state = _extract_yaml_field(content, "recovery_state") or ""
    engine = _extract_yaml_field(content, "engine") or ""

    squirrel_short = session_id[:8] if session_id else ""

    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", started)
    date = date_match.group(1) if date_match else ""

    return {
        "squirrel": squirrel_short,
        "date": date,
        "bundle": bundle if bundle and bundle != "null" else None,
        "engine": engine if engine and engine != "null" else None,
        "summary": recovery_state if recovery_state and recovery_state != "null" else None,
    }


def _extract_yaml_field(content: str, field: str) -> Optional[str]:
    """Extract a simple field value from YAML content using regex."""
    m = re.search(
        r'^{field}:\s*"((?:[^"\\]|\\.)*)"\s*$'.format(field=re.escape(field)),
        content, re.MULTILINE
    )
    if m:
        return m.group(1).replace('\\"', '"')

    m = re.search(
        r"^{field}:\s*'((?:[^'\\]|\\.)*)'\s*$".format(field=re.escape(field)),
        content, re.MULTILINE
    )
    if m:
        return m.group(1)

    m = re.search(
        r"^{field}:\s*(.+?)\s*$".format(field=re.escape(field)),
        content, re.MULTILINE
    )
    if m:
        return m.group(1).strip()

    return None


# ---------------------------------------------------------------------------
# 6. Nested walnut scanner
# ---------------------------------------------------------------------------

def scan_nested_walnuts(walnut: str) -> Dict[str, Dict[str, Any]]:
    """Find nested walnuts (one level deep) and read their ``now.json``.

    Returns a dict keyed by child directory name. Unreadable child
    ``now.json`` files emit ``MalformedYAMLWarning`` and are omitted from
    the result with a placeholder ``{phase: unknown, next: None, updated: None}``.
    """
    children: Dict[str, Dict[str, Any]] = {}
    skip_dirs = {"_kernel", "raw", ".git", "node_modules", "__pycache__",
                 "dist", "build", ".next", "target"}

    try:
        entries = os.listdir(walnut)
    except OSError:
        return children

    for entry in entries:
        if entry.startswith(".") or entry in skip_dirs:
            continue
        entry_path = os.path.join(walnut, entry)
        if not os.path.isdir(entry_path):
            continue

        kernel_key = os.path.join(entry_path, "_kernel", "key.md")
        if not os.path.isfile(kernel_key):
            continue

        child_info: Dict[str, Any] = {
            "phase": "unknown", "next": None, "updated": None
        }

        for now_path in [
            os.path.join(entry_path, "_kernel", "now.json"),
            os.path.join(entry_path, "_kernel", "_generated", "now.json"),
        ]:
            if os.path.isfile(now_path):
                try:
                    with open(now_path, "r", encoding="utf-8") as f:
                        now_data = json.load(f)
                except (IOError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    warnings.warn(
                        "cannot read {}: {}".format(now_path, exc),
                        MalformedYAMLWarning,
                        stacklevel=2,
                    )
                    break
                child_info["phase"] = now_data.get("phase", "unknown")
                next_val = now_data.get("next")
                if isinstance(next_val, dict):
                    child_info["next"] = next_val.get("action")
                elif isinstance(next_val, str):
                    child_info["next"] = next_val
                child_info["updated"] = now_data.get("updated")
                break

        children[entry] = child_info

    return children


# ---------------------------------------------------------------------------
# 7. Assembly (pure -- no tasks.py subprocess call)
# ---------------------------------------------------------------------------

def _empty_task_data() -> Dict[str, Any]:
    return {
        "bundles": {
            "active": {},
            "recent": {},
            "summary": {"total": 0, "done": 0, "draft": 0, "prototype": 0, "published": 0},
        },
        "unscoped": {
            "urgent": [],
            "active": [],
            "todo": [],
            "counts": {"urgent": 0, "active": 0, "todo": 0, "blocked": 0},
        },
    }


def assemble(
    walnut: str,
    task_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Combine all sources into the ``now.json`` projection.

    Divergence from upstream: upstream ``assemble`` shells out to
    ``tasks.py summary`` via subprocess. This pure version accepts
    ``task_data`` as an optional parameter -- callers supply it by calling
    ``tasks_pure.summary_from_walnut(walnut)`` themselves (or pass ``None``
    to use only the direct ``_kernel/tasks.json`` fallback). This keeps the
    module a pure function of its inputs: no subprocess, no surprise I/O
    beyond the filesystem reads the helpers already perform.
    """
    # Parse sources, catching errors individually so one bad file doesn't
    # kill the whole projection.
    try:
        log_data = parse_log(walnut)
    except KernelFileError:
        log_data = _empty_log_data()

    if task_data is None:
        task_data = _empty_task_data()

    try:
        manifest_bundles = scan_bundles(walnut)
    except OSError:
        manifest_bundles = {}

    try:
        sessions = read_squirrel_sessions(walnut)
    except OSError:
        sessions = []

    try:
        children = scan_nested_walnuts(walnut)
    except OSError:
        children = {}

    try:
        direct_unscoped = read_unscoped_tasks(walnut)
    except KernelFileError:
        direct_unscoped = []

    # --- Merge bundles from task_data with manifest data ---
    td_bundles = task_data.get("bundles", {})
    active_tier = dict(td_bundles.get("active", {}))
    recent_tier = dict(td_bundles.get("recent", {}))
    summary_counts = dict(td_bundles.get("summary", {
        "total": 0, "done": 0, "draft": 0, "prototype": 0, "published": 0,
    }))

    for bundle_path, manifest in manifest_bundles.items():
        bundle_name = os.path.basename(bundle_path)

        target_key = None
        target_tier = None
        for key in [bundle_path, bundle_name]:
            if key in active_tier:
                target_key = key
                target_tier = active_tier
                break
            if key in recent_tier:
                target_key = key
                target_tier = recent_tier
                break

        if target_tier is not None and target_key is not None:
            existing = target_tier[target_key]
            if manifest.get("goal") and not existing.get("goal"):
                existing["goal"] = manifest["goal"]
            if manifest.get("status") and not existing.get("status"):
                existing["status"] = manifest["status"]
            if manifest.get("context") and not existing.get("context"):
                existing["context"] = manifest["context"]
            if manifest.get("updated") and not existing.get("updated"):
                existing["updated"] = manifest["updated"]
            if manifest.get("due"):
                existing["due"] = manifest["due"]
        else:
            status = manifest.get("status", "draft")
            summary_counts["total"] = summary_counts.get("total", 0) + 1
            if status in summary_counts:
                summary_counts[status] = summary_counts.get(status, 0) + 1

    # --- Unscoped tasks ---
    unscoped = task_data.get("unscoped", {
        "urgent": [], "active": [], "todo": [],
        "counts": {"urgent": 0, "active": 0, "todo": 0, "blocked": 0},
    })

    unscoped_counts = unscoped.get("counts", {})
    total_unscoped = sum(unscoped_counts.get(k, 0) for k in ("urgent", "active", "todo", "blocked"))
    if total_unscoped == 0 and direct_unscoped:
        u_urgent, u_active, u_todo = [], [], []
        u_counts = {"urgent": 0, "active": 0, "todo": 0, "blocked": 0}
        for t in direct_unscoped:
            status = t.get("status", "todo")
            priority = t.get("priority", "todo")
            title = t.get("title", "")
            if priority == "urgent":
                u_urgent.append(title)
                u_counts["urgent"] += 1
            if status == "active":
                u_active.append(title)
                u_counts["active"] += 1
            elif status == "todo":
                u_todo.append(title)
                u_counts["todo"] += 1
            elif status == "blocked":
                u_counts["blocked"] += 1
        unscoped = {
            "urgent": u_urgent,
            "active": u_active,
            "todo": u_todo,
            "counts": u_counts,
        }

    # --- Blockers ---
    blockers: List[Dict[str, Any]] = []
    for bname, bdata in active_tier.items():
        tasks_info = bdata.get("tasks", {})
        counts = tasks_info.get("counts", {})
        if counts.get("blocked", 0) > 0:
            blockers.append({
                "bundle": bname,
                "blocked_count": counts["blocked"],
            })
    if unscoped.get("counts", {}).get("blocked", 0) > 0:
        blockers.append({
            "scope": "unscoped",
            "blocked_count": unscoped["counts"]["blocked"],
        })

    # --- Most recent squirrel ---
    most_recent_squirrel = log_data.get("squirrel")
    if not most_recent_squirrel and sessions:
        most_recent_squirrel = sessions[0].get("squirrel")

    phase = log_data.get("phase", "unknown")
    next_field = log_data.get("next")
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    now: Dict[str, Any] = {
        "phase": phase,
        "updated": updated,
        "squirrel": most_recent_squirrel,
        "next": next_field,
        "bundles": {
            "active": active_tier if active_tier else {},
            "recent": recent_tier if recent_tier else {},
            "summary": summary_counts,
        },
        "unscoped_tasks": unscoped,
        "recent_sessions": sessions if sessions else [],
        "children": children if children else {},
        "blockers": blockers if blockers else [],
        "context": log_data.get("context", ""),
    }

    return now


__all__ = [
    "parse_log",
    "scan_bundles",
    "parse_manifest",
    "read_unscoped_tasks",
    "find_world_root",
    "read_squirrel_sessions",
    "scan_nested_walnuts",
    "assemble",
]
