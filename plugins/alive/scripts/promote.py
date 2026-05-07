#!/usr/bin/env python3
"""``alive tasks promote`` -- bulk-promote confirmed task-shaped stash items.

Mirrors the :mod:`log` CLI shape: ``SCHEMA_METADATA`` constant exported
at the top + stashed on the subparser via
``set_defaults(_schema_metadata=...)``; argparse subparser registered in
:mod:`cli`; pure-JSON stdout (no logging passthrough); exit codes
0/1/2/3/4/5 per the shared CLI convention.

Two-phase contract per stash item (codex round-2 fix). For each item
where ``type == "task"``:

  1. **Resolve scope** -- ``bundle: <name>`` -> ``<bundle>/tasks.json``;
     missing/null -> ``_kernel/tasks.json`` (epic Decision 3 -- unscoped
     is a first-class outcome, NOT a ``BUNDLE_REQUIRED`` failure).
  2. **Filter cross-walnut** -- if ``routed != <active walnut name>``
     return ``SKIPPED_CROSS_WALNUT`` (no marker, no task created).
  3. **Branch on ``promotion_state``:**
     * ``complete`` -> ``ALREADY_PROMOTED`` with the existing ``task_id``
     * ``pending`` with ``task_id`` set -> resume (re-attempt phase 2 if
       no committed task exists yet, else skip to phase 3 marker
       finalization)
     * missing/null -> fresh promotion, run all three phases below
  4. **Phase 1 (allocate + mark pending):** under flock, scan tasks.json
     (target file from step 1) for max id, allocate next ``t<N+1>``
     UNIONED with pending squirrel reservations world-wide for THIS
     walnut, write ``promotion_state: pending`` + ``task_id: t<N+1>``
     onto the squirrel stash item via atomic_write, fsync.
  5. **Phase 2 (write task):** under same outer flock, call
     ``tasks.add_unlocked(guard, walnut, title, bundle, ...,
     task_id=<pre-allocated-id>)`` in-process. The locking variant
     ``tasks.add()`` would deadlock here by re-acquiring the same
     lockfile (``fcntl.flock`` is per-fd and NOT re-entrant).
  6. **Phase 3 (finalize marker):** under same flock, set
     ``promotion_state: complete`` on the squirrel stash item via
     atomic_write.

Walnut-filtered world-wide pending sweep runs BEFORE the current-session
items on every invocation (epic Decision 2 -- closes the cross-session
retry hole). Scans ``<world_root>/.alive/_squirrels/*.yaml`` filtered by
the session-level ``walnut:`` field; recovered items appear with
``status: RECOVERED_PENDING`` and a ``source_squirrel: <session-id>``
field naming where the marker lived.

Lock & lockfile path
--------------------
A single ``flock_file(<walnut>/_kernel/.tasks.lock)`` outer context
wraps the entire run (sweep + current-session items). The lockfile
lives at ``_kernel/.tasks.lock`` per epic Decision 1 (codex round-5
Critical 1) -- NOT under ``.alive/locks/`` because walnut-local
``.alive/`` is a single-walnut-world sentinel and creating one would
confuse :func:`_world_root_for_promote`. The same lockfile is taken by
:func:`tasks.add` so promote serializes cleanly against direct
``tasks.py add`` invocations.

Exit codes:
    0  success (SUCCEEDED or PARTIAL with at least one PROMOTED_*)
    1  general / malformed yaml / IO failure
    2  usage error
    3  walnut path or world root not found
    4  permission failure
    5  lock acquisition timed out (``_kernel/.tasks.lock``)

YAML strategy
-------------
This module reads + writes the squirrel YAML via a narrow regex-based
parser/writer (the project's existing convention -- see
``project.py:_extract_yaml_field``; PyYAML is NOT a runtime dependency
of the plugin and adding one would expand the marketplace install
surface). The narrow surface: the save skill emits stash items with
plain inline scalars under fixed keys (``content``, ``type``, ``routed``,
``bundle``, ``promotion_state``, ``task_id``); we add/update only
``promotion_state`` and ``task_id`` and never touch the user-authored
fields. Block scalars (``|``/``>``) on the touched keys are rejected
with ``yaml_unsupported`` -- the save skill never emits them, but the
fail-loud check guards against hand-edited squirrel files corrupting
the rewrite.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import stat as _stat_mod
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _common import (  # noqa: E402
    FlockTimeoutError,
    LockGuard,
    WrongLockError,
    _read_json,
    atomic_write_text,
    flock_file,
)
import tasks as _tasks_module  # noqa: E402


# ---------------------------------------------------------------------------
# Schema metadata (consumed by ``alive schema``)
# ---------------------------------------------------------------------------

SCHEMA_METADATA = {
    "description": (
        "Bulk-promote confirmed task-shaped stash items from a squirrel "
        "YAML into the active walnut's tasks.json (bundle-scoped or "
        "walnut-level unscoped) under a single narrow flock transaction "
        "with a two-phase pending|complete marker for crash safety. "
        "Also runs a walnut-filtered world-wide sweep that resumes any "
        "leftover ``promotion_state: pending`` markers from prior "
        "sessions before processing the current --squirrel argument."
    ),
    "stdout_shape": {
        "status": (
            "str -- SUCCEEDED | PARTIAL | FAILED. SUCCEEDED if every "
            "item is in {PROMOTED_*, ALREADY_PROMOTED, "
            "SKIPPED_CROSS_WALNUT}; FAILED if every item is ERROR; "
            "PARTIAL otherwise."
        ),
        "items": (
            "list[object] -- per-item record with stash_index, status, "
            "task_id?, bundle?, scope?, source_squirrel?, error?."
        ),
        "dry_run": "bool -- only present when --dry-run is passed",
    },
    "exit_codes": {
        "0": "success (status SUCCEEDED or PARTIAL with at least one promotion)",
        "1": "general / malformed yaml / IO failure",
        "2": "usage error",
        "3": "walnut path or world root not found",
        "4": "permission failure",
        "5": "lock acquisition timed out on _kernel/.tasks.lock",
    },
    "examples": [
        {
            "input": (
                "alive tasks promote --walnut /path/to/walnut "
                "--squirrel 46e2efcf-... "
            ),
            "output_excerpt": (
                '{"status": "SUCCEEDED", "items": [{"stash_index": 0, '
                '"status": "PROMOTED_BUNDLE", ...}]}'
            ),
        },
        {
            "input": (
                "alive tasks promote --walnut /path/to/walnut  "
                "# no --squirrel: recovery sweep only"
            ),
            "output_excerpt": (
                '{"status": "SUCCEEDED", "items": [{"status": '
                '"RECOVERED_PENDING", "source_squirrel": "...", ...}]}'
            ),
        },
    ],
}


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------

ERROR_USAGE = "usage"
ERROR_WALNUT_NOT_FOUND = "walnut_not_found"
ERROR_WORLD_ROOT = "world_root_error"
ERROR_PERMISSION = "permission"
ERROR_LOCK_TIMEOUT = "lock_timeout"
ERROR_YAML_UNSUPPORTED = "yaml_unsupported"
ERROR_SQUIRREL_NOT_FOUND = "squirrel_not_found"
ERROR_INTERNAL = "internal_error"


class _PromoteError(Exception):
    """Short-circuit into a JSON error envelope; mirrors :class:`log._LogError`."""

    def __init__(self, message, code, exit_code=1, detail=None, extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.detail = detail
        self.extra = extra


def _emit_error(exc):
    """Print the JSON error envelope for *exc* to stdout."""
    error_obj = {"code": exc.code, "message": exc.message}
    if exc.detail is not None:
        error_obj["detail"] = exc.detail
    if exc.extra:
        for k, v in exc.extra.items():
            error_obj[k] = v
    print(json.dumps(
        {"success": False, "error": error_obj},
        indent=2, sort_keys=True,
    ))


# ---------------------------------------------------------------------------
# Squirrel YAML reader + narrow rewriter
# ---------------------------------------------------------------------------

#: Per-stash-item line format: a top-level ``- key: value`` head line
#: that opens a new dict in the ``stash:`` list.
_STASH_LIST_HEAD_RE = re.compile(
    r'^(\s*)-\s+([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$'
)

#: Match the top-level ``stash:`` key (must be at column 0 to avoid
#: false-positives inside a nested ``content:`` value).
_STASH_KEY_RE = re.compile(r'^stash\s*:\s*(.*)$', re.MULTILINE)

#: Match a top-level ``key: value`` at the SESSION root (column 0). Used
#: to scope where the ``stash:`` block ends -- we scan forward from the
#: ``stash:`` head until the next column-0 key OR end of file.
_TOP_LEVEL_KEY_RE = re.compile(
    r'^[A-Za-z_][A-Za-z0-9_-]*\s*:'
)

#: A ``key: value`` line at any indent inside a stash item. Captures
#: leading-whitespace count so the parser tracks indent.
_ITEM_KEY_RE = re.compile(
    r'^(\s*)([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$'
)

#: YAML block-scalar header (``|`` / ``>`` with optional indicator).
_BLOCK_SCALAR_RE = re.compile(r'^[|>]')


def _strip_value(raw):
    """Same narrow strip as ``tasks._strip_yaml_value`` (re-implemented to
    avoid promote.py importing private tasks helpers)."""
    return _tasks_module._strip_yaml_value(raw)


def _read_squirrel_yaml(path):
    """Read the squirrel YAML and return ``(text, items)``.

    ``text`` is the raw file content (kept so we can rewrite slices in
    place via :func:`_replace_item_field`). ``items`` is a list of dicts
    each containing the parsed stash-item fields plus byte offsets the
    rewriter needs:

        {
            "index": <int -- 0-based position in the stash list>,
            "fields": {key: value, ...},
            "head_line": <int -- 0-based line index of the ``- key: ...`` head>,
            "end_line": <int -- exclusive end-line of this item>,
            "child_indent": <int -- indent depth of child keys>,
            "head_indent": <int -- indent depth of the ``- `` marker>,
        }

    Items outside the ``stash:`` list are not returned.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError as exc:
        raise _PromoteError(
            "squirrel YAML not found: {}".format(path),
            code=ERROR_SQUIRREL_NOT_FOUND,
            exit_code=3,
        ) from exc
    except PermissionError as exc:
        raise _PromoteError(
            "permission denied reading squirrel YAML {}: {}".format(path, exc),
            code=ERROR_PERMISSION,
            exit_code=4,
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise _PromoteError(
            "failed to read squirrel YAML {}: {}".format(path, exc),
            code=ERROR_YAML_UNSUPPORTED,
            exit_code=1,
        ) from exc

    items = _parse_stash_items(text, path)
    return text, items


def _parse_stash_items(text, path):
    """Parse stash items from *text* and return the offsets list."""
    lines = text.split("\n")
    # Locate the ``stash:`` line (column-0).
    stash_line_idx = None
    for i, line in enumerate(lines):
        if line.startswith("stash:") or re.match(r"^stash\s*:", line):
            stash_line_idx = i
            break
    if stash_line_idx is None:
        return []
    # Inline form ``stash: []`` -- empty list, nothing to parse.
    rest = lines[stash_line_idx][len("stash:"):].strip()
    if rest and rest != "":
        # Inline value; only ``[]`` is meaningful for our contract.
        return []

    # Walk forward from stash_line_idx + 1 collecting list items until a
    # column-0 sibling key OR EOF.
    items = []
    i = stash_line_idx + 1
    item_index = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip(" ")
        # Stop at next column-0 key (sibling of ``stash:``).
        if line and not line[0].isspace() and _TOP_LEVEL_KEY_RE.match(line):
            break
        # Skip blank lines + comments.
        if stripped == "" or stripped.startswith("#"):
            i += 1
            continue
        # List item head: ``  - key: value``
        head_match = _STASH_LIST_HEAD_RE.match(line)
        if head_match is None:
            i += 1
            continue
        head_indent = len(head_match.group(1))
        first_key = head_match.group(2)
        first_val = _strip_value(head_match.group(3))
        # Walk forward to find this item's body lines: every line
        # indented MORE than the head (the head occupies head_indent
        # columns plus the ``-`` and a space).
        item_end = i + 1
        # Child indent: head_indent + 2 (two spaces under ``-``) is the
        # idiomatic emit, but be lenient -- accept any indent strictly
        # greater than head_indent.
        fields = {first_key: first_val}
        # Detect block scalars on touched keys (fail-loud).
        if first_key in ("promotion_state", "task_id") and first_val and _BLOCK_SCALAR_RE.match(first_val):
            raise _PromoteError(
                "squirrel YAML {} stash item {} key {!r} uses an "
                "unsupported block scalar; only plain inline scalars "
                "are supported on the touched keys".format(
                    path, item_index, first_key
                ),
                code=ERROR_YAML_UNSUPPORTED,
                exit_code=1,
            )
        child_indent = None
        while item_end < len(lines):
            nxt = lines[item_end]
            if nxt.strip() == "" or nxt.lstrip(" ").startswith("#"):
                item_end += 1
                continue
            # Stop at next column-0 OR same-indent ``- `` (next list item).
            cur_indent = len(nxt) - len(nxt.lstrip(" "))
            if not nxt[0].isspace() and _TOP_LEVEL_KEY_RE.match(nxt):
                break
            if cur_indent <= head_indent:
                break
            # Child key.
            km = _ITEM_KEY_RE.match(nxt)
            if km is not None:
                k_indent = len(km.group(1))
                k_name = km.group(2)
                k_val = _strip_value(km.group(3))
                if child_indent is None:
                    child_indent = k_indent
                if k_indent == child_indent:
                    if k_name in ("promotion_state", "task_id") and k_val and _BLOCK_SCALAR_RE.match(k_val):
                        raise _PromoteError(
                            "squirrel YAML {} stash item {} key {!r} "
                            "uses an unsupported block scalar".format(
                                path, item_index, k_name
                            ),
                            code=ERROR_YAML_UNSUPPORTED,
                            exit_code=1,
                        )
                    fields[k_name] = k_val
            item_end += 1
        if child_indent is None:
            child_indent = head_indent + 2
        items.append({
            "index": item_index,
            "fields": fields,
            "head_line": i,
            "end_line": item_end,
            "child_indent": child_indent,
            "head_indent": head_indent,
        })
        item_index += 1
        i = item_end
    return items


def _format_yaml_value(value):
    """Render *value* as a plain YAML inline scalar.

    Quotes only when necessary (string contains characters that would
    confuse the parser: leading whitespace, ``:``, ``#``, ``[``, ``{``,
    ``,``, ``&``, ``*``, ``!``, ``|``, ``>``, single/double quote, ``%``,
    ``@``, backslash, ``\\n``, ``\\r``, ``\\t``). Numbers / bools pass
    through via str(). The narrow set we write (``pending``/``complete``
    for promotion_state and ``t<NNN>`` for task_id) never needs
    quoting; the quote branch exists for defense.
    """
    s = str(value)
    if s == "":
        return '""'
    needs_quote = (
        s != s.strip()
        or s[0] in "[{|>!@&*'\"%#-?,"
        or any(c in s for c in [":", "#", "\n", "\r", "\t"])
        or s.lower() in ("null", "true", "false", "yes", "no", "~")
    )
    if not needs_quote:
        return s
    inner = (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\t", "\\t")
    )
    return '"' + inner + '"'


def _rewrite_item_with_fields(text, item, updates):
    """Return *text* with *item*'s fields updated per *updates* dict.

    Updates are applied as set-or-append: existing keys are rewritten
    in-place (preserving the line's original column position); missing
    keys are appended at the end of the item body using the item's
    ``child_indent`` (defaults to ``head_indent + 2``).

    Other lines in the item body are preserved byte-for-byte. Comments
    and blank lines stay where they are.
    """
    lines = text.split("\n")
    head_line = item["head_line"]
    end_line = item["end_line"]
    head_indent = item["head_indent"]
    child_indent = item["child_indent"]

    # First pass: rewrite existing keys.
    seen = {k: False for k in updates}
    new_lines = list(lines[:head_line])
    # Process the head line first -- it carries the FIRST key on the
    # ``- key: value`` line.
    head_match = _STASH_LIST_HEAD_RE.match(lines[head_line])
    if head_match is None:
        # Should not happen; defensive.
        return text
    first_key = head_match.group(2)
    if first_key in updates:
        new_head = "{}- {}: {}".format(
            " " * head_indent, first_key,
            _format_yaml_value(updates[first_key]),
        )
        new_lines.append(new_head)
        seen[first_key] = True
    else:
        new_lines.append(lines[head_line])

    # Track where to insert appended keys: right before the FIRST
    # blank/comment line at the tail of the item body, or at end_line
    # if no such tail exists.
    body_start = head_line + 1
    body_end = end_line  # exclusive
    # Find tail boundary: walk back from body_end - 1, skipping blanks/
    # comments (so appended keys live BEFORE the tail blank that
    # separates this item from the next).
    insert_at_body_offset = body_end  # exclusive index relative to lines
    j = body_end - 1
    while j >= body_start:
        l = lines[j]
        if l.strip() == "" or l.lstrip(" ").startswith("#"):
            insert_at_body_offset = j
            j -= 1
            continue
        break

    for li in range(body_start, body_end):
        line = lines[li]
        km = _ITEM_KEY_RE.match(line)
        if km is None:
            new_lines.append(line)
            continue
        k_name = km.group(2)
        k_indent = len(km.group(1))
        if k_name in updates and k_indent == child_indent and not seen[k_name]:
            new_lines.append(
                "{}{}: {}".format(
                    " " * child_indent, k_name,
                    _format_yaml_value(updates[k_name]),
                )
            )
            seen[k_name] = True
        else:
            new_lines.append(line)

    # Compute current insertion length so trailing blanks stay attached
    # to the tail. ``new_lines`` currently has body_end - 0 entries
    # mirroring lines[:body_end].
    appended = []
    for k, v in updates.items():
        if not seen[k]:
            appended.append(
                "{}{}: {}".format(
                    " " * child_indent, k,
                    _format_yaml_value(v),
                )
            )

    # Splice appended lines BEFORE the tail blanks. ``new_lines`` length
    # equals body_end. We want to insert ``appended`` at position
    # insert_at_body_offset (which is an index in the ORIGINAL ``lines``
    # array AND, by construction, in ``new_lines``).
    if appended:
        new_lines = (
            new_lines[:insert_at_body_offset]
            + appended
            + new_lines[insert_at_body_offset:]
        )

    # Append the rest of the file unchanged.
    new_lines.extend(lines[end_line:])
    return "\n".join(new_lines)


def _atomic_rewrite_squirrel(path, text):
    """Atomic write helper -- thin wrapper for clearer exception mapping."""
    try:
        atomic_write_text(path, text)
    except PermissionError as exc:
        raise _PromoteError(
            "permission denied writing squirrel YAML {}: {}".format(path, exc),
            code=ERROR_PERMISSION,
            exit_code=4,
        ) from exc
    except OSError as exc:
        raise _PromoteError(
            "failed to write squirrel YAML {}: {}".format(path, exc),
            code=ERROR_YAML_UNSUPPORTED,
            exit_code=1,
        ) from exc


# ---------------------------------------------------------------------------
# Helpers: walnut + bundle resolution + tasks.json id allocation
# ---------------------------------------------------------------------------

def _resolve_target_tasks_path(walnut, bundle):
    """Return the absolute path of the target tasks.json for *bundle*.

    Mirrors :func:`tasks._tasks_path_for_bundle` but without the
    ``_ensure_tasks_json`` migration side-effect (we only need the path
    to compute the high-water-mark id; ``add_unlocked`` runs the
    migration on its own).
    """
    if bundle:
        bundle_dir = _tasks_module._resolve_bundle_path(walnut, bundle)
        return os.path.join(bundle_dir, "tasks.json")
    return os.path.join(walnut, "_kernel", "tasks.json")


def _allocate_task_id(walnut, bundle, world_root, walnut_name):
    """Compute the next ``t<N+1>`` id for the resolved scope.

    Unions max-of-committed-tasks ACROSS THE WHOLE WALNUT (so an item
    promoted to a bundle never reuses an id already used in
    ``_kernel/tasks.json``) with world-wide pending squirrel
    reservations for *walnut_name* (epic Decision 2).
    """
    all_tasks = _tasks_module._collect_all_tasks(walnut)
    completed_path = os.path.join(walnut, "_kernel", "completed.json")
    completed_data = _read_json(completed_path, "completed")
    all_for_id = all_tasks + completed_data["completed"]
    reserved = _tasks_module._pending_reservations_for_walnut(
        world_root, walnut_name
    )
    return _tasks_module._next_id(all_for_id, reserved_ids=reserved)


def _task_already_committed(walnut, bundle, task_id):
    """True if *task_id* already appears in any tasks.json under *walnut*.

    Used by phase 2 to detect "promote crashed AFTER tasks.json write
    but BEFORE marker finalization" -- in which case we skip phase 2
    and proceed straight to phase 3.
    """
    target = _resolve_target_tasks_path(walnut, bundle)
    try:
        data = _read_json(target, "tasks", strict=False)
    except SystemExit:
        return False
    if data is None:
        return False
    for t in data.get("tasks", []):
        if t.get("id") == task_id:
            return True
    return False


# ---------------------------------------------------------------------------
# Walnut-filtered world-wide pending sweep
# ---------------------------------------------------------------------------

def _list_squirrel_files(world_root):
    """Return absolute paths of every ``*.yaml`` under ``<world_root>/.alive/_squirrels/``."""
    sq_dir = os.path.join(world_root, ".alive", "_squirrels")
    if not os.path.isdir(sq_dir):
        return []
    out = []
    try:
        for fname in sorted(os.listdir(sq_dir)):
            if not fname.endswith(".yaml"):
                continue
            fpath = os.path.join(sq_dir, fname)
            if os.path.isfile(fpath):
                out.append(fpath)
    except OSError:
        return []
    return out


def _session_id_from_squirrel(text, fallback_path):
    """Extract ``session_id:`` from the squirrel YAML, falling back to filename."""
    m = re.search(
        r'^session_id\s*:\s*(.+?)\s*$',
        text, re.MULTILINE,
    )
    if m is not None:
        val = _strip_value(m.group(1))
        if val and val != "null":
            return val
    return os.path.splitext(os.path.basename(fallback_path))[0]


def _session_walnut_from_squirrel(text):
    """Extract ``walnut:`` (session-level) from the squirrel YAML."""
    m = re.search(
        r'^walnut\s*:\s*(.+?)\s*$',
        text, re.MULTILINE,
    )
    if m is None:
        return None
    val = _strip_value(m.group(1))
    if val == "null":
        return None
    return val


# ---------------------------------------------------------------------------
# Per-item processing
# ---------------------------------------------------------------------------

def _build_item_record(stash_index, status, task_id=None, bundle=None,
                       scope=None, source_squirrel=None, error=None):
    """Standard per-item JSON record (sorted-key friendly)."""
    return {
        "stash_index": stash_index,
        "status": status,
        "task_id": task_id,
        "bundle": bundle,
        "scope": scope,
        "source_squirrel": source_squirrel,
        "error": error,
    }


def _process_item(
    guard,
    walnut,
    walnut_name,
    world_root,
    squirrel_path,
    text,
    item,
    source_squirrel,
    dry_run,
):
    """Run the two-phase promotion contract for a single stash item.

    Returns ``(record, new_text)``. ``new_text`` is the (possibly
    rewritten) squirrel YAML body; the caller is responsible for
    persisting it via :func:`_atomic_rewrite_squirrel` AFTER each phase
    so a crash mid-loop leaves a coherent on-disk state.
    """
    fields = item["fields"]
    item_type = fields.get("type")
    routed = fields.get("routed")
    bundle = fields.get("bundle") or None
    title = fields.get("content") or ""
    promotion_state = fields.get("promotion_state")
    existing_task_id = fields.get("task_id")
    scope = "bundle" if bundle else "walnut"

    # Type filter FIRST -- only ``type: task`` items are promotable, and
    # the locked contract is "iterate ALL task-shaped items so the
    # resume-detection branch sees and reports `complete` items as
    # ALREADY_PROMOTED" (codex round-11 fix). Non-task items must pass
    # through SILENTLY -- emitting any record (e.g. SKIPPED_CROSS_WALNUT
    # when a non-task item happens to be routed elsewhere) would surface
    # noise to the agent and contradict the per-item status enum, which
    # only describes outcomes for task-shaped items.
    if item_type != "task":
        return (None, text)

    # Step 2: cross-walnut filter (skip without marker / task creation).
    if routed and routed != walnut_name:
        return (
            _build_item_record(
                item["index"], "SKIPPED_CROSS_WALNUT",
                bundle=bundle, scope=scope,
                source_squirrel=source_squirrel,
            ),
            text,
        )

    # Step 3a: already-promoted short-circuit.
    if promotion_state == "complete":
        return (
            _build_item_record(
                item["index"], "ALREADY_PROMOTED",
                task_id=existing_task_id, bundle=bundle, scope=scope,
                source_squirrel=source_squirrel,
            ),
            text,
        )

    # Step 3b: pending resume.
    if promotion_state == "pending" and existing_task_id:
        # Check if tasks.json already has this id.
        if _task_already_committed(walnut, bundle, existing_task_id):
            # Phase 2 already done; only phase 3 (finalize marker) left.
            if not dry_run:
                new_text = _rewrite_item_with_fields(
                    text, item, {"promotion_state": "complete"},
                )
                _atomic_rewrite_squirrel(squirrel_path, new_text)
            else:
                new_text = text
            return (
                _build_item_record(
                    item["index"], "RECOVERED_PENDING",
                    task_id=existing_task_id,
                    bundle=bundle, scope=scope,
                    source_squirrel=source_squirrel,
                ),
                new_text,
            )
        # No commit yet -- run phase 2 with the pre-allocated id.
        if not dry_run:
            try:
                _tasks_module.add_unlocked(
                    guard, walnut, title=title, bundle=bundle,
                    task_id=existing_task_id,
                )
            except WrongLockError:
                # Re-raise -- this is a programmer error, not a per-item
                # failure; promote should never call add_unlocked with a
                # wrong guard.
                raise
            except Exception as exc:  # noqa: BLE001 -- per-item error reporting
                return (
                    _build_item_record(
                        item["index"], "ERROR",
                        task_id=existing_task_id,
                        bundle=bundle, scope=scope,
                        source_squirrel=source_squirrel,
                        error="phase2: {}: {}".format(
                            type(exc).__name__, exc
                        ),
                    ),
                    text,
                )
            # Phase 3.
            new_text = _rewrite_item_with_fields(
                text, item, {"promotion_state": "complete"},
            )
            _atomic_rewrite_squirrel(squirrel_path, new_text)
        else:
            new_text = text
        return (
            _build_item_record(
                item["index"], "RECOVERED_PENDING",
                task_id=existing_task_id,
                bundle=bundle, scope=scope,
                source_squirrel=source_squirrel,
            ),
            new_text,
        )

    # Step 4: fresh promotion. Allocate id under flock.
    new_id = _allocate_task_id(walnut, bundle, world_root, walnut_name)
    promoted_status = (
        "PROMOTED_BUNDLE" if bundle else "PROMOTED_UNSCOPED"
    )
    if dry_run:
        return (
            _build_item_record(
                item["index"], promoted_status,
                task_id=new_id, bundle=bundle, scope=scope,
                source_squirrel=source_squirrel,
            ),
            text,
        )
    # Phase 1: write pending marker + reserved id.
    try:
        new_text = _rewrite_item_with_fields(
            text, item,
            {"promotion_state": "pending", "task_id": new_id},
        )
        _atomic_rewrite_squirrel(squirrel_path, new_text)
    except _PromoteError:
        raise
    except Exception as exc:  # noqa: BLE001
        return (
            _build_item_record(
                item["index"], "ERROR",
                bundle=bundle, scope=scope,
                source_squirrel=source_squirrel,
                error="phase1: {}: {}".format(type(exc).__name__, exc),
            ),
            text,
        )
    # Re-parse so subsequent items see correct offsets, but for the
    # current item we already have the updated fields. The caller
    # processes one item per re-parse cycle (see :func:`_process_squirrel`).
    text = new_text
    # Phase 2: write task.
    try:
        _tasks_module.add_unlocked(
            guard, walnut, title=title, bundle=bundle, task_id=new_id,
        )
    except WrongLockError:
        raise
    except Exception as exc:  # noqa: BLE001
        return (
            _build_item_record(
                item["index"], "ERROR",
                task_id=new_id, bundle=bundle, scope=scope,
                source_squirrel=source_squirrel,
                error="phase2: {}: {}".format(type(exc).__name__, exc),
            ),
            text,
        )
    # Phase 3: finalize marker. Re-locate the item by re-parsing so
    # the rewriter has fresh offsets reflecting phase-1 line shifts.
    items_after_phase1 = _parse_stash_items(text, squirrel_path)
    target_item = None
    for it in items_after_phase1:
        if it["index"] == item["index"]:
            target_item = it
            break
    if target_item is None:
        # Shouldn't happen -- phase 1 inserted, not deleted, fields.
        return (
            _build_item_record(
                item["index"], "ERROR",
                task_id=new_id, bundle=bundle, scope=scope,
                source_squirrel=source_squirrel,
                error="phase3: re-parse lost item index {}".format(
                    item["index"]
                ),
            ),
            text,
        )
    new_text = _rewrite_item_with_fields(
        text, target_item, {"promotion_state": "complete"},
    )
    _atomic_rewrite_squirrel(squirrel_path, new_text)
    return (
        _build_item_record(
            item["index"], promoted_status,
            task_id=new_id, bundle=bundle, scope=scope,
            source_squirrel=source_squirrel,
        ),
        new_text,
    )


def _process_squirrel(
    guard, walnut, walnut_name, world_root, squirrel_path,
    source_squirrel_label, dry_run, recovery_only,
):
    """Process every promotable stash item in *squirrel_path*.

    Returns a list of per-item records.

    *recovery_only* gates which items are picked up:
      * True -- only items with ``promotion_state == "pending"`` are
        processed (the world-wide sweep path).
      * False -- every ``type: task`` item is iterated; the per-item
        branch handles ``complete`` / ``pending`` / fresh distinctly.
    """
    text, items = _read_squirrel_yaml(squirrel_path)
    records = []
    for item in items:
        if recovery_only:
            if item["fields"].get("promotion_state") != "pending":
                continue
        # Re-read each iteration so byte offsets stay valid as we
        # mutate the file (phase 1 + phase 3 each rewrite). For
        # determinism + simplicity we re-parse from disk between items.
        text, items_after = _read_squirrel_yaml(squirrel_path)
        # Find the corresponding item by index in the fresh parse.
        fresh_item = None
        for it in items_after:
            if it["index"] == item["index"]:
                fresh_item = it
                break
        if fresh_item is None:
            continue
        record, text = _process_item(
            guard, walnut, walnut_name, world_root,
            squirrel_path, text, fresh_item,
            source_squirrel_label, dry_run,
        )
        if record is not None:
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Top-level handler
# ---------------------------------------------------------------------------

def _resolve_squirrel_path(world_root, squirrel_id):
    """Resolve a ``--squirrel <session-id>`` argument to a YAML path.

    Looks for ``<world_root>/.alive/_squirrels/<session-id>.yaml``.
    Returns the absolute path or raises :class:`_PromoteError` (exit 3)
    if missing.
    """
    candidate = os.path.join(
        world_root, ".alive", "_squirrels", squirrel_id + ".yaml"
    )
    if not os.path.isfile(candidate):
        raise _PromoteError(
            "squirrel YAML for session {!r} not found at {}".format(
                squirrel_id, candidate
            ),
            code=ERROR_SQUIRREL_NOT_FOUND,
            exit_code=3,
        )
    return candidate


def handle(args):
    """Execute ``alive tasks promote``; return exit code.

    Pure-JSON-stdout contract (mirrors :func:`log.handle`): every error
    path lands on stdout as a JSON envelope.
    """
    try:
        return _handle_inner(args)
    except _PromoteError as exc:
        _emit_error(exc)
        return exc.exit_code
    except FlockTimeoutError as exc:
        _emit_error(_PromoteError(
            str(exc),
            code=ERROR_LOCK_TIMEOUT,
            exit_code=5,
        ))
        return 5
    except Exception as exc:  # noqa: BLE001 -- pure-JSON-stdout contract
        _emit_error(_PromoteError(
            "internal error: {}: {}".format(type(exc).__name__, exc),
            code=ERROR_INTERNAL,
            exit_code=1,
        ))
        return 1


def _handle_inner(args):
    walnut_abs = os.path.abspath(os.path.expanduser(args.walnut))
    try:
        st = os.stat(walnut_abs)
    except FileNotFoundError as exc:
        raise _PromoteError(
            "walnut path does not exist: {}".format(walnut_abs),
            code=ERROR_WALNUT_NOT_FOUND,
            exit_code=3,
        ) from exc
    except PermissionError as exc:
        raise _PromoteError(
            "permission denied stat'ing walnut {}: {}".format(walnut_abs, exc),
            code=ERROR_PERMISSION,
            exit_code=4,
        ) from exc
    except OSError as exc:
        raise _PromoteError(
            "failed to stat walnut {}: {}".format(walnut_abs, exc),
            code=ERROR_INTERNAL,
            exit_code=1,
        ) from exc
    if not _stat_mod.S_ISDIR(st.st_mode):
        raise _PromoteError(
            "walnut path is not a directory: {}".format(walnut_abs),
            code=ERROR_WALNUT_NOT_FOUND,
            exit_code=3,
        )

    walnut_name = os.path.basename(walnut_abs)

    # World-root discovery via the strict ``.alive/_squirrels/`` sentinel
    # (epic Decision 2 -- single-walnut worlds carry the bare ``.alive/``
    # marker without the subdirectory; we MUST require the subdirectory
    # so the recovery sweep has a directory to iterate).
    try:
        world_root = _tasks_module._world_root_for_promote(walnut_abs)
    except FileNotFoundError as exc:
        raise _PromoteError(
            str(exc),
            code=ERROR_WORLD_ROOT,
            exit_code=3,
        ) from exc

    dry_run = bool(getattr(args, "dry_run", False))
    squirrel_id = getattr(args, "squirrel", None)

    items_records = []
    lock_path = _tasks_module._tasks_lock_path(walnut_abs)

    with flock_file(lock_path) as guard:
        # ---------------------------------------------------------------
        # Phase A: walnut-filtered world-wide pending sweep. Runs FIRST
        # on every invocation -- closes the cross-session retry hole
        # (epic Decision 2).
        # ---------------------------------------------------------------
        sweep_paths = _list_squirrel_files(world_root)
        # Skip the current --squirrel target during sweep (it'll be
        # processed in phase B with full task-iteration semantics, not
        # just pending-recovery).
        current_squirrel_path = None
        if squirrel_id:
            try:
                current_squirrel_path = _resolve_squirrel_path(
                    world_root, squirrel_id
                )
            except _PromoteError:
                # Allow sweep to run even if the named squirrel is
                # missing -- but re-raise after sweep so the caller
                # sees the not-found error. Capture for re-raise.
                current_squirrel_path = "__missing__"
        for spath in sweep_paths:
            if (
                current_squirrel_path
                and current_squirrel_path != "__missing__"
                and os.path.abspath(spath) == os.path.abspath(current_squirrel_path)
            ):
                continue
            try:
                text_only, _items_only = _read_squirrel_yaml(spath)
            except _PromoteError:
                # Malformed squirrel YAML -- record nothing, continue.
                continue
            sess_walnut = _session_walnut_from_squirrel(text_only)
            if sess_walnut != walnut_name:
                continue  # cross-walnut isolation
            sess_label = _session_id_from_squirrel(text_only, spath)
            recs = _process_squirrel(
                guard, walnut_abs, walnut_name, world_root,
                spath, sess_label, dry_run,
                recovery_only=True,
            )
            items_records.extend(recs)

        # ---------------------------------------------------------------
        # Phase B: process the current --squirrel target (if provided).
        # Re-raise the not-found error captured during phase A so the
        # caller still sees a clean exit 3 when --squirrel was bogus.
        # ---------------------------------------------------------------
        if current_squirrel_path == "__missing__":
            raise _PromoteError(
                "squirrel YAML for session {!r} not found at "
                "{}/.alive/_squirrels/{}.yaml".format(
                    squirrel_id, world_root, squirrel_id
                ),
                code=ERROR_SQUIRREL_NOT_FOUND,
                exit_code=3,
            )
        if current_squirrel_path:
            # The session-level ``walnut:`` filter applies ONLY to the
            # world-wide sweep (Phase A); the explicitly named current
            # squirrel must always be processed -- per-item cross-walnut
            # handling lives in the per-item ``routed != walnut_name``
            # branch (returns SKIPPED_CROSS_WALNUT). Skipping a named
            # --squirrel based on its session-level walnut field would
            # silently drop work the caller explicitly asked us to look
            # at and contradict the locked contract.
            text_only, _items_only = _read_squirrel_yaml(current_squirrel_path)
            sess_label = _session_id_from_squirrel(
                text_only, current_squirrel_path
            )
            recs = _process_squirrel(
                guard, walnut_abs, walnut_name, world_root,
                current_squirrel_path, sess_label, dry_run,
                recovery_only=False,
            )
            items_records.extend(recs)

    # ---------------------------------------------------------------
    # Top-level status aggregation.
    # ---------------------------------------------------------------
    if not items_records:
        top_status = "SUCCEEDED"
    else:
        statuses = {r["status"] for r in items_records}
        # Spec contract (locked):
        #   SUCCEEDED iff every status is in the success set
        #     {PROMOTED_BUNDLE, PROMOTED_UNSCOPED, ALREADY_PROMOTED,
        #      SKIPPED_CROSS_WALNUT}
        #   FAILED iff every status is ERROR
        #   PARTIAL otherwise.
        # RECOVERED_PENDING is intentionally NOT in the success set --
        # the spec's per-item enum lists it separately, signaling that a
        # caller saw at least one repair this run (worth surfacing to
        # the agent as "look at recovery telemetry" rather than letting
        # it land in the silent SUCCEEDED bucket).
        success_set = {
            "PROMOTED_BUNDLE", "PROMOTED_UNSCOPED",
            "ALREADY_PROMOTED", "SKIPPED_CROSS_WALNUT",
        }
        if statuses == {"ERROR"}:
            top_status = "FAILED"
        elif statuses.issubset(success_set):
            top_status = "SUCCEEDED"
        else:
            top_status = "PARTIAL"

    payload = {
        "status": top_status,
        "items": items_records,
    }
    if dry_run:
        payload["dry_run"] = True
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if top_status != "FAILED" else 1


# ---------------------------------------------------------------------------
# Argparse registration
# ---------------------------------------------------------------------------

def _json_error_handler(parser):
    """Argparse error replacement that emits a JSON envelope on stdout."""

    def _error(message):
        payload = {
            "success": False,
            "error": {
                "code": ERROR_USAGE,
                "message": "usage: {}".format(message),
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        sys.exit(2)

    return _error


def _tasks_group_missing_subcommand(args):
    """Handler for bare ``alive tasks`` (no sub-sub-command)."""
    payload = {
        "success": False,
        "error": {
            "code": ERROR_USAGE,
            "message": "missing tasks subcommand; try `alive tasks promote --help`",
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2


def register(subparsers):
    """Register the ``tasks`` group and its ``promote`` subcommand."""
    parser = subparsers.add_parser(
        "tasks",
        help="Task-management subcommands (promote, ...).",
        description="Task-management subcommands.",
    )
    tasks_subparsers = parser.add_subparsers(dest="tasks_command")
    tasks_subparsers.required = False

    promote = tasks_subparsers.add_parser(
        "promote",
        help=SCHEMA_METADATA["description"],
        description=SCHEMA_METADATA["description"],
    )
    promote.add_argument(
        "--plugin-root",
        default=argparse.SUPPRESS,
        help=(
            "Override the ALIVE plugin root directory "
            "(defaults: $ALIVE_PLUGIN_ROOT, then auto-discovery)."
        ),
    )
    promote.add_argument(
        "--walnut",
        required=True,
        help="Path to the active walnut directory.",
    )
    promote.add_argument(
        "--squirrel",
        default=None,
        help=(
            "Squirrel session id to process. Optional -- omit to run "
            "ONLY the walnut-filtered world-wide recovery sweep "
            "(no current-session items)."
        ),
    )
    promote.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Plan the promotion (returns the JSON shape that would "
            "result) without writing tasks.json or marker mutations."
        ),
    )
    promote.error = _json_error_handler(promote)  # type: ignore[assignment]
    parser.error = _json_error_handler(parser)  # type: ignore[assignment]

    from schema import SCHEMA_METADATA_DEFAULT_KEY  # noqa: E402
    promote.set_defaults(
        _handler=handle,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )
    parser.set_defaults(_handler=_tasks_group_missing_subcommand)
    return parser


# ---------------------------------------------------------------------------
# Direct-invocation support
# ---------------------------------------------------------------------------

def _standalone_main(argv=None):
    parser = argparse.ArgumentParser(prog="alive-tasks")
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)
    args = parser.parse_args(["tasks"] + (list(argv) if argv else []))
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(_standalone_main(sys.argv[1:]))
