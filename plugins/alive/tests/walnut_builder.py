#!/usr/bin/env python3
"""Synthetic walnut fixture builder for the v3 P2P test suite (LD13 / .11).

Builds programmatic walnut trees on disk so round-trip tests do not depend on
checked-in tar fixtures (which rot, produce unreadable diffs, and lock the
test suite to a specific layout). Two layouts are supported:

- ``v3`` (default) -- ``_kernel/`` flat, JSON tasks, bundles flat at walnut
  root. Mirrors the production layout that ``alive-p2p.py create_package``
  emits.
- ``v2`` -- legacy ``bundles/`` container, per-bundle ``tasks.md`` markdown,
  ``_kernel/_generated/now.json`` projection. Used by the v2->v3 migration
  test matrix (task .12) so the same builder serves both halves.

Stdlib only. No external test framework. Returns absolute paths.

Usage::

    from plugins.alive.tests.walnut_builder import build_walnut

    walnut = build_walnut(
        tmp_path,
        name="test-walnut",
        layout="v3",
        bundles=[
            {"name": "shielding-review", "goal": "Review shielding",
             "status": "active",
             "files": {"draft-01.md": "# draft"}},
        ],
        tasks={"unscoped": [{"id": "t1", "title": "do thing"}]},
        live_files=[{"path": "engineering/spec.md", "content": "# spec"}],
        log_entries=[{"timestamp": "2026-04-01T00:00:00Z",
                      "session_id": "test", "body": "Initial."}],
    )
"""

import json
import os
from typing import Any, Dict, List, Optional, Union


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_text(path, content):
    # type: (str, str) -> None
    """Write UTF-8 text, creating parent dirs as needed."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _yaml_scalar(value):
    # type: (Any) -> str
    """Quote a value as a YAML scalar (single-line, double-quoted)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # Escape backslashes and double quotes for the double-quoted form.
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"{0}"'.format(escaped)


# ---------------------------------------------------------------------------
# Templates -- minimal but valid v3 walnut content
# ---------------------------------------------------------------------------

_DEFAULT_KEY_TEMPLATE = (
    "---\n"
    "type: venture\n"
    "name: {name}\n"
    "goal: \"{goal}\"\n"
    "created: {created}\n"
    "rhythm: weekly\n"
    "tags: []\n"
    "links: []\n"
    "---\n"
    "\n"
    "# {name}\n"
)

_DEFAULT_LOG_TEMPLATE = (
    "---\n"
    "walnut: {name}\n"
    "created: {created}\n"
    "last-entry: {last_entry}\n"
    "entry-count: {entry_count}\n"
    "summary: Initial walnut.\n"
    "---\n"
    "\n"
)

_DEFAULT_INSIGHTS_TEMPLATE = (
    "---\n"
    "walnut: {name}\n"
    "---\n"
    "\n"
    "## Standing knowledge\n"
    "\n"
    "Real insight content for round-trip equality.\n"
)

_DEFAULT_NOW_JSON = {
    "phase": "active",
    "updated": "2026-04-01T00:00:00Z",
    "bundle": None,
    "next": "TBD",
    "squirrel": "test-session",
    "context": "Test walnut.",
}


def _render_log_md(name, walnut_created, log_entries):
    # type: (str, str, Optional[List[Dict[str, str]]]) -> str
    """Render a v3-shaped log.md with optional entries.

    Each entry dict supports ``timestamp``, ``session_id``, ``body``. Newest
    first per the prepend-only convention.
    """
    if not log_entries:
        return _DEFAULT_LOG_TEMPLATE.format(
            name=name,
            created=walnut_created,
            last_entry=walnut_created,
            entry_count=0,
        )
    sorted_entries = list(log_entries)  # caller-provided order preserved
    last_entry = sorted_entries[0].get("timestamp", walnut_created)
    head = (
        "---\n"
        "walnut: {name}\n"
        "created: {created}\n"
        "last-entry: {last_entry}\n"
        "entry-count: {count}\n"
        "summary: Test walnut with {count} entries.\n"
        "---\n"
        "\n"
    ).format(
        name=name,
        created=walnut_created,
        last_entry=last_entry,
        count=len(sorted_entries),
    )
    body_chunks = []
    for entry in sorted_entries:
        ts = entry.get("timestamp", walnut_created)
        sid = entry.get("session_id", "test-session")
        body = entry.get("body", "Test entry.")
        body_chunks.append(
            "## {ts} - squirrel:{sid}\n\n{body}\n\nsigned: squirrel:{sid}\n\n".format(
                ts=ts, sid=sid, body=body,
            )
        )
    return head + "".join(body_chunks)


def _render_bundle_manifest(layout, name, goal, status, extra=None):
    # type: (str, str, str, str, Optional[Dict[str, Any]]) -> str
    """Build a context.manifest.yaml string.

    Both v2 and v3 use the same minimal schema for fixture purposes; tests
    that exercise schema differences can pass an explicit ``extra`` dict.
    """
    lines = [
        "goal: {0}".format(_yaml_scalar(goal)),
        "status: {0}".format(_yaml_scalar(status)),
    ]
    if extra:
        for key, value in extra.items():
            lines.append("{0}: {1}".format(key, _yaml_scalar(value)))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_walnut(
    tmp_path,                  # type: str
    name="test-walnut",        # type: str
    layout="v3",               # type: str
    bundles=None,              # type: Optional[List[Dict[str, Any]]]
    tasks=None,                # type: Optional[Dict[str, List[Dict[str, Any]]]]
    live_files=None,           # type: Optional[List[Dict[str, str]]]
    walnut_created="2026-01-01",  # type: str
    log_entries=None,          # type: Optional[List[Dict[str, str]]]
    sub_walnuts=None,          # type: Optional[List[Dict[str, Any]]]
    goal="Test walnut goal",   # type: str
    include_now_json=True,     # type: bool
):
    # type: (...) -> str
    """Build a synthetic walnut on disk and return its absolute path.

    Parameters:
        tmp_path: parent directory under which the walnut is created.
        name: walnut directory name (becomes basename of the returned path).
        layout: ``"v3"`` (default) or ``"v2"``.
        bundles: list of bundle specs. Each entry is a dict::

            {
                "name": "shielding-review",         # leaf name (required)
                "goal": "...",                       # default "Bundle goal"
                "status": "active",                  # default "active"
                "files": {"draft-01.md": "body"},   # extra files inside the bundle
                "manifest_extra": {"key": "value"}, # extra manifest YAML keys
                "raw_files": {"a.txt": "..."},      # files placed under raw/
            }

        tasks: dict mapping ``"unscoped"`` and bundle names to lists of task
            dicts. v3 layout writes ``_kernel/tasks.json`` (unscoped tasks)
            plus per-bundle dispatch is currently appended to the same file
            with a ``bundle`` field. v2 layout writes per-bundle ``tasks.md``
            files inside ``bundles/{name}/``.
        live_files: list of ``{"path": "rel/path", "content": "..."}``
            entries that get copied to the walnut root as live context.
        walnut_created: ISO date or datetime used in key.md / log.md
            frontmatter.
        log_entries: optional list of log entry dicts (see _render_log_md).
            When omitted, log.md ships an empty body (frontmatter only).
        sub_walnuts: optional list of sub-walnut specs (each is a dict
            forwarded to ``build_walnut`` recursively, with the parent path
            as ``tmp_path``). Used to test the scan boundary.
        goal: short goal string for ``_kernel/key.md``.
        include_now_json: when True (v2 only), write a dummy
            ``_kernel/_generated/now.json``. v3 walnuts never get a stored
            ``now.json`` -- it is a generated projection.

    Returns:
        Absolute path to the created walnut directory.
    """
    if layout not in ("v2", "v3"):
        raise ValueError("layout must be 'v2' or 'v3', got {0!r}".format(layout))

    walnut_root = os.path.abspath(os.path.join(tmp_path, name))
    os.makedirs(walnut_root, exist_ok=True)

    # ---- _kernel/ -----------------------------------------------------------
    kernel = os.path.join(walnut_root, "_kernel")
    os.makedirs(kernel, exist_ok=True)

    _write_text(
        os.path.join(kernel, "key.md"),
        _DEFAULT_KEY_TEMPLATE.format(
            name=name, goal=goal, created=walnut_created,
        ),
    )

    _write_text(
        os.path.join(kernel, "log.md"),
        _render_log_md(name, walnut_created, log_entries),
    )

    _write_text(
        os.path.join(kernel, "insights.md"),
        _DEFAULT_INSIGHTS_TEMPLATE.format(name=name),
    )

    # v3 walnuts use JSON tasks; v2 walnuts use per-bundle markdown tasks.
    unscoped_tasks = (tasks or {}).get("unscoped", [])
    if layout == "v3":
        tasks_json = {"tasks": list(unscoped_tasks)}
        # Per-bundle tasks land in the same flat list with a bundle field
        # so v3 walnut fixtures still carry them.
        for bundle_name, bundle_tasks in (tasks or {}).items():
            if bundle_name == "unscoped":
                continue
            for t in bundle_tasks:
                entry = dict(t)
                entry.setdefault("bundle", bundle_name)
                tasks_json["tasks"].append(entry)
        _write_text(
            os.path.join(kernel, "tasks.json"),
            json.dumps(tasks_json, indent=2) + "\n",
        )
        _write_text(
            os.path.join(kernel, "completed.json"),
            json.dumps({"completed": []}, indent=2) + "\n",
        )

    # v2 layout adds the _generated projection.
    if layout == "v2" and include_now_json:
        gen_dir = os.path.join(kernel, "_generated")
        os.makedirs(gen_dir, exist_ok=True)
        _write_text(
            os.path.join(gen_dir, "now.json"),
            json.dumps(_DEFAULT_NOW_JSON, indent=2) + "\n",
        )

    # ---- bundles ------------------------------------------------------------
    bundles_list = bundles or []
    if layout == "v3":
        # v3 flat: bundles live at walnut root
        for spec in bundles_list:
            _build_bundle_v3(walnut_root, spec)
    else:  # v2
        # v2 container: bundles live under bundles/<name>/ with per-bundle tasks.md
        bundles_container = os.path.join(walnut_root, "bundles")
        os.makedirs(bundles_container, exist_ok=True)
        for spec in bundles_list:
            _build_bundle_v2(bundles_container, spec, (tasks or {}))

    # ---- live context -------------------------------------------------------
    for live in (live_files or []):
        rel = live.get("path") if isinstance(live, dict) else None
        content = live.get("content", "") if isinstance(live, dict) else ""
        if not rel:
            continue
        _write_text(os.path.join(walnut_root, rel), content)

    # ---- sub-walnuts (used to test scan boundaries) -------------------------
    for sub in (sub_walnuts or []):
        sub_kwargs = dict(sub)
        sub_name = sub_kwargs.pop("name", "sub-walnut")
        sub_layout = sub_kwargs.pop("layout", layout)
        build_walnut(
            tmp_path=walnut_root,
            name=sub_name,
            layout=sub_layout,
            **sub_kwargs
        )

    return walnut_root


def _build_bundle_v3(walnut_root, spec):
    # type: (str, Dict[str, Any]) -> None
    """Materialise a v3 flat bundle (sibling of _kernel)."""
    bname = spec["name"]
    bdir = os.path.join(walnut_root, bname)
    os.makedirs(bdir, exist_ok=True)
    _write_text(
        os.path.join(bdir, "context.manifest.yaml"),
        _render_bundle_manifest(
            "v3",
            bname,
            spec.get("goal", "Bundle goal"),
            spec.get("status", "active"),
            spec.get("manifest_extra"),
        ),
    )
    for fname, body in (spec.get("files") or {}).items():
        _write_text(os.path.join(bdir, fname), body)
    raw_files = spec.get("raw_files") or {}
    if raw_files:
        os.makedirs(os.path.join(bdir, "raw"), exist_ok=True)
        for fname, body in raw_files.items():
            _write_text(os.path.join(bdir, "raw", fname), body)


def _build_bundle_v2(bundles_container, spec, all_tasks):
    # type: (str, Dict[str, Any], Dict[str, List[Dict[str, Any]]]) -> None
    """Materialise a v2 nested bundle (under bundles/<name>/) with tasks.md."""
    bname = spec["name"]
    bdir = os.path.join(bundles_container, bname)
    os.makedirs(bdir, exist_ok=True)
    _write_text(
        os.path.join(bdir, "context.manifest.yaml"),
        _render_bundle_manifest(
            "v2",
            bname,
            spec.get("goal", "Bundle goal"),
            spec.get("status", "active"),
            spec.get("manifest_extra"),
        ),
    )
    for fname, body in (spec.get("files") or {}).items():
        _write_text(os.path.join(bdir, fname), body)
    # Per-bundle tasks.md (markdown form for v2)
    bundle_tasks = all_tasks.get(bname, [])
    if bundle_tasks:
        lines = ["## Tasks", ""]
        for t in bundle_tasks:
            mark = "[x]" if t.get("status") == "done" else "[ ]"
            lines.append("- {0} {1}".format(mark, t.get("title", "untitled")))
        _write_text(
            os.path.join(bdir, "tasks.md"),
            "\n".join(lines) + "\n",
        )
    raw_files = spec.get("raw_files") or {}
    if raw_files:
        os.makedirs(os.path.join(bdir, "raw"), exist_ok=True)
        for fname, body in raw_files.items():
            _write_text(os.path.join(bdir, "raw", fname), body)


__all__ = ["build_walnut"]
