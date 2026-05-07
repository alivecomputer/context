"""Phase 10: ``phase_surface_dispatch``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from ._shared import (
    _marker_completed,
    _marker_failed,
    _marker_running,
    _write_dispatch_results_sidecar,
)



def phase_surface_dispatch(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional[List[Any]]:
    """Phase 10: dispatch each compatible surface's migrator.

    Skipped under ``--surfaces=none`` (the surfaces_none flag set by
    phase 4). NEVER raises; surface-level exceptions are converted
    into failed/skipped DispatchResults inside ``dispatch_all``.
    """
    from system_upgrade.surfaces import dispatch_all

    if pipeline_context is None:
        return None
    _marker_running(pipeline_context, "surface_dispatch")
    try:
        if getattr(pipeline_context, "surfaces_none", False):
            # No-op under --surfaces=none. Phase 12 record emission
            # will carry forward the surviving retry map.
            _marker_completed(pipeline_context, "surface_dispatch")
            return None
        probe_results = pipeline_context.probe_results or []
        # surface_retry_map is already POST-stale-drop (phase 4
        # applied it so the no-op gate could see the surviving set).
        # Re-using the map here means dispatch sees the same items
        # the gate consulted -- no surprise re-evaluation between
        # phases.
        surface_retry_map = pipeline_context.surface_retry_map or {}

        dispatch_results = dispatch_all(
            world_root_resolved=world_root_resolved,
            probe_results=probe_results,
            surface_retry_map=surface_retry_map,
        )
        pipeline_context.dispatch_results = dispatch_results
        # persist the dispatch_results sidecar
        # so a resume past surface_dispatch can rehydrate ctx and
        # build the canonical record with the actual dispatch
        # outcomes (rather than an empty surfaces section). The
        # sidecar is written atomically to keep crash semantics
        # identical to the rest of the orchestrator.
        try:
            _write_dispatch_results_sidecar(
                world_root_resolved,
                pipeline_context.started_iso,
                dispatch_results,
            )
        except OSError:
            # Best-effort: the sidecar is a resume-safety net, not a
            # hard contract. The mainline run already has the data
            # in memory; the sidecar only matters if we crash before
            # phase_record runs.
            pass
    except Exception as exc:  # noqa: BLE001
        _marker_failed(pipeline_context, "surface_dispatch", str(exc))
        raise
    _marker_completed(pipeline_context, "surface_dispatch")
    return dispatch_results
