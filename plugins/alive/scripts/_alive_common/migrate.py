"""LD6/LD7 v2 -> v3 layout migration helper.

Extracted verbatim from ``alive-p2p.py`` (T2 of fn-18). Public surface:

- ``migrate_v2_layout(staging_dir, *, now_iso=None, session_id=None)``

The helper reshapes a freshly-extracted v2 staging directory into v3
shape in place; it never touches the target walnut.

resolve_session_id callsite preservation
----------------------------------------

``alive-p2p.py`` historically defined a LOCAL ``resolve_session_id()``
that returns ``os.environ.get("ALIVE_SESSION_ID", "manual")``. That is
DIFFERENT from ``_common.resolve_session_id`` (which synthesises an
anonymous ID). The migration flow's session-id default behavior is the
``"manual"`` semantics, NOT the anonymous-ID one.

This module preserves that semantics: it ships its own ``now_utc_iso``
and ``resolve_session_id`` that match the alive-p2p originals byte-for-
byte. The ``alive-p2p.py`` shim wraps the implementation and forwards
the ALIVE-P2P module-level ``now_utc_iso()`` / ``resolve_session_id()``
return values via the optional ``now_iso=`` / ``session_id=`` kwargs --
so any test that monkey-patches ``alive_p2p.now_utc_iso`` /
``alive_p2p.resolve_session_id`` continues to work without modification.
Tests targeting the new module path can monkey-patch
``_alive_common.migrate.now_utc_iso`` / ``resolve_session_id`` directly,
or pass values via the kwargs.

Stdlib-only.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Mockable environment helpers (LD9)
# ---------------------------------------------------------------------------
#
# Local copies preserved verbatim from ``alive-p2p.py`` -- the alive-p2p
# semantics (``"manual"`` default for session-id) is the contract this
# extraction protects. ``_common.resolve_session_id`` synthesises an
# anonymous ID and would change the migration's default behavior; do
# not silently swap it in.

def now_utc_iso():
    # type: () -> str
    """Return the current UTC time as an ISO 8601 string.

    Wrapped in a function so tests can monkeypatch a fixed timestamp without
    touching ``datetime`` globally. Format matches the stub constants.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def resolve_session_id():
    # type: () -> str
    """Return the current ALIVE session id, or ``"manual"`` for CLI runs."""
    return os.environ.get("ALIVE_SESSION_ID", "manual")


_V2_TASKS_MD_LINE = re.compile(r"^- \[([ ~x])\]\s+(.+?)(?:\s+@(\S+))?\s*$")


def _parse_v2_tasks_md(content, bundle_name, iso_timestamp, session_id):
    # type: (str, str, str, str) -> List[Dict[str, Any]]
    """Parse a v2 ``tasks.md`` markdown checklist into v3 task dicts.

    Accepts any mix of ``- [ ]`` / ``- [~]`` / ``- [x]`` lines with optional
    trailing ``@session`` attribution. Ignores headings, blank lines, frontmatter,
    and any line that does not match the checkbox pattern. IDs are assigned
    sequentially as ``t-001``, ``t-002``, ... scoped to the parsed bundle --
    these are fresh IDs because v2 markdown tasks carry no structured identity.

    Parameters:
        content: raw ``tasks.md`` text
        bundle_name: the bundle leaf name (stored as the task's ``bundle`` field)
        iso_timestamp: migration timestamp (stored as ``created``)
        session_id: session id for attribution (used when the line has no ``@``)

    Returns a list of task dicts shaped for ``{bundle}/tasks.json``::

        [{"id": "t-001", "title": "...", "status": "active|done",
          "priority": "normal|high", "assignee": None, "due": None,
          "tags": [], "created": iso_timestamp, "session": session_id,
          "bundle": bundle_name}, ...]
    """
    tasks = []  # type: List[Dict[str, Any]]
    seq = 0

    # Strip optional YAML frontmatter so ``- [ ]`` bullets inside don't parse.
    lines = content.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break

    for raw in lines:
        m = _V2_TASKS_MD_LINE.match(raw)
        if not m:
            continue
        mark, title, session_attrib = m.group(1), m.group(2), m.group(3)
        title = title.strip()
        if not title:
            continue

        if mark == " ":
            status = "active"
            priority = "normal"
        elif mark == "~":
            status = "active"
            priority = "high"
        else:  # mark == "x"
            status = "done"
            priority = "normal"

        seq += 1
        task = {
            "id": "t-{0:03d}".format(seq),
            "title": title,
            "status": status,
            "priority": priority,
            "assignee": None,
            "due": None,
            "tags": [],
            "created": iso_timestamp,
            "session": session_attrib or session_id,
            "bundle": bundle_name,
        }
        tasks.append(task)

    return tasks


def _write_tasks_json(path, tasks):
    # type: (str, List[Dict[str, Any]]) -> None
    """Write a ``{"tasks": [...]}`` dict to ``path`` via atomic replace."""
    dir_path = os.path.dirname(path)
    if dir_path and not os.path.isdir(dir_path):
        os.makedirs(dir_path, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def migrate_v2_layout(staging_dir, now_iso=None, session_id=None):
    # type: (str, Optional[str], Optional[str]) -> Dict[str, Any]
    """Transform a v2 package staging directory into v3 shape in place.

    Applied to a staging dir that has ALREADY been extracted from the tar and
    validated. Does NOT touch the target walnut -- operates on staging only.

    The receive pipeline (task .9) calls this when layout inference (LD7)
    reports ``source_layout == "v2"``. Idempotent: running twice on the same
    staging dir is a no-op second time.

    Transforms (in order):
        1. Drop ``_kernel/_generated/`` entirely if present.
        2. Flatten ``bundles/{name}/`` -> ``{name}/`` at staging root, with
           ``-imported`` suffix on collision with an existing live-context
           dir of the same name.
        3. Convert each migrated bundle's ``tasks.md`` -> ``tasks.json`` via
           ``_parse_v2_tasks_md`` + ``_write_tasks_json``. Delete the original
           ``tasks.md`` after successful conversion.

    Parameters:
        staging_dir: absolute path to the extracted staging tree
        now_iso: optional ISO timestamp override. Defaults to
            ``now_utc_iso()`` resolved at call time, matching the alive-p2p
            local semantics. The ``alive-p2p.py`` shim forwards
            ``alive_p2p.now_utc_iso()`` here so existing test
            monkeypatches on ``alive_p2p.now_utc_iso`` continue to work.
        session_id: optional session-id override. Defaults to
            ``resolve_session_id()`` resolved at call time. Same shim
            forwarding rationale as ``now_iso``.

    Returns a dict with keys:
        actions: List[str]          -- human-readable transform log, in order
        warnings: List[str]         -- non-fatal issues (e.g. tasks.md +
                                        tasks.json both present -> kept json)
        bundles_migrated: List[str] -- final leaf names of flattened bundles
                                        (with any ``-imported`` suffix applied)
        tasks_converted: int        -- total count of task entries written
                                        across every migrated bundle's tasks.json
        errors: List[str]           -- non-fatal errors captured per-bundle
                                        (e.g. unreadable tasks.md); the
                                        migration continues across the rest
    """
    staging_dir = os.path.abspath(staging_dir)
    result = {
        "actions": [],
        "warnings": [],
        "bundles_migrated": [],
        "tasks_converted": 0,
        "errors": [],
    }  # type: Dict[str, Any]

    if not os.path.isdir(staging_dir):
        result["errors"].append(
            "staging dir does not exist: {0}".format(staging_dir)
        )
        return result

    generated_dir = os.path.join(staging_dir, "_kernel", "_generated")
    bundles_container = os.path.join(staging_dir, "bundles")

    has_generated = os.path.isdir(generated_dir)
    if os.path.isdir(bundles_container):
        has_bundles = any(
            os.path.isdir(os.path.join(bundles_container, name))
            for name in os.listdir(bundles_container)
        )
    else:
        has_bundles = False

    # Idempotency short-circuit: already v3 shape.
    if not has_generated and not has_bundles:
        result["actions"].append("no-op (already v3 layout)")
        return result

    # --- Step 1: drop _kernel/_generated/ --------------------------------
    if has_generated:
        shutil.rmtree(generated_dir)
        result["actions"].append("Dropped _kernel/_generated/")

    # --- Step 2: flatten bundles/{name}/ -> {name}/ -----------------------
    flattened = []  # type: List[Tuple[str, str]]  # (final_name, bundle_dir)
    if os.path.isdir(bundles_container):
        # Sort for deterministic behaviour across filesystems.
        child_names = sorted(os.listdir(bundles_container))
        for name in child_names:
            src = os.path.join(bundles_container, name)
            if not os.path.isdir(src):
                # Stray files inside bundles/ are a protocol oddity; warn
                # and leave them where they are (they'll be dropped when we
                # rmtree the empty container below, so preserve instead).
                result["warnings"].append(
                    "non-directory entry in bundles/: {0}".format(name)
                )
                continue

            final_name = name
            dst = os.path.join(staging_dir, final_name)
            if os.path.exists(dst):
                final_name = "{0}-imported".format(name)
                dst = os.path.join(staging_dir, final_name)
                # Guard against a second-order collision (extremely rare:
                # both ``name`` and ``name-imported`` already exist).
                if os.path.exists(dst):
                    result["errors"].append(
                        "cannot flatten bundles/{0}: both {0} and "
                        "{0}-imported already exist at staging root".format(
                            name
                        )
                    )
                    continue

            shutil.move(src, dst)
            flattened.append((final_name, dst))
            if final_name == name:
                result["actions"].append(
                    "Flattened bundles/{0} -> {0}".format(name)
                )
            else:
                result["actions"].append(
                    "Flattened bundles/{0} -> {1} (collision suffix)".format(
                        name, final_name
                    )
                )

        # Remove empty bundles/ container.
        try:
            remaining = os.listdir(bundles_container)
        except OSError:
            remaining = []
        if not remaining:
            try:
                os.rmdir(bundles_container)
            except OSError as exc:
                result["warnings"].append(
                    "could not remove empty bundles/ dir: {0}".format(exc)
                )
        else:
            result["warnings"].append(
                "bundles/ container not empty after flatten; "
                "{0} entries remain".format(len(remaining))
            )

    result["bundles_migrated"] = [name for name, _ in flattened]

    # --- Step 3: convert {bundle}/tasks.md -> tasks.json ------------------
    iso_timestamp = now_iso if now_iso is not None else now_utc_iso()
    sid = session_id if session_id is not None else resolve_session_id()

    for final_name, bundle_dir in flattened:
        tasks_md = os.path.join(bundle_dir, "tasks.md")
        tasks_json = os.path.join(bundle_dir, "tasks.json")

        if not os.path.isfile(tasks_md):
            continue  # bundle had no markdown tasks; nothing to convert

        if os.path.isfile(tasks_json):
            # Both present -- prefer the existing JSON, warn, leave tasks.md
            # in place for the human to reconcile post-import.
            result["warnings"].append(
                "bundle '{0}' has both tasks.md and tasks.json; "
                "kept tasks.json, left tasks.md untouched".format(final_name)
            )
            continue

        try:
            with open(tasks_md, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            result["errors"].append(
                "failed to read {0}/tasks.md: {1}".format(final_name, exc)
            )
            continue

        parsed = _parse_v2_tasks_md(
            content, final_name, iso_timestamp, sid
        )

        try:
            _write_tasks_json(tasks_json, parsed)
        except OSError as exc:
            result["errors"].append(
                "failed to write {0}/tasks.json: {1}".format(final_name, exc)
            )
            continue

        try:
            os.remove(tasks_md)
        except OSError as exc:
            result["warnings"].append(
                "converted {0}/tasks.md but could not remove original: "
                "{1}".format(final_name, exc)
            )

        result["tasks_converted"] += len(parsed)
        result["actions"].append(
            "Converted {0}/tasks.md -> tasks.json ({1} tasks)".format(
                final_name, len(parsed)
            )
        )

    return result


__all__ = (
    "migrate_v2_layout",
    "now_utc_iso",
    "resolve_session_id",
)
