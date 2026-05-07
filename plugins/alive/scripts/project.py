#!/usr/bin/env python3
"""ALIVE Context System -- Projection builder (project.py).

Reads all source files in a walnut and assembles _kernel/now.json.
Runs post-save via hook. The agent NEVER writes now.json -- this script does.

Usage: python3 project.py --walnut /path/to/walnut
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# Shared helpers (see scripts/_common.py).
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _common import atomic_write_json  # noqa: E402
from _common import find_world_root as _common_find_world_root  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Log Parser
# ---------------------------------------------------------------------------

def parse_log(walnut):
    """Read _kernel/log.md and extract the most recent entry."""
    log_path = os.path.join(walnut, "_kernel", "log.md")
    if not os.path.isfile(log_path):
        return {
            "context": "",
            "phase": "unknown",
            "bundle": None,
            "squirrel": None,
        }

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return {
            "context": "",
            "phase": "unknown",
            "bundle": None,
            "squirrel": None,
        }

    # Skip YAML frontmatter
    body = content
    fm_match = re.match(r"^---\s*\n.*?\n---\s*\n", content, re.DOTALL)
    if fm_match:
        body = content[fm_match.end():]

    # Strip single-line HTML comments (e.g. ``<!-- entry-hash: abc12345 -->``)
    # whose only content is the comment. Multi-line comments are not expected
    # in the log-entry layout and are deliberately out of scope. Anchored at
    # ``^\s*<!--.*?-->\s*$`` per line so a legitimate inline ``<!-- x -->``
    # appearing mid-prose is preserved.
    body = re.sub(
        r"(?m)^\s*<!--.*?-->\s*$\n?",
        "",
        body,
    )

    # Find the first ## YYYY-MM-DD heading
    entry_pattern = re.compile(
        r"^## (\d{4}-\d{2}-\d{2}[^\n]*)", re.MULTILINE
    )
    matches = list(entry_pattern.finditer(body))
    if not matches:
        return {
            "context": "",
            "phase": "unknown",
            "bundle": None,
            "squirrel": None,
        }

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
    # Remove the ## heading line
    if context_lines:
        context_lines = context_lines[1:]
    # Remove signed: lines, markdown section headers (### ), and empty metadata
    context_lines = [
        ln for ln in context_lines
        if not re.match(r"^\s*signed:\s", ln)
        and not re.match(r"^###\s", ln)
        and not re.match(r"^\*\*Type:\*\*", ln)
    ]
    # Collapse multiple blank lines
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
        # Look for phase-like words in the narrative
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

    # Extract bundle reference from "What Was Built" or bundle: mentions
    bundle = None
    bundle_match = re.search(r"bundle:\s*(\S+)", entry_text, re.IGNORECASE)
    if bundle_match:
        bundle = bundle_match.group(1).strip()
    else:
        # Look in "What Was Built" section
        built_match = re.search(
            r"### What Was Built\s*\n(.*?)(?=\n### |\n## |\Z)",
            entry_text, re.DOTALL
        )
        if built_match:
            # Extract first bundle-like path mention
            path_match = re.search(
                r"`?(?:bundles/)?([a-z0-9_-]+)/`?",
                built_match.group(1)
            )
            if path_match:
                bundle = path_match.group(1)

    return {
        "context": context_text,
        "phase": phase,
        "bundle": bundle,
        "squirrel": squirrel,
    }


# ---------------------------------------------------------------------------
# 2. Bundle Scanner
# ---------------------------------------------------------------------------

def scan_bundles(walnut):
    """Walk walnut recursively finding context.manifest.yaml files.

    Returns dict keyed by bundle path relative to walnut.
    Skips _kernel/, raw/, .git, hidden dirs, node_modules, and
    directories inside nested walnuts.
    """
    bundles = {}
    skip_dirs = {"_kernel", "raw", ".git", "node_modules", "__pycache__",
                 "dist", "build", ".next", "target"}

    # Track nested walnut roots so we don't scan inside them
    nested_walnut_roots = set()

    for root, dirs, files in os.walk(walnut):
        rel = os.path.relpath(root, walnut)

        # Skip hidden directories
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d not in skip_dirs
        ]

        # If we're inside a nested walnut, skip
        inside_nested = False
        for nw in nested_walnut_roots:
            if rel.startswith(nw + os.sep) or rel == nw:
                inside_nested = True
                break
        if inside_nested:
            continue

        # Detect nested walnuts (directories with _kernel/key.md)
        if rel != "." and "_kernel" in dirs:
            kernel_key = os.path.join(root, "_kernel", "key.md")
            if os.path.isfile(kernel_key):
                nested_walnut_roots.add(rel)
                # Don't scan further into this nested walnut for bundles
                dirs[:] = []
                continue

        if "context.manifest.yaml" in files:
            manifest_path = os.path.join(root, "context.manifest.yaml")
            bundle_name = os.path.relpath(root, walnut)
            parsed = parse_manifest(manifest_path)
            if parsed is not None:
                bundles[bundle_name] = parsed

    return bundles


def parse_manifest(filepath):
    """Parse context.manifest.yaml using regex only. Returns dict or None."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return None

    result = {}

    # Simple single-line fields
    for field in ("goal", "status", "updated", "due"):
        m = re.search(
            r"^{field}:\s*['\"]?(.*?)['\"]?\s*$".format(field=re.escape(field)),
            content, re.MULTILINE
        )
        if m:
            result[field] = m.group(1).strip()

    # Context field -- may be multi-line block scalar
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

    # Active sessions
    sessions = []
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
# 3. Task Integration
# ---------------------------------------------------------------------------

def get_task_data(walnut):
    """Call tasks.py summary and return parsed JSON, or empty structure."""
    tasks_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks.py")

    if not os.path.isfile(tasks_script):
        return _empty_task_data()

    try:
        result = subprocess.run(
            ["python3", tasks_script, "summary", "--walnut", walnut, "--include-items"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass

    return _empty_task_data()


def _empty_task_data():
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


# ---------------------------------------------------------------------------
# 4. Unscoped Tasks (direct read of _kernel/tasks.json)
# ---------------------------------------------------------------------------

def read_unscoped_tasks(walnut):
    """Read _kernel/tasks.json directly as a fallback for unscoped tasks."""
    tasks_path = os.path.join(walnut, "_kernel", "tasks.json")
    if not os.path.isfile(tasks_path):
        return []
    try:
        with open(tasks_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tasks", [])
    except (IOError, json.JSONDecodeError, UnicodeDecodeError):
        return []


# ---------------------------------------------------------------------------
# 5. Squirrel Session Reader
# ---------------------------------------------------------------------------

def find_world_root(walnut):
    """Locate the ALIVE world root for `walnut`, or return None on miss.

    Delegates to ``_common.find_world_root`` (which ports the bash
    sibling's full tier order: ``$ALIVE_WORLD_ROOT_OVERRIDE`` ->
    ``~/.config/alive/world-root`` (with legacy walnut migration) ->
    bootstrap cwd walk-up via ``is_valid_world_root``). The shared
    finder raises FileNotFoundError when no tier matches;
    ``project.py`` callers treat a miss as "no squirrel sessions
    available", so we translate back to None.
    """
    try:
        return _common_find_world_root(walnut)
    except FileNotFoundError:
        return None


def read_squirrel_sessions(walnut):
    """Read recent squirrel sessions from .alive/_squirrels/."""
    world_root = find_world_root(walnut)
    if not world_root:
        return []

    sq_dir = os.path.join(world_root, ".alive", "_squirrels")
    if not os.path.isdir(sq_dir):
        return []

    walnut_name = os.path.basename(os.path.abspath(walnut))

    # Collect all session files with mtime
    session_files = []
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

    # Sort by mtime descending (most recent first)
    session_files.sort(reverse=True)

    # Parse each and filter to this walnut
    sessions = []
    for mtime, fpath in session_files:
        if len(sessions) >= 5:
            break
        parsed = _parse_squirrel_yaml(fpath, walnut_name)
        if parsed is not None:
            sessions.append(parsed)

    return sessions


def _parse_squirrel_yaml(filepath, walnut_name):
    """Parse a squirrel YAML file using regex. Returns dict or None."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return None

    # Check walnut field -- match against walnut name
    # Sessions use either walnut: or alive: field
    walnut_field = _extract_yaml_field(content, "walnut")
    alive_field = _extract_yaml_field(content, "alive")

    matched_walnut = walnut_field or alive_field
    if not matched_walnut or matched_walnut == "null":
        return None
    if matched_walnut != walnut_name:
        return None

    session_id = _extract_yaml_field(content, "session_id") or ""
    started = _extract_yaml_field(content, "started") or ""
    ended = _extract_yaml_field(content, "ended") or ""
    bundle = _extract_yaml_field(content, "bundle") or ""
    recovery_state = _extract_yaml_field(content, "recovery_state") or ""
    engine = _extract_yaml_field(content, "engine") or ""

    squirrel_short = session_id[:8] if session_id else ""

    # Extract date from started field
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", started)
    date = date_match.group(1) if date_match else ""

    return {
        "squirrel": squirrel_short,
        "date": date,
        "bundle": bundle if bundle and bundle != "null" else None,
        "engine": engine if engine and engine != "null" else None,
        "summary": recovery_state if recovery_state and recovery_state != "null" else None,
    }


def _extract_yaml_field(content, field):
    """Extract a simple field value from YAML content using regex."""
    # Handle quoted values
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

    # Unquoted
    m = re.search(
        r"^{field}:\s*(.+?)\s*$".format(field=re.escape(field)),
        content, re.MULTILINE
    )
    if m:
        return m.group(1).strip()

    return None


# ---------------------------------------------------------------------------
# 6. Nested Walnut Scanner
# ---------------------------------------------------------------------------

def scan_nested_walnuts(walnut):
    """Find nested walnuts (one level deep) and read their now.json."""
    children = {}
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

        # Check if this is a nested walnut (has _kernel/key.md)
        kernel_key = os.path.join(entry_path, "_kernel", "key.md")
        if not os.path.isfile(kernel_key):
            continue

        # Read its now.json if it exists
        child_info = {"phase": "unknown", "updated": None}

        # Try v3 path first, then v2 path
        for now_path in [
            os.path.join(entry_path, "_kernel", "now.json"),
            os.path.join(entry_path, "_kernel", "_generated", "now.json"),
        ]:
            if os.path.isfile(now_path):
                try:
                    with open(now_path, "r", encoding="utf-8") as f:
                        now_data = json.load(f)
                    child_info["phase"] = now_data.get("phase", "unknown")
                    child_info["updated"] = now_data.get("updated")
                    break
                except (IOError, json.JSONDecodeError, UnicodeDecodeError):
                    pass

        children[entry] = child_info

    return children


# ---------------------------------------------------------------------------
# 7. Assembly
# ---------------------------------------------------------------------------

def assemble(walnut):
    """Combine all sources into the now.json projection."""
    # Parse all sources, catching errors individually
    try:
        log_data = parse_log(walnut)
    except Exception:
        log_data = {
            "context": "", "phase": "unknown",
            "bundle": None, "squirrel": None,
        }

    try:
        task_data = get_task_data(walnut)
    except Exception:
        task_data = _empty_task_data()

    try:
        manifest_bundles = scan_bundles(walnut)
    except Exception:
        manifest_bundles = {}

    try:
        sessions = read_squirrel_sessions(walnut)
    except Exception:
        sessions = []

    try:
        children = scan_nested_walnuts(walnut)
    except Exception:
        children = {}

    # Read unscoped tasks directly as fallback
    try:
        direct_unscoped = read_unscoped_tasks(walnut)
    except Exception:
        direct_unscoped = []

    # --- Merge bundles from task_data with manifest data ---
    td_bundles = task_data.get("bundles", {})
    active_tier = dict(td_bundles.get("active", {}))
    recent_tier = dict(td_bundles.get("recent", {}))
    summary_counts = dict(td_bundles.get("summary", {
        "total": 0, "done": 0, "draft": 0, "prototype": 0, "published": 0,
    }))

    # Merge manifest data into active/recent tiers
    for bundle_path, manifest in manifest_bundles.items():
        # The bundle name for matching could be the last segment of the path
        # or the full relative path. Try both.
        bundle_name = os.path.basename(bundle_path)

        # Check if this bundle is in active or recent tier from task_data
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
            # Merge manifest fields into existing tier entry
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
            # Bundle is only in manifests (not in task_data).
            # Add it to summary counts.
            status = manifest.get("status", "draft")
            summary_counts["total"] = summary_counts.get("total", 0) + 1
            if status in summary_counts:
                summary_counts[status] = summary_counts.get(status, 0) + 1

    # --- Unscoped tasks ---
    unscoped = task_data.get("unscoped", {
        "urgent": [], "active": [], "todo": [],
        "counts": {"urgent": 0, "active": 0, "todo": 0, "blocked": 0},
    })

    # If task_data unscoped is empty but we have direct unscoped tasks, use those
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
    blockers = []
    # From active tier bundles
    for bname, bdata in active_tier.items():
        tasks_info = bdata.get("tasks", {})
        counts = tasks_info.get("counts", {})
        if counts.get("blocked", 0) > 0:
            blockers.append({
                "bundle": bname,
                "blocked_count": counts["blocked"],
            })
    # From unscoped
    if unscoped.get("counts", {}).get("blocked", 0) > 0:
        blockers.append({
            "scope": "unscoped",
            "blocked_count": unscoped["counts"]["blocked"],
        })

    # --- Determine most recent squirrel ---
    most_recent_squirrel = log_data.get("squirrel")
    if not most_recent_squirrel and sessions:
        most_recent_squirrel = sessions[0].get("squirrel")

    # --- Phase ---
    phase = log_data.get("phase", "unknown")

    # --- Timestamp ---
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Assemble final structure ---
    now = {
        "phase": phase,
        "updated": updated,
        "squirrel": most_recent_squirrel,
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


# ---------------------------------------------------------------------------
# 8. Write
# ---------------------------------------------------------------------------

def write_now_json(walnut, data):
    """Write now.json atomically to _kernel/now.json via _common helper."""
    target = os.path.join(walnut, "_kernel", "now.json")
    atomic_write_json(target, data)
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ALIVE Context System -- build _kernel/now.json projection"
    )
    parser.add_argument(
        "--walnut", required=True,
        help="Path to the walnut directory"
    )
    args = parser.parse_args()

    walnut = os.path.abspath(args.walnut)
    if not os.path.isdir(walnut):
        print("Error: not a directory: {}".format(walnut), file=sys.stderr)
        sys.exit(1)

    data = assemble(walnut)
    target = write_now_json(walnut, data)
    print("Wrote {}".format(target))


if __name__ == "__main__":
    main()
