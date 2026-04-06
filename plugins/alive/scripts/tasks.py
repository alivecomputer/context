#!/usr/bin/env python3
"""ALIVE Context System -- Task management CLI.

The agent never reads/writes task files directly; it calls this script instead.

Subcommands: add, done, drop, edit, list, summary
"""

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _read_json(path, key, strict=True):
    """Read a JSON file. Create with {key: []} if missing.

    If strict=False, return None on malformed files instead of exiting.
    This allows callers like _collect_all_tasks to skip bad files gracefully.
    """
    if not os.path.exists(path):
        return {key: []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or key not in data:
            if strict:
                print("Error: malformed {}".format(path), file=sys.stderr)
                sys.exit(1)
            print("Warning: skipping malformed {}".format(path), file=sys.stderr)
            return None
        return data
    except json.JSONDecodeError:
        if strict:
            print("Error: malformed JSON in {}".format(path), file=sys.stderr)
            sys.exit(1)
        print("Warning: skipping malformed JSON in {}".format(path), file=sys.stderr)
        return None


def _atomic_write(path, data):
    """Write JSON atomically via .tmp + os.replace()."""
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _next_id(tasks):
    """Find the highest tNNN across a list of tasks and return the next one."""
    highest = 0
    for t in tasks:
        m = re.match(r"^t(\d+)$", t.get("id", ""))
        if m:
            highest = max(highest, int(m.group(1)))
    return "t{:03d}".format(highest + 1)


# ---------------------------------------------------------------------------
# v2 tasks.md → v3 tasks.json migration
# ---------------------------------------------------------------------------

# Section headers map to priority values
_SECTION_PRIORITY = {
    "urgent": "urgent",
    "active": "active",
    "to do": "todo",
    "todo": "todo",
    "done": "done",
    "done (recent)": "done",
    "completed": "done",
}


def _parse_task_line(line, priority, counter):
    """Parse a single markdown task line into a v3 task dict.

    Handles:
      - [ ] Task text @session_id (by DATE)
      - [x] Task text @session_id (DATE)
    """
    # Determine done status from checkbox
    done_match = re.match(r"^\s*-\s*\[([ xX])\]\s*(.*)", line)
    if not done_match:
        return None
    is_done = done_match.group(1).lower() == "x"
    text = done_match.group(2).strip()
    if not text:
        return None

    # Extract session/assignee (@hexid)
    session = None
    session_match = re.search(r"@([a-f0-9]{6,12})\b", text)
    if session_match:
        session = session_match.group(1)
        text = text[:session_match.start()].rstrip() + text[session_match.end():]
        text = text.strip()

    # Extract @urgent tag (not a session id)
    if "@urgent" in text:
        priority = "urgent"
        text = text.replace("@urgent", "").strip()

    # Extract due date: (by DATE) or (by EOD DATE)
    due = None
    due_match = re.search(r"\(by\s+(?:EOD\s+)?(\d{4}-\d{2}-\d{2})\)", text)
    if due_match:
        due = due_match.group(1)
        text = text[:due_match.start()].rstrip() + text[due_match.end():]
        text = text.strip()

    # Extract completion date: (DATE) at end
    completed_date = None
    if is_done:
        comp_match = re.search(r"\((\d{4}-\d{2}-\d{2})\)\s*$", text)
        if comp_match:
            completed_date = comp_match.group(1)
            text = text[:comp_match.start()].strip()

    # Clean up trailing/leading punctuation artifacts
    text = text.strip(" ,;-")

    task = {
        "id": "t{:03d}".format(counter),
        "title": text,
        "status": "done" if is_done else ("active" if priority == "active" else "todo"),
        "priority": priority if not is_done else "todo",
        "assignee": None,
        "due": due,
        "tags": [],
        "created": completed_date or _today(),
        "session": session or "migrated",
    }

    if is_done and completed_date:
        task["completed"] = completed_date

    return task


def _migrate_tasks_md(md_path, json_path):
    """Migrate a v2 tasks.md to v3 tasks.json + completed.json.

    - Parses section headers (## Urgent, ## Active, ## To Do, ## Done)
    - Parses checkbox lines with session IDs, dates, tags
    - Writes tasks.json (open tasks) and completed.json (done tasks)
    - Renames tasks.md → tasks.md.v2-backup
    - Returns the path to the new tasks.json
    """
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")
    current_priority = "todo"
    counter = 1
    open_tasks = []
    done_tasks = []

    # Skip YAML frontmatter if present
    in_frontmatter = False
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                body_start = i + 1
                break

    for line in lines[body_start:]:
        stripped = line.strip()

        # Section header detection
        if stripped.startswith("## ") or stripped.startswith("# "):
            header = stripped.lstrip("#").strip().lower()
            if header in _SECTION_PRIORITY:
                current_priority = _SECTION_PRIORITY[header]
            continue

        # Task line
        if re.match(r"^\s*-\s*\[", stripped):
            task = _parse_task_line(stripped, current_priority, counter)
            if task:
                counter += 1
                if task["status"] == "done":
                    done_tasks.append(task)
                else:
                    open_tasks.append(task)

    # Write tasks.json
    tasks_data = {"tasks": open_tasks}
    _atomic_write(json_path, tasks_data)

    # Write completed.json alongside (in _kernel/ if kernel path, else same dir)
    parent = os.path.dirname(json_path)
    completed_path = os.path.join(parent, "completed.json")
    if not os.path.exists(completed_path):
        completed_data = {"completed": done_tasks}
        _atomic_write(completed_path, completed_data)
    elif done_tasks:
        # Append to existing completed.json
        existing = _read_json(completed_path, "completed", strict=False)
        if existing is not None:
            existing["completed"].extend(done_tasks)
            _atomic_write(completed_path, existing)

    # Backup the original
    backup_path = md_path + ".v2-backup"
    if not os.path.exists(backup_path):
        os.rename(md_path, backup_path)
    else:
        # Backup already exists (edge case), just remove the md
        os.remove(md_path)

    task_count = len(open_tasks)
    done_count = len(done_tasks)
    print(
        "Migrated {} → {} ({} open, {} done)".format(
            os.path.basename(md_path), os.path.basename(json_path),
            task_count, done_count
        ),
        file=sys.stderr,
    )
    return json_path


def _upgrade_v2_json(json_path):
    """Upgrade a v2-format tasks.json in place.

    v2 format: {"tasks": [{"text": "...", "status": "...", "priority": "normal"}]}
    v3 format: {"tasks": [{"id": "t001", "title": "...", "status": "...", "priority": "todo", ...}]}

    Detection: any task with "text" key and no "id" key is v2.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return  # Let _read_json handle the error

    if not isinstance(data, dict) or "tasks" not in data:
        return

    tasks = data["tasks"]
    if not tasks:
        return

    # Check if upgrade needed: v2 tasks have "text" not "title"
    needs_upgrade = any(
        "text" in t and "id" not in t
        for t in tasks
    )
    if not needs_upgrade:
        return

    # Map v2 priority values to v3
    priority_map = {"normal": "todo", "urgent": "urgent", "active": "active"}

    upgraded = []
    counter = 1
    for t in tasks:
        if "text" in t and "id" not in t:
            v2_priority = t.get("priority", "todo")
            v3_priority = priority_map.get(v2_priority, v2_priority)
            upgraded.append({
                "id": "t{:03d}".format(counter),
                "title": t["text"],
                "status": t.get("status", "todo"),
                "priority": v3_priority,
                "assignee": None,
                "due": None,
                "tags": t.get("tags", []),
                "created": _today(),
                "session": t.get("session", "migrated"),
            })
        else:
            # Already v3 format (mixed file), keep as-is
            upgraded.append(t)
        counter += 1

    data["tasks"] = upgraded
    _atomic_write(json_path, data)

    print(
        "Upgraded {} v2 tasks in {} to v3 format".format(
            len([t for t in tasks if "text" in t and "id" not in t]),
            os.path.basename(json_path)
        ),
        file=sys.stderr,
    )


def _ensure_tasks_json(json_path):
    """Ensure tasks.json exists and is v3 format. Migrate if needed.

    Called before any read/write to a tasks.json path.
    Handles three cases:
      1. tasks.json exists in v3 format — no-op
      2. tasks.json exists in v2 format (text, no id) — upgrade in place
      3. No tasks.json, but tasks.md exists — migrate from markdown
      4. Neither exists — will be created by _read_json on first access
    """
    if os.path.exists(json_path):
        _upgrade_v2_json(json_path)
        return json_path

    # Check for v2 tasks.md at the same location
    parent = os.path.dirname(json_path)
    md_path = os.path.join(parent, "tasks.md")
    if os.path.isfile(md_path):
        return _migrate_tasks_md(md_path, json_path)

    # Neither exists — will be created by _read_json on first access
    return json_path


def _all_task_files(walnut):
    """Return absolute paths of every tasks.json under walnut, recursively.

    Stops at nested walnut boundaries (_kernel/key.md) so a parent walnut
    doesn't scan into child walnuts. Each walnut manages its own tasks.
    """
    results = []
    walnut_abs = os.path.abspath(walnut)
    skip_dirs = {
        ".git", "node_modules", "__pycache__", "dist", "build", ".next", "target",
        # Archive and reference directories contain legacy files that may not
        # conform to the v3 tasks.json schema.  Never scan into them.
        "_archive", "_references", "01_Archive", "raw",
    }
    for root, dirs, files in os.walk(walnut):
        # Skip hidden dirs, known non-content dirs, and anything with "archive" in name
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".")
            and d not in skip_dirs
            and "archive" not in d.lower()
        ]
        # Stop at nested walnut boundaries (but not the root walnut itself)
        if os.path.abspath(root) != walnut_abs:
            kernel_key = os.path.join(root, "_kernel", "key.md")
            if os.path.isfile(kernel_key):
                dirs[:] = []  # don't descend into nested walnut
                continue
        if "tasks.json" in files:
            json_path = os.path.join(root, "tasks.json")
            _ensure_tasks_json(json_path)  # upgrade v2 format if needed
            results.append(json_path)
        elif "tasks.md" in files:
            # Auto-migrate v2 tasks.md → tasks.json on first touch
            md_path = os.path.join(root, "tasks.md")
            json_path = os.path.join(root, "tasks.json")
            _migrate_tasks_md(md_path, json_path)
            results.append(json_path)
    return results


def _find_task(walnut, task_id):
    """Find a task by ID across all tasks.json files.

    Returns (file_path, task_dict, data_dict) or exits with error.
    """
    for tf in _all_task_files(walnut):
        data = _read_json(tf, "tasks", strict=False)
        if data is None:
            continue
        for task in data["tasks"]:
            if task.get("id") == task_id:
                return tf, task, data
    print("Error: task {} not found".format(task_id), file=sys.stderr)
    sys.exit(1)


def _resolve_bundle_path(walnut, bundle):
    """Find a bundle directory by name, checking v3 flat, v2 bundles/, and nested."""
    if not bundle:
        return None
    # v3: flat in walnut root
    candidate = os.path.join(walnut, bundle)
    if os.path.isdir(candidate):
        return candidate
    # v2: inside bundles/ container
    candidate = os.path.join(walnut, "bundles", bundle)
    if os.path.isdir(candidate):
        return candidate
    # v1: inside _core/_capsules/
    candidate = os.path.join(walnut, "_core", "_capsules", bundle)
    if os.path.isdir(candidate):
        return candidate
    # Not found — will be created at v3 location
    return os.path.join(walnut, bundle)


def _tasks_path_for_bundle(walnut, bundle):
    if bundle:
        bundle_dir = _resolve_bundle_path(walnut, bundle)
        json_path = os.path.join(bundle_dir, "tasks.json")
    else:
        json_path = os.path.join(walnut, "_kernel", "tasks.json")
    return _ensure_tasks_json(json_path)


def _collect_all_tasks(walnut):
    """Return every task from every tasks.json under walnut."""
    all_tasks = []
    for tf in _all_task_files(walnut):
        data = _read_json(tf, "tasks", strict=False)
        if data is not None:
            all_tasks.extend(data["tasks"])
    return all_tasks


def _find_all_walnuts(world_root):
    """Find all walnut directories under an ALIVE world root.

    A walnut is any directory containing _kernel/key.md.
    Scans 02_Life/, 04_Ventures/, 05_Experiments/, and 01_Archive/.
    """
    walnuts = []
    for domain in ["01_Archive", "02_Life", "04_Ventures", "05_Experiments"]:
        domain_path = os.path.join(world_root, domain)
        if not os.path.isdir(domain_path):
            continue
        for root, dirs, files in os.walk(domain_path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            kernel_key = os.path.join(root, "_kernel", "key.md")
            if os.path.isfile(kernel_key):
                walnuts.append(root)
                dirs[:] = []  # don't descend into nested walnuts
    return sorted(walnuts)


def _read_manifest_field(manifest_path, field):
    """Read a single field from context.manifest.yaml using regex.

    Handles simple `field: value` and multi-line `field: |` blocks.
    """
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Try multi-line block scalar first (field: | or field: >)
    pattern_block = r'^{field}:\s*[|>]-?\s*\n((?:[ \t]+.+\n?)*)'.format(
        field=re.escape(field)
    )
    m = re.search(pattern_block, content, re.MULTILINE)
    if m:
        lines = m.group(1).split("\n")
        stripped = [line.strip() for line in lines if line.strip()]
        return "\n".join(stripped)

    # Simple single-line
    pattern_simple = r'^{field}:\s*["\']?(.*?)["\']?\s*$'.format(
        field=re.escape(field)
    )
    m = re.search(pattern_simple, content, re.MULTILINE)
    if m:
        return m.group(1)

    return None


def _find_bundles(walnut):
    """Return list of (bundle_name, bundle_abs_path) for all bundles, any version.

    Walks recursively. Finds v3 flat bundles, v2 bundles/ container,
    v1 _core/_capsules/ with companion.md. Skips _kernel/, .git, node_modules.
    """
    bundles = []
    skip_dirs = {"_kernel", "_core", ".git", "node_modules", "raw", "__pycache__"}
    for root, dirs, files in os.walk(walnut):
        # Don't descend into system/hidden dirs
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        # v2/v3: context.manifest.yaml
        if "context.manifest.yaml" in files:
            name = os.path.basename(root)
            bundles.append((name, root))
        # v1: companion.md (legacy capsule)
        elif "companion.md" in files:
            name = os.path.basename(root)
            bundles.append((name, root))
    return bundles


def _last_squirrel(bundle_path):
    """Find the most recent squirrel file in a bundle's _squirrels/ dir."""
    sq_dir = os.path.join(bundle_path, "_squirrels")
    if not os.path.isdir(sq_dir):
        return None
    squirrels = []
    for f in os.listdir(sq_dir):
        fp = os.path.join(sq_dir, f)
        if os.path.isfile(fp):
            squirrels.append((os.path.getmtime(fp), f))
    if not squirrels:
        return None
    squirrels.sort(reverse=True)
    mtime, name = squirrels[0]
    return {
        "squirrel": name,
        "date": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"),
    }


def _dir_last_touched(bundle_path):
    """Return ISO date of the most recently modified file in a bundle dir."""
    bundle_dir = bundle_path
    latest = 0.0
    for root, _dirs, files in os.walk(bundle_dir):
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
# Subcommands
# ---------------------------------------------------------------------------

def cmd_add(args):
    walnut = args.walnut
    if not os.path.isdir(walnut):
        print("Error: invalid walnut path: {}".format(walnut), file=sys.stderr)
        sys.exit(1)

    target = _tasks_path_for_bundle(walnut, args.bundle)

    # Collect all tasks across walnut for ID generation (including completed)
    all_tasks = _collect_all_tasks(walnut)
    completed_path = os.path.join(walnut, "_kernel", "completed.json")
    completed_data = _read_json(completed_path, "completed")
    all_for_id = all_tasks + completed_data["completed"]
    data = _read_json(target, "tasks")

    new_id = _next_id(all_for_id)
    session = args.session or os.environ.get("CLAUDE_SESSION_ID", "manual")

    task = {
        "id": new_id,
        "title": args.title,
        "status": "active" if args.priority == "active" else "todo",
        "priority": args.priority,
        "assignee": args.assignee,
        "due": args.due,
        "tags": [t.strip() for t in args.tags.split(",")] if args.tags else [],
        "created": _today(),
        "session": session,
    }
    if args.bundle:
        task["bundle"] = args.bundle

    data["tasks"].append(task)
    _atomic_write(target, data)
    print(json.dumps(task, indent=2))


def cmd_done(args):
    walnut = args.walnut
    if not os.path.isdir(walnut):
        print("Error: invalid walnut path: {}".format(walnut), file=sys.stderr)
        sys.exit(1)

    tf, task, data = _find_task(walnut, args.id)

    # Remove from source
    data["tasks"] = [t for t in data["tasks"] if t.get("id") != args.id]
    _atomic_write(tf, data)

    # Add to completed.json
    completed_path = os.path.join(walnut, "_kernel", "completed.json")
    completed_data = _read_json(completed_path, "completed")

    task["status"] = "done"
    task["completed"] = _today()
    task["completed_by"] = args.by or getpass.getuser()

    completed_data["completed"].append(task)
    _atomic_write(completed_path, completed_data)

    print("Task {} marked done.".format(args.id))


def cmd_drop(args):
    walnut = args.walnut
    if not os.path.isdir(walnut):
        print("Error: invalid walnut path: {}".format(walnut), file=sys.stderr)
        sys.exit(1)

    tf, task, data = _find_task(walnut, args.id)

    # Remove from source
    data["tasks"] = [t for t in data["tasks"] if t.get("id") != args.id]
    _atomic_write(tf, data)

    # Add to completed.json
    completed_path = os.path.join(walnut, "_kernel", "completed.json")
    completed_data = _read_json(completed_path, "completed")

    task["status"] = "dropped"
    task["completed"] = _today()
    if args.reason:
        task["reason"] = args.reason

    completed_data["completed"].append(task)
    _atomic_write(completed_path, completed_data)

    print("Task {} dropped.".format(args.id))


def cmd_edit(args):
    walnut = args.walnut
    if not os.path.isdir(walnut):
        print("Error: invalid walnut path: {}".format(walnut), file=sys.stderr)
        sys.exit(1)

    tf, task, data = _find_task(walnut, args.id)

    # Apply field updates
    if args.title is not None:
        task["title"] = args.title
    if args.priority is not None:
        task["priority"] = args.priority
    if args.status is not None:
        task["status"] = args.status
    if args.assignee is not None:
        task["assignee"] = args.assignee
    if args.due is not None:
        task["due"] = args.due
    if args.tags is not None:
        task["tags"] = [t.strip() for t in args.tags.split(",")]

    new_bundle = args.bundle

    if new_bundle is not None:
        new_target = _tasks_path_for_bundle(walnut, new_bundle if new_bundle else None)
        if new_target != tf:
            # Remove from old file
            data["tasks"] = [t for t in data["tasks"] if t.get("id") != args.id]
            _atomic_write(tf, data)
            # Add to new file
            new_data = _read_json(new_target, "tasks")
            task["bundle"] = new_bundle if new_bundle else None
            new_data["tasks"].append(task)
            _atomic_write(new_target, new_data)
            print(json.dumps(task, indent=2))
            return

    # Write back in place
    for i, t in enumerate(data["tasks"]):
        if t.get("id") == args.id:
            data["tasks"][i] = task
            break
    _atomic_write(tf, data)
    print(json.dumps(task, indent=2))


def cmd_list(args):
    world = getattr(args, "world", None)
    walnut = getattr(args, "walnut", None)
    search = getattr(args, "search", None)

    if not world and not walnut:
        print("Error: either --walnut or --world is required", file=sys.stderr)
        sys.exit(1)

    # Determine which walnuts to scan
    if world:
        if not os.path.isdir(os.path.join(world, ".alive")):
            print("Error: {} does not appear to be an ALIVE world".format(world), file=sys.stderr)
            sys.exit(1)
        walnut_paths = _find_all_walnuts(world)
    else:
        if not os.path.isdir(walnut):
            print("Error: invalid walnut path: {}".format(walnut), file=sys.stderr)
            sys.exit(1)
        walnut_paths = [walnut]

    # Collect tasks across all target walnuts
    all_tasks = []
    for wp in walnut_paths:
        tasks = _collect_all_tasks(wp)
        if world:
            # Add walnut attribution for cross-walnut results
            walnut_name = os.path.relpath(wp, world)
            for t in tasks:
                t["walnut"] = walnut_name
        all_tasks.extend(tasks)

    # Apply filters
    filtered = []
    for task in all_tasks:
        # Default: exclude done and dropped unless explicitly filtered
        if args.status:
            if task.get("status") != args.status:
                continue
        else:
            if task.get("status") in ("done", "dropped"):
                continue

        if args.bundle and task.get("bundle") != args.bundle:
            continue
        if args.priority and task.get("priority") != args.priority:
            continue
        if args.assignee and task.get("assignee") != args.assignee:
            continue
        if args.tag and args.tag not in task.get("tags", []):
            continue

        # Text search filter
        if search:
            title = task.get("title", "")
            if search.lower() not in title.lower():
                continue

        filtered.append(task)

    print(json.dumps(filtered, indent=2))


def cmd_summary(args):
    walnut = args.walnut
    if not os.path.isdir(walnut):
        print("Error: invalid walnut path: {}".format(walnut), file=sys.stderr)
        sys.exit(1)

    include_items = args.include_items
    thirty_days_ago = datetime.now() - timedelta(days=30)

    # Collect tasks grouped by bundle
    bundle_tasks = {}  # key: bundle name or None for _kernel

    for tf in _all_task_files(walnut):
        data = _read_json(tf, "tasks")
        # Determine bundle from directory
        parent = os.path.basename(os.path.dirname(tf))
        bundle_name = None if parent == "_kernel" else parent
        if bundle_name not in bundle_tasks:
            bundle_tasks[bundle_name] = []
        bundle_tasks[bundle_name].extend(data["tasks"])

    # Also load completed tasks for counts
    completed_path = os.path.join(walnut, "_kernel", "completed.json")
    completed_data = _read_json(completed_path, "completed")
    completed_by_bundle = {}
    for ct in completed_data["completed"]:
        b = ct.get("bundle")
        if b not in completed_by_bundle:
            completed_by_bundle[b] = []
        completed_by_bundle[b].append(ct)

    # All known bundles (from manifest files) — returns (name, abs_path)
    known_bundles = _find_bundles(walnut)

    # Build output
    active_tier = {}
    recent_tier = {}
    status_counts = {"done": 0, "draft": 0, "prototype": 0, "published": 0}

    for bundle_name, bundle_path in known_bundles:
        manifest_path = os.path.join(bundle_path, "context.manifest.yaml")
        # Also check for v1 companion.md
        if not os.path.exists(manifest_path):
            manifest_path = os.path.join(bundle_path, "companion.md")
        goal = _read_manifest_field(manifest_path, "goal") or ""
        status = _read_manifest_field(manifest_path, "status") or "draft"
        context = _read_manifest_field(manifest_path, "context") or ""

        tasks = bundle_tasks.get(bundle_name, [])
        c_tasks = completed_by_bundle.get(bundle_name, [])

        # Counts
        counts = {"urgent": 0, "active": 0, "todo": 0, "blocked": 0, "done": 0}
        urgent_titles = []
        active_titles = []
        assignees = set()

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

        # Count done from completed
        done_count = 0
        for ct in c_tasks:
            if ct.get("status") == "done":
                done_count += 1
        counts["done"] = done_count

        # Track all bundle statuses for summary totals
        if status in status_counts:
            status_counts[status] += 1

        # Determine tier
        has_urgent = any(t.get("priority") == "urgent" for t in tasks)
        has_active = any(t.get("status") == "active" for t in tasks)

        if has_urgent or has_active:
            entry = {
                "status": status,
                "goal": goal,
                "context": context,
                "tasks": {
                    "counts": counts,
                },
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
                entry = {
                    "status": status,
                    "goal": goal,
                    "counts": counts,
                    "last_touched": last_touched_str,
                }
                recent_tier[bundle_name] = entry

    # Summary counts include ALL bundles regardless of tier
    summary_counts = dict(status_counts)
    summary_counts["total"] = len(known_bundles)

    # Unscoped tasks (_kernel tasks with no bundle)
    unscoped_tasks = bundle_tasks.get(None, [])
    unscoped = {
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

    output = {
        "bundles": {
            "active": active_tier,
            "recent": recent_tier,
            "summary": summary_counts,
        },
        "unscoped": unscoped,
    }

    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ALIVE Context System task manager"
    )
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add")
    p_add.add_argument("--walnut", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--bundle", default=None)
    p_add.add_argument("--priority", default="todo",
                        choices=["urgent", "active", "todo"])
    p_add.add_argument("--assignee", default=None)
    p_add.add_argument("--due", default=None)
    p_add.add_argument("--tags", default=None)
    p_add.add_argument("--session", default=None)

    # done
    p_done = sub.add_parser("done")
    p_done.add_argument("--walnut", required=True)
    p_done.add_argument("--id", required=True)
    p_done.add_argument("--by", default=None)

    # drop
    p_drop = sub.add_parser("drop")
    p_drop.add_argument("--walnut", required=True)
    p_drop.add_argument("--id", required=True)
    p_drop.add_argument("--reason", default=None)

    # edit
    p_edit = sub.add_parser("edit")
    p_edit.add_argument("--walnut", required=True)
    p_edit.add_argument("--id", required=True)
    p_edit.add_argument("--title", default=None)
    p_edit.add_argument("--priority", default=None,
                        choices=["urgent", "active", "todo"])
    p_edit.add_argument("--status", default=None,
                        choices=["todo", "active", "blocked", "done", "dropped"])
    p_edit.add_argument("--assignee", default=None)
    p_edit.add_argument("--due", default=None)
    p_edit.add_argument("--tags", default=None)
    p_edit.add_argument("--bundle", default=None)

    # list
    p_list = sub.add_parser("list")
    p_list.add_argument("--walnut", default=None, help="Single walnut path to list tasks from")
    p_list.add_argument("--world", default=None, help="World root — list tasks across all walnuts")
    p_list.add_argument("--search", default=None, help="Case-insensitive substring match on task title")
    p_list.add_argument("--bundle", default=None)
    p_list.add_argument("--priority", default=None)
    p_list.add_argument("--assignee", default=None)
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--tag", default=None)

    # summary
    p_summary = sub.add_parser("summary")
    p_summary.add_argument("--walnut", required=True)
    p_summary.add_argument("--include-items", action="store_true", default=False)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "add": cmd_add,
        "done": cmd_done,
        "drop": cmd_drop,
        "edit": cmd_edit,
        "list": cmd_list,
        "summary": cmd_summary,
    }

    dispatch[args.command](args)


if __name__ == "__main__":
    main()
