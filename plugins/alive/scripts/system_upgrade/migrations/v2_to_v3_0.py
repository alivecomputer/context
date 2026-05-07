"""v2 -> v3.0 migration runner (T9 of fn-18).

Public entry: :func:`run_v2_to_v3_0`. Consumes phase-3's
``DetectionReport``, phase-7's ``WalkthroughDecisions``, and the
post-backup world tree to execute v2 -> v3.0 operations in place.

Operations encoded as data (see :data:`_V2_TO_V3_0_OPS`). The runner
iterates ops in order, appending each result to the runstate file
(:mod:`._record`) after the per-op fsync. On success the in-memory
``MigrationReport`` is returned to the orchestrator for phase 12's
final-record write -- the runner NEVER writes the canonical
``<iso-ts>.yaml``.

Per-walnut scope
----------------
World-level migrations (``03_Inputs/`` -> ``03_Inbox/``,
``.walnut/`` -> ``.alive/``) execute against the world root once.
Per-walnut migrations (``_kernel/_generated/`` flatten,
``bundles/`` flatten, ``tasks.md`` -> ``tasks.json``,
``observations.md`` removal, duplicate-``now.md`` merge) execute
against each walnut listed in
``DetectionReport.per_walnut_versions`` whose resolved version is
below v3.0. T2's ``migrate_v2_layout`` operates on a staging
directory; T9 reuses individual transforms but adapts them to
in-place per-walnut application.

Idempotency
-----------
Every operation short-circuits when its preconditions no longer
hold (``_kernel/_generated/`` already absent, ``bundles/`` already
flattened, etc.). Running the runner twice in succession against
the same walnut produces an identical post-state.

Walkthrough apply (Codex M9 / phase 9)
--------------------------------------
After the v2 -> v3.0 filesystem ops complete, this module calls
:func:`walkthrough.apply.apply` to execute walkthrough decisions for
v3.0-retired patterns -- e.g. ``_core/`` -> ``_kernel/`` references
in ``.alive/skills/<name>/SKILL.md``. The walkthrough apply step
honours the operator's per-match decisions (skipped occurrences are
preserved; accepted occurrences get ``.bak.<ts>`` siblings + in-place
rewrites).

Stdlib-only (R10): no PyYAML / ruamel; runstate I/O via
:mod:`system_upgrade._record_codec`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

from _common import iso_now

from .. import _normalize_version
from . import _record, _retroactive
# Back-compat re-export: MigrationReport + OpResult moved to ``_record``
# so the package ``__init__`` can resolve them without dragging this
# 1,540 LOC body into the import graph. Direct imports of the form
# ``from system_upgrade.migrations.v2_to_v3_0 import MigrationReport``
# continue to work via this re-export, but only after ``v2_to_v3_0`` is
# itself imported -- bare access through the package goes through the
# eager binding in ``__init__.py``.
from ._record import MigrationReport, OpResult


__all__ = (
    "MigrationReport",
    "OpResult",
    "run_v2_to_v3_0",
)


_V2_TASKS_MD_LINE = re.compile(r"^- \[([ ~x])\]\s+(.+?)(?:\s+@(\S+))?\s*$")


# ---------------------------------------------------------------------------
# Per-walnut sub-ops
# ---------------------------------------------------------------------------


def _flatten_kernel_generated(
    walnut_root: str,
    *,
    dry_run: bool,
    now_provider,
) -> Optional[OpResult]:
    """Promote ``_kernel/_generated/now.json`` -> ``_kernel/now.json``.

    Returns None when the precondition is absent (already migrated --
    no ``_kernel/_generated/`` or no ``now.json`` inside).

    **Clobber-safety:** when
    ``_kernel/now.json`` ALREADY EXISTS at the destination -- e.g.
    because the upstream duplicate-``now.md`` merge already projected
    the merged content into the v3 canonical location -- this op MUST
    NOT overwrite it. The merged projection is load-bearing for content
    preservation; the legacy ``_generated/now.json`` is a stale copy by
    construction (the merge ran first because both kernels reference
    the same logical now-state). The legacy file is preserved as a
    ``.bak.<ts>`` sibling at the destination so an operator can audit
    the difference if needed.
    """
    generated_dir = os.path.join(walnut_root, "_kernel", "_generated")
    if not os.path.isdir(generated_dir):
        return None

    src = os.path.join(generated_dir, "now.json")
    dst = os.path.join(walnut_root, "_kernel", "now.json")

    moved = False
    preserved_existing = False
    if os.path.isfile(src):
        if dry_run:
            moved = True
        elif os.path.isfile(dst):
            # Clobber-safety: an upstream merge already projected the
            # canonical _kernel/now.json. Preserve the legacy generated
            # copy as a sibling backup for forensics, then remove the
            # source so the _generated/ container can be cleaned up.
            ts_suffix = now_provider().replace(":", "").replace("-", "")[:15]
            bak = os.path.join(
                walnut_root, "_kernel",
                "now.json.legacy-generated.bak." + ts_suffix,
            )
            try:
                shutil.copy2(src, bak)
            except OSError:
                # Backup is best-effort; continue with the removal.
                pass
            try:
                os.remove(src)
            except OSError:
                pass
            preserved_existing = True
        else:
            os.replace(src, dst)
            moved = True

    # Promote any other generated files into the flat _kernel/ then
    # remove the empty _generated/ container.
    if not dry_run:
        try:
            for entry in list(os.listdir(generated_dir)):
                gsrc = os.path.join(generated_dir, entry)
                gdst = os.path.join(walnut_root, "_kernel", entry)
                if os.path.exists(gdst):
                    # Don't clobber an existing _kernel/<entry>; leave
                    # the generated/ copy in place for the operator.
                    continue
                shutil.move(gsrc, gdst)
        except OSError:
            pass
        try:
            os.rmdir(generated_dir)
        except OSError:
            # Non-empty (clobber-skips above) or already gone -- leave
            # the directory and surface no error.
            pass

    if preserved_existing:
        detail = (
            "preserved upstream-projected _kernel/now.json; legacy "
            "_generated/now.json kept as .legacy-generated.bak.<ts>"
        )
    elif moved:
        detail = "now.json promoted"
    else:
        detail = "no now.json present"

    return OpResult(
        op_type="flatten_kernel_generated",
        from_path=generated_dir,
        to_path=os.path.join(walnut_root, "_kernel"),
        status="applied",
        timestamp=now_provider(),
        detail=detail,
        walnut_root=walnut_root,
    )


def _flatten_bundles(
    walnut_root: str,
    *,
    dry_run: bool,
    now_provider,
) -> List[OpResult]:
    """Flatten ``bundles/<name>/`` -> ``<name>/`` at the walnut root.

    Mirrors the v3 flat-bundles convention (per CLAUDE.md: "v3 uses
    flat layout; bundles live at walnut root, not under bundles/").
    Collisions with an existing root-level ``<name>/`` get the
    ``-imported`` suffix per ``migrate_v2_layout`` semantics.
    """
    bundles_dir = os.path.join(walnut_root, "bundles")
    if not os.path.isdir(bundles_dir):
        return []

    out: List[OpResult] = []
    try:
        children = sorted(os.listdir(bundles_dir))
    except OSError as exc:
        return [
            OpResult(
                op_type="flatten_bundles",
                from_path=bundles_dir,
                status="failed",
                timestamp=now_provider(),
                detail="listdir failed: {}".format(exc),
                walnut_root=walnut_root,
            )
        ]

    for name in children:
        src = os.path.join(bundles_dir, name)
        if not os.path.isdir(src):
            continue
        final_name = name
        dst = os.path.join(walnut_root, final_name)
        if os.path.exists(dst):
            final_name = "{}-imported".format(name)
            dst = os.path.join(walnut_root, final_name)
            if os.path.exists(dst):
                out.append(OpResult(
                    op_type="flatten_bundles",
                    from_path=src,
                    to_path=dst,
                    status="failed",
                    timestamp=now_provider(),
                    detail=(
                        "double collision: both {} and {}-imported "
                        "exist at walnut root".format(name, name)
                    ),
                    walnut_root=walnut_root,
                ))
                continue
        if not dry_run:
            try:
                shutil.move(src, dst)
            except OSError as exc:
                out.append(OpResult(
                    op_type="flatten_bundles",
                    from_path=src,
                    to_path=dst,
                    status="failed",
                    timestamp=now_provider(),
                    detail="move failed: {}".format(exc),
                    walnut_root=walnut_root,
                ))
                continue
        out.append(OpResult(
            op_type="flatten_bundles",
            from_path=src,
            to_path=dst,
            status="applied",
            timestamp=now_provider(),
            detail=(
                "" if final_name == name
                else "collision suffix applied: {}".format(final_name)
            ),
            walnut_root=walnut_root,
        ))

    # Remove the now-empty bundles/ container.
    if not dry_run:
        try:
            remaining = os.listdir(bundles_dir)
        except OSError:
            remaining = ["?"]
        if not remaining:
            try:
                os.rmdir(bundles_dir)
            except OSError:
                pass
    return out


def _convert_tasks_md(
    walnut_root: str,
    *,
    dry_run: bool,
    iso_timestamp: str,
    session_id: str,
    now_provider,
) -> List[OpResult]:
    """Convert each ``<bundle>/tasks.md`` to ``tasks.json`` in place.

    The bundle list is the set of top-level dirs under the walnut
    root that contain a ``tasks.md`` file. Bundles already carrying
    ``tasks.json`` are left untouched (warning detail recorded).

    Also handles the per-walnut ``_kernel/tasks.md`` case (the v3
    walnut-level tasks file), which mirrors the per-bundle pattern
    but lands under ``_kernel/tasks.json``.
    """
    out: List[OpResult] = []

    # Per-bundle conversion (top-level dirs).
    try:
        children = sorted(os.listdir(walnut_root))
    except OSError as exc:
        return [
            OpResult(
                op_type="convert_tasks_md",
                from_path=walnut_root,
                status="failed",
                timestamp=now_provider(),
                detail="listdir failed: {}".format(exc),
                walnut_root=walnut_root,
            )
        ]

    candidates: List[Tuple[str, str, str]] = []
    for name in children:
        if name.startswith(".") or name.startswith("_"):
            # Skip dotted dirs (.alive, .git) and the kernel
            # (_kernel handled separately); _core handled by its own
            # op upstream.
            continue
        bundle_dir = os.path.join(walnut_root, name)
        if not os.path.isdir(bundle_dir):
            continue
        tasks_md = os.path.join(bundle_dir, "tasks.md")
        if os.path.isfile(tasks_md):
            candidates.append((name, bundle_dir, tasks_md))

    # Walnut-level kernel tasks.md (separate path).
    kernel_tasks_md = os.path.join(walnut_root, "_kernel", "tasks.md")
    if os.path.isfile(kernel_tasks_md):
        candidates.append(("_kernel", os.path.join(walnut_root, "_kernel"), kernel_tasks_md))

    for bundle_name, bundle_dir, tasks_md in candidates:
        tasks_json = os.path.join(bundle_dir, "tasks.json")
        if os.path.isfile(tasks_json):
            out.append(OpResult(
                op_type="convert_tasks_md",
                from_path=tasks_md,
                to_path=tasks_json,
                status="skipped",
                timestamp=now_provider(),
                detail="tasks.json already present; left tasks.md untouched",
                walnut_root=walnut_root,
            ))
            continue

        try:
            with open(tasks_md, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            out.append(OpResult(
                op_type="convert_tasks_md",
                from_path=tasks_md,
                status="failed",
                timestamp=now_provider(),
                detail="read failed: {}".format(exc),
                walnut_root=walnut_root,
            ))
            continue

        parsed = _parse_v2_tasks_md(content, bundle_name, iso_timestamp, session_id)

        if not dry_run:
            try:
                _write_tasks_json(tasks_json, parsed)
            except OSError as exc:
                out.append(OpResult(
                    op_type="convert_tasks_md",
                    from_path=tasks_md,
                    to_path=tasks_json,
                    status="failed",
                    timestamp=now_provider(),
                    detail="write failed: {}".format(exc),
                    walnut_root=walnut_root,
                ))
                continue
            try:
                os.remove(tasks_md)
            except OSError:
                pass

        out.append(OpResult(
            op_type="convert_tasks_md",
            from_path=tasks_md,
            to_path=tasks_json,
            status="applied",
            timestamp=now_provider(),
            detail="{} task(s) converted".format(len(parsed)),
            walnut_root=walnut_root,
        ))

        # Companion completed.json (created at the same level as
        # tasks.json for the kernel-level conversion -- per drift
        # inventory: "_kernel/completed.json: created during tasks
        # migration"). Per-bundle conversions don't get one.
        if bundle_name == "_kernel" and not dry_run:
            completed_json = os.path.join(bundle_dir, "completed.json")
            if not os.path.isfile(completed_json):
                try:
                    _write_tasks_json(completed_json, [])
                    out.append(OpResult(
                        op_type="create_completed_json",
                        to_path=completed_json,
                        status="applied",
                        timestamp=now_provider(),
                        detail="created empty completed.json",
                        walnut_root=walnut_root,
                    ))
                except OSError as exc:
                    out.append(OpResult(
                        op_type="create_completed_json",
                        to_path=completed_json,
                        status="failed",
                        timestamp=now_provider(),
                        detail="write failed: {}".format(exc),
                        walnut_root=walnut_root,
                    ))

    return out


def _remove_observations(
    walnut_root: str,
    *,
    dry_run: bool,
    now_provider,
) -> Optional[OpResult]:
    """Remove ``_kernel/observations.md`` (content folded by phase-9 manifest pass).

    Per drift-inventory: "observations.md content folded into bundle
    manifest if present". This op is the final removal -- the content
    folding is handled by the upstream walkthrough/apply pass for the
    v3.0-retired-pattern catalog entries that match the file.
    """
    obs = os.path.join(walnut_root, "_kernel", "observations.md")
    if not os.path.isfile(obs):
        return None
    if not dry_run:
        try:
            os.remove(obs)
        except OSError as exc:
            return OpResult(
                op_type="remove_observations",
                from_path=obs,
                status="failed",
                timestamp=now_provider(),
                detail="remove failed: {}".format(exc),
                walnut_root=walnut_root,
            )
    return OpResult(
        op_type="remove_observations",
        from_path=obs,
        status="applied",
        timestamp=now_provider(),
        walnut_root=walnut_root,
    )


def _merge_duplicate_now(
    walnut_root: str,
    *,
    dry_run: bool,
    now_provider,
    timestamp_suffix: str,
) -> List[OpResult]:
    """Merge duplicate ``now.md`` / ``companion.md`` per.

    When BOTH ``<walnut>/<name>.md`` (root) AND
    ``<walnut>/_core/<name>.md`` exist (v1 / v2 mixed-state walnut),
    the migration MUST prefer the ``_core/``-rooted file as primary
    AND merge any unique content from the root-level file into it
    BEFORE producing ``_kernel/now.json``.

    Merge algorithm (per spec acceptance criterion):

    1. Parse both files as text.
    2. Concatenate non-overlapping line sets.
    3. Preserve order from the ``_core/``-rooted file.
    4. Append uniquely-rooted-file lines under a clearly-labeled
       ``## Merged from root-level <basename>.md (pre-migration duplicate)``
       section.
    5. **For ``now.md``**: project the merged markdown into
       ``_kernel/now.json`` so the post-migration v3 layout has the
       canonical projection populated (per:
       merged content must reach ``_kernel/now.json``, not just
       ``_core/now.md``). The projection here is a minimal
       migration-time payload (``{markdown, source, walnut, ...}``);
       the post-save hook regenerates the full structured projection
       on the next save via ``project.py``.

    The root-level file is removed AFTER the merge writes; a
    ``.bak.<ts>`` sibling preserves the pre-merge state at the
    root-level path until sweep aging.
    """
    out: List[OpResult] = []
    for basename in ("now.md", "companion.md"):
        root_path = os.path.join(walnut_root, basename)
        core_path = os.path.join(walnut_root, "_core", basename)
        if not (os.path.isfile(root_path) and os.path.isfile(core_path)):
            continue

        try:
            with open(core_path, "r", encoding="utf-8") as f:
                core_content = f.read()
            with open(root_path, "r", encoding="utf-8") as f:
                root_content = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            out.append(OpResult(
                op_type="merge_duplicate_now",
                from_path=root_path,
                to_path=core_path,
                status="failed",
                timestamp=now_provider(),
                detail="read failed: {}".format(exc),
                walnut_root=walnut_root,
            ))
            continue

        merged = _merge_markdown_unique_lines(
            core_content, root_content, basename,
        )

        if dry_run:
            out.append(OpResult(
                op_type="merge_duplicate_now",
                from_path=root_path,
                to_path=core_path,
                status="applied",
                timestamp=now_provider(),
                detail="(dry-run) would merge",
                walnut_root=walnut_root,
            ))
            continue

        # 1. Backup the root-level file (.bak.<ts>) before removing.
        bak_path = root_path + ".bak." + timestamp_suffix
        try:
            shutil.copy2(root_path, bak_path)
        except OSError as exc:
            out.append(OpResult(
                op_type="merge_duplicate_now",
                from_path=root_path,
                to_path=core_path,
                status="failed",
                timestamp=now_provider(),
                detail="backup write failed: {}".format(exc),
                walnut_root=walnut_root,
            ))
            continue

        # 2. Atomically write the merged content to the _core/ path.
        try:
            tmp = core_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(merged)
            os.replace(tmp, core_path)
        except OSError as exc:
            out.append(OpResult(
                op_type="merge_duplicate_now",
                from_path=root_path,
                to_path=core_path,
                status="failed",
                timestamp=now_provider(),
                detail="merge write failed: {}".format(exc),
                walnut_root=walnut_root,
            ))
            continue

        # 3. Remove the root-level file.
        try:
            os.remove(root_path)
        except OSError as exc:
            # The merge is already on disk; surface the cleanup
            # failure as a warning-level detail but mark the op
            # applied (the load-bearing content preservation is
            # complete).
            out.append(OpResult(
                op_type="merge_duplicate_now",
                from_path=root_path,
                to_path=core_path,
                status="applied",
                timestamp=now_provider(),
                detail=(
                    "merged but could not remove root-level file: {}"
                    .format(exc)
                ),
                walnut_root=walnut_root,
            ))
            continue

        out.append(OpResult(
            op_type="merge_duplicate_now",
            from_path=root_path,
            to_path=core_path,
            status="applied",
            timestamp=now_provider(),
            detail=(
                "merged duplicate {}; backup at {}".format(
                    basename, os.path.basename(bak_path),
                )
            ),
            walnut_root=walnut_root,
        ))

        # 4. For now.md only: project the merged content into
        #    ``_kernel/now.json`` so the v3.0 canonical projection
        #    location is populated post-migration. This is a minimal
        #    migration-time payload -- ``project.py`` regenerates the
        #    full structured projection on the next post-save hook run.
        if basename == "now.md":
            kernel_dir = os.path.join(walnut_root, "_kernel")
            now_json_path = os.path.join(kernel_dir, "now.json")
            try:
                if not os.path.isdir(kernel_dir):
                    os.makedirs(kernel_dir, exist_ok=True)
                payload = {
                    "schema_version": "1",
                    "source": "v2_to_v3_0_migration",
                    "walnut": os.path.basename(walnut_root),
                    "merged_from": [basename, "_core/" + basename],
                    "markdown": merged,
                }
                tmp_json = now_json_path + ".tmp"
                with open(tmp_json, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                os.replace(tmp_json, now_json_path)
                out.append(OpResult(
                    op_type="project_kernel_now_json",
                    from_path=core_path,
                    to_path=now_json_path,
                    status="applied",
                    timestamp=now_provider(),
                    detail="projected merged now.md into _kernel/now.json",
                    walnut_root=walnut_root,
                ))
            except OSError as exc:
                out.append(OpResult(
                    op_type="project_kernel_now_json",
                    from_path=core_path,
                    to_path=now_json_path,
                    status="failed",
                    timestamp=now_provider(),
                    detail="now.json write failed: {}".format(exc),
                    walnut_root=walnut_root,
                ))

    return out


def _merge_markdown_unique_lines(
    core_content: str, root_content: str, basename: str,
) -> str:
    """Concatenate non-overlapping lines, preserving _core/ order.

    Lines unique to the root-level file are appended under a clearly
    labeled section. Lines present in both files surface only once
    (in their _core/ position).
    """
    core_lines = core_content.splitlines()
    root_lines = root_content.splitlines()
    core_set = set(line.strip() for line in core_lines if line.strip())
    unique_root: List[str] = []
    for line in root_lines:
        stripped = line.strip()
        if not stripped:
            unique_root.append(line)
            continue
        if stripped in core_set:
            continue
        unique_root.append(line)

    # If every root line was a duplicate, no merged section is needed.
    if not any(line.strip() for line in unique_root):
        # Preserve trailing newline from the core content if present.
        return core_content if core_content.endswith("\n") else core_content + "\n"

    parts: List[str] = [core_content.rstrip("\n")]
    parts.append("")
    parts.append(
        "## Merged from root-level {} (pre-migration duplicate)".format(basename)
    )
    parts.append("")
    parts.extend(unique_root)
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# tasks.md parsing helpers (mirror migrate_v2_layout's logic)
# ---------------------------------------------------------------------------


def _parse_v2_tasks_md(content, bundle_name, iso_timestamp, session_id):
    """Mirror of ``_alive_common.migrate._parse_v2_tasks_md``.

    Inlined here rather than imported because the upstream helper is
    private (``_parse_v2_tasks_md``) AND embeds session-id semantics
    we do not want to forward-couple to. Keeping the parse local
    means future v3.0 -> v3.x runners can adapt the schema without
    perturbing the alive-p2p shim's contract.
    """
    tasks = []
    seq = 0

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
    """Atomic JSON write with the v3 ``{"tasks": [...]}`` envelope."""
    dir_path = os.path.dirname(path)
    if dir_path and not os.path.isdir(dir_path):
        os.makedirs(dir_path, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# World-level sub-ops
# ---------------------------------------------------------------------------


def _rename_inputs_inbox(
    world_root: str,
    *,
    dry_run: bool,
    now_provider,
) -> Optional[OpResult]:
    """Rename ``03_Inputs/`` -> ``03_Inbox/`` at the world root."""
    src = os.path.join(world_root, "03_Inputs")
    dst = os.path.join(world_root, "03_Inbox")
    if not os.path.isdir(src):
        return None
    if os.path.exists(dst):
        return OpResult(
            op_type="rename_inputs_inbox",
            from_path=src,
            to_path=dst,
            status="failed",
            timestamp=now_provider(),
            detail="destination 03_Inbox already exists; manual reconciliation needed",
        )
    if not dry_run:
        try:
            os.rename(src, dst)
        except OSError as exc:
            return OpResult(
                op_type="rename_inputs_inbox",
                from_path=src,
                to_path=dst,
                status="failed",
                timestamp=now_provider(),
                detail="rename failed: {}".format(exc),
            )
    return OpResult(
        op_type="rename_inputs_inbox",
        from_path=src,
        to_path=dst,
        status="applied",
        timestamp=now_provider(),
    )


def _rename_walnut_alive(
    world_root: str,
    *,
    dry_run: bool,
    now_provider,
) -> Optional[OpResult]:
    """Rename ``.walnut/`` -> ``.alive/`` at the world root.

    A pre-existing ``.alive/`` (typical for v3 worlds in transition)
    is treated as a soft-merge: contents under the source dir get
    merged into the existing destination. We do NOT clobber existing
    target-dir entries (the operator reconciles by hand if needed).
    """
    src = os.path.join(world_root, ".walnut")
    dst = os.path.join(world_root, ".alive")
    if not os.path.isdir(src):
        return None
    if not os.path.isdir(dst):
        if not dry_run:
            try:
                os.rename(src, dst)
            except OSError as exc:
                return OpResult(
                    op_type="rename_walnut_alive",
                    from_path=src,
                    to_path=dst,
                    status="failed",
                    timestamp=now_provider(),
                    detail="rename failed: {}".format(exc),
                )
        return OpResult(
            op_type="rename_walnut_alive",
            from_path=src,
            to_path=dst,
            status="applied",
            timestamp=now_provider(),
        )

    # Both exist -- soft-merge children that don't already exist in
    # the destination.
    merged = 0
    skipped = 0
    if not dry_run:
        try:
            entries = os.listdir(src)
        except OSError as exc:
            return OpResult(
                op_type="rename_walnut_alive",
                from_path=src,
                to_path=dst,
                status="failed",
                timestamp=now_provider(),
                detail="listdir failed: {}".format(exc),
            )
        for name in entries:
            sentry = os.path.join(src, name)
            dentry = os.path.join(dst, name)
            if os.path.exists(dentry):
                skipped += 1
                continue
            try:
                shutil.move(sentry, dentry)
                merged += 1
            except OSError:
                skipped += 1
        # Try to clean up the (possibly empty) source dir.
        try:
            if not os.listdir(src):
                os.rmdir(src)
        except OSError:
            pass
    return OpResult(
        op_type="rename_walnut_alive",
        from_path=src,
        to_path=dst,
        status="applied",
        timestamp=now_provider(),
        detail="merged {} entries, {} pre-existing kept".format(merged, skipped),
    )


# ---------------------------------------------------------------------------
# Walkthrough-apply driver lives in ``migrations._record`` as
# :func:`_apply_walkthrough_decisions` -- shared with the v3.0 -> v3.1
# and v3.1 -> v3.2 runners so the v3.0-retired catalog is driven by
# the same composition.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


_FROM_VERSION = "2.0"
_TO_VERSION = "3.0"


def run_v2_to_v3_0(
    world_root: str,
    snapshot: Any = None,
    plan: Any = None,
    walkthrough_decisions: Any = None,
    *,
    detection: Any = None,
    started_iso: Optional[str] = None,
    tool_version_at_run: str = "",
    session_id: str = "manual",
    dry_run: bool = False,
    now_provider=None,
    resume_marker: Any = None,
    halt_on_failure: bool = True,
) -> MigrationReport:
    """Execute v2 -> v3.0 migration ops + walkthrough apply.

    Parameters
    ----------
    world_root :
        Absolute path to the locked, post-backup world.
    snapshot :
        Phase-2 ``FileSnapshot`` (forensic; not consumed by the
        in-place migration ops, which read live disk for mutation
        targets). Reserved for future probe-style inspection.
    plan :
        Phase-3 / phase-4 plan payload. Reserved -- the runner
        currently derives its op set from the live filesystem so
        it remains robust to plan/disk drift between phases.
    walkthrough_decisions :
        Phase-7 ``WalkthroughDecisions`` to apply. ``None`` skips the
        walkthrough-apply step entirely.
    detection :
        Phase-3 ``DetectionReport``. The runner reads
        ``per_walnut_versions`` to scope per-walnut migrations and
        ``world_version`` to gate world-level ops. When None the
        runner falls back to a live-disk discovery (every directory
        under ``world_root`` containing ``_core/`` OR
        ``_kernel/_generated/`` OR ``bundles/`` is treated as a
        walnut needing migration).
    started_iso :
        Run-start timestamp (matches lock-meta sidecar's
        ``started_iso``). Defaults to the current UTC time.
    tool_version_at_run :
        Plugin version captured at run start (forensic).
    session_id :
        Session id stored on per-task records (matches
        ``migrate_v2_layout`` semantics).
    dry_run :
        ``True`` returns the planned ``MigrationReport`` without any
        filesystem writes (mirrors phase 9's ``--dry-run`` skip).
    now_provider :
        Callable returning ISO timestamp strings. Defaults to
        :func:`_common.iso_now`. Tests pin to a fixed value.
    resume_marker :
        Optional :class:`system_upgrade.state.ResumeMarker` for the
        in-flight upgrade run. When supplied, the runner advances the
        marker via T6's documented ``mark_step_running`` /
        ``mark_step_completed`` / ``mark_step_failed`` API and writes
        it via :func:`system_upgrade.resume.write_marker` after each
        successful op AND on any op failure. The marker write is the
        SOLE source of truth for ``--resume``; the forensic
        ``-runstate.yaml`` is independent.
    halt_on_failure :
        ``True`` (default) -- the runner stops issuing further ops as
        soon as one op returns ``status="failed"`` OR walkthrough
        apply raises. The marker is set to FAILED via
        ``mark_step_failed`` (when supplied). Set to ``False`` only
        when the caller explicitly wants best-effort continuation
        (test suites for individual ops typically pass False so a
        single-op failure does not mask coverage of subsequent ops).

    Returns
    -------
    MigrationReport
        Aggregated report. The orchestrator (phase 12) merges this
        into the canonical final upgrade record. The runner itself
        writes ONLY the runstate file (forensic), the resume marker
        update (T6's surface, when ``resume_marker`` is supplied),
        and -- when the world is messy -- the retroactive backfill
        record.
    """
    world_root = os.path.abspath(world_root)
    now_provider = now_provider or iso_now
    started_iso = started_iso or now_provider()
    timestamp_suffix = (
        started_iso.replace(":", "-").replace("Z", "")[:19]
        if started_iso else "00000000-000000"
    )

    report = MigrationReport(
        from_version=_FROM_VERSION,
        to_version=_TO_VERSION,
        started_iso=started_iso,
        dry_run=dry_run,
    )

    # ------------------------------------------------------------------
    # Initialise runstate (skipped under dry-run -- runstate is a
    # forensic side-effect, not part of the planned-output contract).
    # ------------------------------------------------------------------
    runstate_path: Optional[str] = None
    if not dry_run:
        try:
            runstate_path = _record.init_runstate(
                world_root,
                started_iso,
                tool_version_at_run=tool_version_at_run,
                from_version=_FROM_VERSION,
                to_version=_TO_VERSION,
            )
            report.runstate_path = runstate_path
        except OSError as exc:
            report.errors.append("runstate init failed: {}".format(exc))

    # ------------------------------------------------------------------
    # Resume marker handling (shared plumbing in
    # ``migrations._record.MigrationResumeTracker``).
    #
    # Marker semantics:
    #   * mark_step_running(PLUGIN_MIGRATE) fires ONCE at the start of
    #     the runner (``begin_running``), then per-op-progress writes
    #     refresh ``halted_iso`` (``refresh_running``) so the marker
    #     reflects in-flight progress without falsely advertising the
    #     WHOLE phase as completed.
    #   * mark_step_completed(PLUGIN_MIGRATE) fires ONCE at the end of
    #     the runner (``finalise_completed``), only on a clean finish
    #     (no halts, no failures). This is the only point where
    #     ``--resume`` should advance past phase 9.
    #   * mark_step_failed(PLUGIN_MIGRATE) fires on the FIRST op
    #     failure or walkthrough exception (``mark_failed``);
    #     halt_on_failure stops the loop at that point so the marker
    #     on disk reflects the actual halt point.
    # ------------------------------------------------------------------
    from ..state import Step  # noqa: PLC0415

    marker_tracker = _record.MigrationResumeTracker(
        world_root=world_root,
        step=Step.PLUGIN_MIGRATE,
        now_provider=now_provider,
        dry_run=dry_run,
        error_sink=report.errors,
        initial_marker=resume_marker,
    )
    marker_tracker.begin_running()

    def _record_op(op: OpResult) -> bool:
        """Append op to report + runstate; refresh marker progress.

        Returns ``True`` when the caller should keep issuing further
        ops (op succeeded / skipped, OR ``halt_on_failure`` is False).
        Returns ``False`` when the loop must stop (op failed AND
        ``halt_on_failure`` is True). The runstate append is
        best-effort; errors there do NOT halt the loop because the
        forensic file is independent of the migration's correctness.

        Marker semantics:
          * status == "failed"  -> mark_step_failed (terminal).
          * status == "applied" -> mark_step_running progress refresh.
          * status == "skipped" -> no marker write (the step body did
            no real work; the existing RUNNING marker is sufficient).
        """
        report.operations.append(op)
        if runstate_path is not None:
            try:
                _record.append_runstate_op(runstate_path, op.as_dict())
            except OSError as exc:
                report.errors.append(
                    "runstate append for {} failed: {}".format(op.op_type, exc)
                )
        if op.status == "failed":
            err_summary = "{}: {}".format(op.op_type, op.detail)
            report.errors.append(err_summary)
            marker_tracker.mark_failed(op.op_type, err_summary)
            if halt_on_failure:
                marker_tracker.set_halted()
                return False
            return True
        if op.status == "applied":
            marker_tracker.refresh_running()
        return True

    # ------------------------------------------------------------------
    # Resolve walnut list. Per-walnut migration runs against each
    # walnut whose detection result is below v3.0; if no detection is
    # supplied we fall back to a live-disk sweep that catches
    # legitimate v2 markers (`_core/`, `_kernel/_generated/`,
    # `bundles/`).
    # ------------------------------------------------------------------
    walnuts_to_migrate: List[str] = _resolve_walnuts(world_root, detection)
    report.walnuts_migrated = list(walnuts_to_migrate)

    # ------------------------------------------------------------------
    # World-level ops (run once per world, not per walnut).
    # The walrus-style `_record_op` returns False to signal halt.
    # ------------------------------------------------------------------
    world_op = _rename_inputs_inbox(
        world_root, dry_run=dry_run, now_provider=now_provider,
    )
    if world_op is not None and not _record_op(world_op):
        return _finalise(report, now_provider)
    world_op = _rename_walnut_alive(
        world_root, dry_run=dry_run, now_provider=now_provider,
    )
    if world_op is not None and not _record_op(world_op):
        return _finalise(report, now_provider)

    # ------------------------------------------------------------------
    # Per-walnut ops (each walnut gets the full sub-op chain).
    # ------------------------------------------------------------------
    for walnut_root in walnuts_to_migrate:
        # Duplicate-now / duplicate-companion merge MUST come BEFORE
        # the _core / _kernel/_generated flattens; otherwise the
        # _core/now.md primary copy disappears before we can merge.
        for op in _merge_duplicate_now(
            walnut_root,
            dry_run=dry_run,
            now_provider=now_provider,
            timestamp_suffix=timestamp_suffix,
        ):
            if not _record_op(op):
                return _finalise(report, now_provider)

        # Bundles flatten (top-level dirs under the walnut root).
        for op in _flatten_bundles(
            walnut_root, dry_run=dry_run, now_provider=now_provider,
        ):
            if not _record_op(op):
                return _finalise(report, now_provider)

        # _kernel/_generated flatten (per-walnut).
        op = _flatten_kernel_generated(
            walnut_root, dry_run=dry_run, now_provider=now_provider,
        )
        if op is not None and not _record_op(op):
            return _finalise(report, now_provider)

        # Tasks.md -> tasks.json (per bundle + per walnut kernel).
        for op in _convert_tasks_md(
            walnut_root,
            dry_run=dry_run,
            iso_timestamp=started_iso,
            session_id=session_id,
            now_provider=now_provider,
        ):
            if not _record_op(op):
                return _finalise(report, now_provider)

        # observations.md removal.
        op = _remove_observations(
            walnut_root, dry_run=dry_run, now_provider=now_provider,
        )
        if op is not None and not _record_op(op):
            return _finalise(report, now_provider)

    # ------------------------------------------------------------------
    # Walkthrough apply (Codex M9 / phase 9): rewrite v3.0-retired
    # patterns in user extensions. Per, exceptions
    # halt the migration when ``halt_on_failure`` is True; the marker
    # transitions to FAILED so ``--resume`` sees the run as halted at
    # phase 9.
    # ------------------------------------------------------------------
    try:
        applied, skipped = _record._apply_walkthrough_decisions(
            world_root,
            walkthrough_decisions,
            timestamp_suffix=timestamp_suffix,
            dry_run=dry_run,
        )
        report.walkthrough_applied = applied
        report.walkthrough_skipped = skipped
    except Exception as exc:  # noqa: BLE001
        err_summary = "walkthrough apply failed: {}".format(exc)
        report.errors.append(err_summary)
        marker_tracker.mark_failed("walkthrough_apply", err_summary)
        if halt_on_failure:
            marker_tracker.set_halted()
            return _finalise(report, now_provider)

    # ------------------------------------------------------------------
    # Retroactive synthesis for messy worlds. Run only on a clean
    # finish (no halt) so a partial-failure run does not write a
    # synthesized backfill that misrepresents the world's state.
    # ------------------------------------------------------------------
    if not dry_run and not marker_tracker.halted and not marker_tracker.had_failure:
        try:
            retro = _retroactive.synthesize_retroactive_record(
                world_root,
                started_iso,
                inferred_source_version=_FROM_VERSION,
                target_version=_TO_VERSION,
                tool_version_at_run=tool_version_at_run,
                operations=[op.as_dict() for op in report.operations],
                detection_signals=(
                    detection.all_signals_raw
                    if detection is not None
                    and getattr(detection, "all_signals_raw", None)
                    else None
                ),
            )
            report.retroactive_path = retro
        except OSError as exc:
            report.errors.append("retroactive synthesis failed: {}".format(exc))

    # Promote the marker to COMPLETED exactly once, after every per-
    # walnut + walkthrough op finished cleanly. On a halted / failed
    # run this is a no-op so the marker stays in FAILED / RUNNING and
    # ``--resume`` correctly re-enters phase 9.
    marker_tracker.finalise_completed()

    return _finalise(report, now_provider)


def _finalise(report: MigrationReport, now_provider) -> MigrationReport:
    """Stamp ``finished_iso`` and return. Centralised so every early-return
    halt path produces a fully-populated report.
    """
    report.finished_iso = now_provider()
    return report


# ---------------------------------------------------------------------------
# Walnut resolution
# ---------------------------------------------------------------------------


def _resolve_walnuts(
    world_root: str, detection: Any,
) -> List[str]:
    """Return the absolute walnut paths needing v2 -> v3.0 migration.

    Detection-driven path: filter ``per_walnut_versions`` to entries
    below v3.0. Fallback path (no detection supplied): live-disk
    sweep for v2 markers under the world root.
    """
    target = (3, 0, 0)
    if detection is not None:
        per_walnut = getattr(detection, "per_walnut_versions", None) or {}
        out: List[str] = []
        for walnut_path, version in per_walnut.items():
            try:
                if _normalize_version(version) < target:
                    out.append(os.path.abspath(walnut_path))
            except (ValueError, TypeError):
                # Unparseable version -- include it so the runner
                # tries to migrate (idempotent ops short-circuit on
                # already-v3 walnuts).
                out.append(os.path.abspath(walnut_path))
        # Also include the world root if the world version is below
        # target AND the world root looks walnut-shaped.
        try:
            world_v = _normalize_version(
                getattr(detection, "world_version", "") or "0.0"
            )
            if world_v < target and _looks_like_v2_walnut(world_root):
                if world_root not in out:
                    out.append(world_root)
        except (ValueError, TypeError):
            pass
        return sorted(set(out))

    # Fallback: live-disk sweep.
    found: List[str] = []
    if _looks_like_v2_walnut(world_root):
        found.append(world_root)
    for root, dirs, _files in os.walk(world_root):
        # Don't descend into hidden / ignored dirs.
        dirs[:] = [
            d for d in dirs
            if not (d.startswith(".") and d != "_kernel")
            and d not in ("node_modules", "__pycache__", "venv", ".venv")
        ]
        if root == world_root:
            continue
        if _looks_like_v2_walnut(root):
            found.append(root)
            # Walnut boundary: don't descend.
            dirs[:] = []
    return sorted(set(found))


def _looks_like_v2_walnut(path: str) -> bool:
    """True iff *path* carries any v2-walnut marker."""
    return (
        os.path.isdir(os.path.join(path, "_core"))
        or os.path.isdir(os.path.join(path, "_kernel", "_generated"))
        or os.path.isdir(os.path.join(path, "bundles"))
    )
