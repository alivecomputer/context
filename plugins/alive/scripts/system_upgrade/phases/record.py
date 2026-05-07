"""Phase 12: ``phase_record``."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from _common import iso_now

from .._phase_helpers import enumerate_operations

from ._shared import (
    PhaseWriteError,
    _marker_completed,
    _marker_failed,
    _marker_running,
    _unique_record_path,
)



def phase_record(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional[str]:
    """Phase 12: write the canonical upgrade record + sweep old tarballs.

    Aggregates every phase's report into a schema-conforming
    UpgradeRecord, writes it atomically via
    :func:`_record_codec.write_atomic`, and runs the post-record
    tarball sweep (``--keep-tarballs``).
    """
    ctx = pipeline_context
    if ctx is not None:
        _marker_running(ctx, "record")
    try:
        from .. import _record_codec  # noqa: PLC0415
        from ..surfaces import build_surfaces_record_section  # noqa: PLC0415
        from ..sweep import sweep_tarballs  # noqa: PLC0415

        finished_iso = iso_now()
        started_iso = ctx.started_iso if ctx is not None else finished_iso
        tool_version = ctx.tool_version if ctx is not None else ""
        detection = ctx.detection if ctx is not None else None

        # Aggregate cleanup + migration ops (+ pre-upgrade backup
        # tarball pointer) into the canonical operation vocabulary
        # shared with the dry-run plan file. See
        # ``_phase_helpers.enumerate_operations``.
        cleanup_report = (
            getattr(ctx, "cleanup_report", None) if ctx is not None else None
        )
        migration_reports = (
            getattr(ctx, "migration_reports", None) if ctx is not None else None
        ) or []
        backup_path = (
            getattr(ctx, "backup_tarball_path", None) if ctx is not None else None
        )
        operations: List[Dict[str, Any]] = enumerate_operations(
            cleanup_report=cleanup_report,
            migration_reports=migration_reports,
            backup_tarball_path=backup_path,
        )

        # Walkthrough skipped list (for the schema field).
        walkthrough_skipped: List[Dict[str, Any]] = []
        decisions = (
            getattr(ctx, "walkthrough_decisions", None)
            if ctx is not None else None
        )
        if decisions is not None:
            for sk in getattr(decisions, "skipped", ()):
                walkthrough_skipped.append({
                    "path": sk.path,
                    "pattern_id": sk.pattern_id,
                    "reason": sk.reason,
                })

        # Surfaces section.
        surfaces_section = build_surfaces_record_section(
            probe_results=ctx.probe_results if ctx is not None else None,
            dispatch_results=(
                ctx.dispatch_results if ctx is not None else None
            ),
            carried_forward=(
                ctx.surface_retry_map if ctx is not None else {}
            ),
            version_mismatch_check_skipped=(
                ctx.version_mismatch_check_skipped if ctx is not None else []
            ),
            surfaces_none=bool(
                ctx is not None and getattr(ctx, "surfaces_none", False)
            ),
        )

        record = {
            "schema_version": "1",
            "started_at": started_iso,
            "finished_at": finished_iso,
            "tool_version_at_run": tool_version,
            "world_version": (
                detection.world_version if detection else ""
            ),
            "per_walnut_versions": (
                dict(detection.per_walnut_versions) if detection else {}
            ),
            "operations": operations,
            # ``reason`` is null on full-run records. Only no-op
            # short-circuit records carry a human-readable string; a
            # full upgrade run has no canonical decision to encode in
            # this slot, and the canonical schema mandates Optional[str]
            # here. See tests/upgrade/schema/upgrade_record.py for the
            # validator that enforces the same shape on the WRITE path.
            "reason": None,
            "surfaces": surfaces_section,
            "stale_retry_dropped": list(
                ctx.stale_retry_dropped if ctx is not None else []
            ),
            "walkthrough_skipped": walkthrough_skipped,
        }

        ts = finished_iso.replace(":", "-").replace("Z", "")
        # Collision-resolution: back-to-back runs in the same second
        # otherwise overwrite each other.
        record_path = _unique_record_path(
            os.path.join(world_root_resolved, ".alive", "upgrades"),
            ts,
        )
        try:
            _record_codec.write_atomic(record_path, record)
        except OSError as exc:
            raise PhaseWriteError("record", exc) from exc

        # Tarball sweep -- never let a sweep error fail the run after
        # the canonical record has been written.
        try:
            keep_days = int(getattr(args, "keep_tarballs", 30))
            # Keep the just-written tarball regardless of age.
            protect: Tuple[str, ...] = ()
            if backup_path:
                protect = (backup_path,)
            sweep_tarballs(
                world_root_resolved,
                keep_days=keep_days,
                protect=protect,
            )
        except Exception:  # noqa: BLE001
            pass
    except PhaseWriteError:
        if ctx is not None:
            _marker_failed(ctx, "record", "permission/filesystem error")
        raise
    except Exception as exc:  # noqa: BLE001
        if ctx is not None:
            _marker_failed(ctx, "record", str(exc))
        raise

    if ctx is not None:
        ctx.record_path = record_path
        _marker_completed(ctx, "record")
    return record_path
