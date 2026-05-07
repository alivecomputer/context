"""Phase 6: ``phase_backup``."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, List, Optional

from _common import iso_now

from ._shared import (
    PhaseWriteError,
    _marker_completed,
    _marker_failed,
    _marker_running,
)



def phase_backup(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional[Any]:
    """Phase 6: write the atomic pre-upgrade tarball.

    Builds the selection from the cleanup + migrate plans (computed
    against the live disk -- both phases honour their own
    idempotency guards so re-deriving here cannot poison phase 8/9).
    Records the final tarball path on ``ctx.backup_tarball_path``.

    Skipped under ``--dry-run``; the dispatcher already filters those
    phases out.
    """
    ctx = pipeline_context
    if ctx is not None:
        _marker_running(ctx, "backup")
    try:
        from ..backup import (  # noqa: PLC0415
            build_backup_selection, create_backup,
        )
        from ..cleanup import build_cleanup_plan  # noqa: PLC0415

        # Filename-safe ISO from started_iso (orchestrator-owned timestamp).
        iso_ts = (ctx.started_iso if ctx is not None else "").replace(":", "-").replace("Z", "")
        if not iso_ts:
            iso_ts = iso_now().replace(":", "-").replace("Z", "")

        # Cleanup-plan-derived targets (live disk; idempotent).
        cleanup_targets: List[str] = []
        try:
            for tgt in build_cleanup_plan(world_root_resolved):
                cleanup_targets.append(tgt.absolute_path)
        except Exception:  # noqa: BLE001
            pass

        # Migrate targets: per-walnut _kernel/ + world-root walnut content.
        migrate_targets: List[str] = []
        if ctx is not None and ctx.detection is not None:
            for walnut_path in ctx.detection.per_walnut_versions.keys():
                migrate_targets.append(
                    os.path.join(walnut_path, "_kernel"),
                )
        # World-root walnut content / legacy markers.
        for name in (
            "companion.md", "now.md", "key.md", "log.md", "insights.md",
            "_core",
        ):
            p = os.path.join(world_root_resolved, name)
            if os.path.exists(p):
                migrate_targets.append(p)

        # Walkthrough-eligible user-extension files: phase 9 may
        # rewrite these in place (or write .bak.<ts> siblings under
        # --ext-migration=backup-only). The backup MUST capture the
        # pre-rewrite bytes so rollback covers exactly the files
        # phase 7/9 are allowed to change. Additionally, capture the
        # entire user-extension trees (.alive/skills, .alive/rules,
        # .alive/hooks) so future cleanup-action="walkthrough_rewrite"
        # entries that the catalog adds later are also recoverable.
        extra_targets: List[str] = []
        if ctx is not None and ctx.detection is not None:
            seen_paths: set = set()
            for m in ctx.detection.walkthrough_eligible_matches:
                p = getattr(m, "path", None)
                if p and p not in seen_paths:
                    seen_paths.add(p)
                    extra_targets.append(p)
        for sub in ("skills", "rules", "hooks"):
            tree = os.path.join(world_root_resolved, ".alive", sub)
            if os.path.isdir(tree):
                extra_targets.append(tree)

        selected, skipped = build_backup_selection(
            world_root_resolved,
            iso_ts,
            cleanup_targets=cleanup_targets,
            migrate_targets=migrate_targets,
            extra_targets=extra_targets,
        )

        if not selected:
            # Nothing to back up (clean v3.2 short-circuits via the
            # no-op gate before reaching here; this branch covers the
            # ``--force-run`` corner case where the gate was bypassed
            # but no mutating phase will fire).
            if ctx is not None:
                _marker_completed(ctx, "backup")
            return None

        report = create_backup(
            world_root_resolved,
            iso_ts,
            selected_paths=selected,
            skipped_self_inclusion=skipped,
        )
    except Exception as exc:  # noqa: BLE001
        if ctx is not None:
            _marker_failed(ctx, "backup", str(exc))
        raise

    if report.error:
        if ctx is not None:
            _marker_failed(ctx, "backup", report.error)
        raise PhaseWriteError(
            "backup", OSError("backup failed: " + report.error),
        )

    if ctx is not None:
        ctx.backup_tarball_path = report.final_path
        ctx.backup_report = report
        _marker_completed(ctx, "backup")
    return report
