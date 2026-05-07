"""Phase 3: ``phase_detect``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from ._shared import (
    PhaseNotImplemented,
    _marker_completed,
    _marker_failed,
    _marker_running,
)



def phase_detect(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional["DetectionReport"]:
    """Phase 3: run content-fingerprint detection.

    Delegates to :func:`version_detect.detect_world_version` against
    the snapshot phase 2 captured. Records the resulting
    ``DetectionReport`` on ``ctx.detection``.
    """
    ctx = pipeline_context
    if ctx is not None:
        _marker_running(ctx, "detect")
    try:
        from ..version_detect import (  # noqa: PLC0415
            DetectionRefusal,
            detect_world_version,
            union_walnuts,
        )

        snapshot = ctx.snapshot if ctx is not None else None
        if snapshot is None:
            raise PhaseNotImplemented(
                "phase_detect requires a populated snapshot on the "
                "pipeline context (phase_snapshot did not run)"
            )

        # Plugin root for tool-version capture (forensic only -- never
        # feeds the no-op gate per orchestrator spec).
        plugin_root_override = getattr(args, "plugin_root", None)
        try:
            from _common import resolve_plugin_root  # noqa: PLC0415
            plugin_root = resolve_plugin_root(plugin_root_override)
        except (ImportError, FileNotFoundError):
            plugin_root = plugin_root_override

        # Discover walnuts via the legacy-aware union finder so
        # detection sees v1/v2/v3 walnut shapes uniformly.
        walnuts: Optional[List[str]]
        legacy: Optional[List[str]]
        try:
            walnuts, legacy = union_walnuts(world_root_resolved)
        except Exception:  # noqa: BLE001
            walnuts, legacy = None, None

        try:
            detection = detect_world_version(
                snapshot,
                world_root_resolved,
                walnuts=walnuts,
                legacy_walnuts=legacy,
                plugin_root=plugin_root,
                assume_empty_world=bool(
                    getattr(args, "assume_empty_world", False),
                ),
            )
        except DetectionRefusal:
            # Detection refusal is a hard error from a phase the
            # operator can't bypass with a phase-stub error message.
            # Surface as a run failure: re-raise so the caller's
            # exception path takes over.
            raise
    except Exception as exc:  # noqa: BLE001
        if ctx is not None:
            _marker_failed(ctx, "detect", str(exc))
        raise

    if ctx is not None:
        ctx.detection = detection
        _marker_completed(ctx, "detect")
    return detection
