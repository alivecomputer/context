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


def _read_json(path, key):
    """Read a JSON file. Create with {key: []} if missing."""
    if not os.path.exists(path):
        return {key: []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or key not in data:
            print("Error: malformed {}".format(path), file=sys.stderr)
            sys.exit(1)
        return data
    except json.JSONDecodeError:
        print("Error: malformed JSON in {}".format(path), file=sys.stderr)
        sys.exit(1)


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


def _all_task_files(walnut):
    """Return absolute paths of every tasks.json under walnut, recursively.

    Stops at nested walnut boundaries (_kernel/key.md) so a parent walnut
    doesn't scan into child walnuts. Each walnut manages its own tasks.
    """
    results = []
    walnut_abs = os.path.abspath(walnut)
    skip_dirs = {".git", "node_modules", "__pycache__", "dist", "build", ".next", "target"}
    for root, dirs, files in os.walk(walnut):
        # Skip hidden dirs and known non-content dirs
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip_dirs]
        # Stop at nested walnut boundaries (but not the root walnut itself)
        if os.path.abspath(root) != walnut_abs:
            kernel_key = os.path.join(root, "_kernel", "key.md")
            if os.path.isfile(kernel_key):
                dirs[:] = []  # don't descend into nested walnut
                continue
        if "tasks.json" in files:
            results.append(os.path.join(root, "tasks.json"))
        if "tasks.md" in files and "tasks.json" not in files:
            print(
                "Warning: {}/tasks.md found (v2 format). "
                "Run system-upgrade to migrate.".format(root),
                file=sys.stderr,
            )
    return results


def _find_task(walnut, task_id):
    """Find a task by ID across all tasks.json files.

    Returns (file_path, task_dict, data_dict) or exits with error.
    """
    for tf in _all_task_files(walnut):
        data = _read_json(tf, "tasks")
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
        return os.path.join(bundle_dir, "tasks.json")
    return os.path.join(walnut, "_kernel", "tasks.json")


def _collect_all_tasks(walnut):
    """Return every task from every tasks.json under walnut."""
    all_tasks = []
    for tf in _all_task_files(walnut):
        data = _read_json(tf, "tasks")
        all_tasks.extend(data["tasks"])
    return all_tasks


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
    walnut = args.walnut
    if not os.path.isdir(walnut):
        print("Error: invalid walnut path: {}".format(walnut), file=sys.stderr)
        sys.exit(1)

    all_tasks = _collect_all_tasks(walnut)

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
    p_list.add_argument("--walnut", required=True)
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
