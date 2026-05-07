"""Phase 4: ``phase_probe_surfaces``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ._shared import _marker_completed, _marker_failed, _marker_running



def phase_probe_surfaces(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional[Tuple[Optional[List["ProbeResult"]], Dict[str, "SurfaceRetryRecord"]]]:
    """Phase 4: load prior retry state + (optionally) probe surfaces.

    The prior-record load runs UNCONDITIONALLY (even under
    ``--surfaces=none``) so the no-op gate sees pending retries from
    prior runs. The per-surface probe is SKIPPED when the
    ``--surfaces`` filter resolves to ``None`` (the ``"none"``
    sentinel).

    Stale-drop is applied here when running under ``--surfaces=none``
    (only the AGE clause runs without probe). For probed runs we
    defer the drop to phase 12 record-emission so the dispatch step
    sees the full carry-forward set.
    """
    # Local import keeps surfaces/ optional at module-load time -- a
    # broken surface impl shouldn't take down ``import system_upgrade.
    # orchestrator``.
    from system_upgrade.surfaces import (
        apply_stale_drop,
        load_prior_final_record,
        parse_surfaces_filter,
        probe_all,
    )

    if pipeline_context is not None:
        _marker_running(pipeline_context, "probe_surfaces")
    try:
        surfaces_arg = getattr(args, "surfaces", None)
        active = parse_surfaces_filter(surfaces_arg)

        surface_retry_map, prior_started_at, _prior_path = (
            load_prior_final_record(world_root_resolved)
        )
        if active is None:
            # --surfaces=none: probe is skipped entirely. Apply AGE
            # clause NOW so the no-op gate sees the post-drop map.
            surviving, dropped, version_skip = apply_stale_drop(
                surface_retry_map,
                prior_started_at=prior_started_at,
                probe_results=None,
            )
            if pipeline_context is not None:
                pipeline_context.probe_results = None
                pipeline_context.surface_retry_map = surviving
                # Stash the stale-drop output so phase-12 record
                # emission can reference it without re-running the
                # predicate.
                pipeline_context.stale_retry_dropped = list(dropped)
                pipeline_context.version_mismatch_check_skipped = list(
                    version_skip
                )
                pipeline_context.surfaces_none = True
                _marker_completed(pipeline_context, "probe_surfaces")
            return None, surviving

        probe_results = probe_all(active)
        # Apply stale-drop NOW (phase 4) so phase 5's no-op gate sees
        # the post-drop retry map. Deferring this to phase 10 (where
        # dispatch consumes the retries) leaves the gate evaluating
        # the raw map and can spuriously force a full upgrade on a
        # current-version world whose only blot is a stale retry
        # record that the predicate would have dropped.
        surviving, dropped, version_skip = apply_stale_drop(
            surface_retry_map,
            prior_started_at=prior_started_at,
            probe_results=probe_results,
        )
        if pipeline_context is not None:
            pipeline_context.probe_results = probe_results
            pipeline_context.surface_retry_map = surviving
            pipeline_context.surfaces_none = False
            pipeline_context.prior_started_at = prior_started_at
            pipeline_context.stale_retry_dropped = list(dropped)
            pipeline_context.version_mismatch_check_skipped = list(
                version_skip
            )
    except Exception as exc:  # noqa: BLE001
        if pipeline_context is not None:
            _marker_failed(pipeline_context, "probe_surfaces", str(exc))
        raise

    if pipeline_context is not None:
        _marker_completed(pipeline_context, "probe_surfaces")
    return probe_results, surviving
