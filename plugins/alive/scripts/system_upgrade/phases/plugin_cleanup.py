"""Phase 8: ``phase_plugin_cleanup``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from ._shared import _marker_completed, _marker_failed, _marker_running



def phase_plugin_cleanup(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional[Any]:
    """Phase 8: world-state cleanup + retained-tarball sweep.

    Delegates to :func:`cleanup.cleanup` for the catalog-driven
    deletion sweep, then runs :func:`sweep.sweep_tarballs` to prune
    older pre-upgrade tarballs (keeps the latest one even when older
    than ``--keep-tarballs``).
    """
    ctx = pipeline_context
    if ctx is not None:
        _marker_running(ctx, "plugin_cleanup")
    try:
        from ..cleanup import (  # noqa: PLC0415
            MODE_NORMAL, MODE_SURFACES_NONE, cleanup,
        )

        surfaces_none = bool(
            ctx is not None and getattr(ctx, "surfaces_none", False)
        )
        # Build the surface-state path union from probe results.
        surface_state: List[str] = []
        if ctx is not None and ctx.probe_results is not None:
            for p in ctx.probe_results:
                surface_state.extend(p.state_paths)

        dry_run = bool(getattr(args, "dry_run", False))
        cleanup_report = cleanup(
            world_root_resolved,
            snapshot=ctx.snapshot if ctx is not None else None,
            mode=MODE_SURFACES_NONE if surfaces_none else MODE_NORMAL,
            surface_state_paths=tuple(surface_state),
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        if ctx is not None:
            _marker_failed(ctx, "plugin_cleanup", str(exc))
        raise

    if ctx is not None:
        ctx.cleanup_report = cleanup_report
        _marker_completed(ctx, "plugin_cleanup")
    return cleanup_report
