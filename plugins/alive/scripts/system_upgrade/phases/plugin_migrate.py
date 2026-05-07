"""Phase 9: ``phase_plugin_migrate``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from .. import TARGET_WORLD_VERSION, _normalize_version

from ._shared import _marker_completed, _marker_failed, _marker_running



def phase_plugin_migrate(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional[List[Any]]:
    """Phase 9: chain v2->v3.0, v3.0->v3.1, v3.1->v3.2 migrations.

    The migration set is sequenced by the lowest detected version:
    v1 / v2 worlds run all three; v3.0 worlds run the last two; v3.1
    worlds run the v3.1->v3.2 step only; v3.2 worlds run nothing
    (the no-op gate already short-circuited those, but we still gate
    here to handle the ``--force-run`` corner case).

    Walkthrough decisions from phase 7 are passed through so the
    apply step (inside each migration runner) honours the operator's
    per-match choices.
    """
    ctx = pipeline_context
    if ctx is not None:
        _marker_running(ctx, "plugin_migrate")
    try:
        # Lowest detected version across world AND per-walnut maps
        # Using ``world_version`` alone would
        # skip walnut-local stragglers: a world can resolve to 3.2
        # via a world-scope floor (e.g. demo-state marker) while an
        # individual walnut still resolves to 3.0 due to a stale
        # walnut-local vestige. The migration runners are scoped per
        # walnut and are idempotent on already-migrated walnuts, so
        # sequencing from the lowest seen version is safe and
        # complete.
        from_version = "0.0"
        if ctx is not None and ctx.detection is not None:
            world_v_raw = ctx.detection.world_version or "0.0"
            per_walnut_raw = list(
                (ctx.detection.per_walnut_versions or {}).values()
            )
            try:
                world_vt = _normalize_version(world_v_raw)
            except (ValueError, TypeError):
                world_vt = None
            min_vt = world_vt
            min_str = world_v_raw
            for v in per_walnut_raw:
                try:
                    pv = _normalize_version(v)
                except (ValueError, TypeError):
                    continue
                if min_vt is None or pv < min_vt:
                    min_vt = pv
                    min_str = v
            from_version = min_str

        try:
            wv = _normalize_version(from_version)
        except (ValueError, TypeError):
            wv = (0, 0, 0)

        target = _normalize_version(TARGET_WORLD_VERSION)
        if wv >= target:
            # Already at target -- nothing to migrate. (This path is
            # hit under --force-run on a clean v3.2 world.)
            if ctx is not None:
                _marker_completed(ctx, "plugin_migrate")
            return []

        decisions = (
            ctx.walkthrough_decisions if ctx is not None else None
        )
        detection = ctx.detection if ctx is not None else None
        snapshot = ctx.snapshot if ctx is not None else None
        started_iso = ctx.started_iso if ctx is not None else None
        tool_version = ctx.tool_version if ctx is not None else ""

        plugin_root_override = getattr(args, "plugin_root", None)
        try:
            from _common import resolve_plugin_root  # noqa: PLC0415
            plugin_root = resolve_plugin_root(plugin_root_override)
        except (ImportError, FileNotFoundError):
            plugin_root = plugin_root_override

        reports: List[Any] = []

        # Each migration runner is idempotent: invoking against an
        # already-migrated world short-circuits per-op. Sequencing by
        # explicit version comparison keeps the chain robust to
        # partial-migration worlds (v2 fragments mixed with v3.0
        # walnuts -- the v2->v3.0 runner handles the v2 stragglers
        # and leaves the v3.0 walnuts alone).
        dry_run = bool(getattr(args, "dry_run", False))
        if wv < _normalize_version("3.0"):
            from ..migrations.v2_to_v3_0 import run_v2_to_v3_0  # noqa: PLC0415
            r = run_v2_to_v3_0(
                world_root_resolved,
                snapshot=snapshot,
                walkthrough_decisions=decisions,
                detection=detection,
                started_iso=started_iso,
                tool_version_at_run=tool_version,
                halt_on_failure=True,
                dry_run=dry_run,
            )
            reports.append(r)
        if wv < _normalize_version("3.1"):
            from ..migrations.v3_0_to_v3_1 import run_v3_0_to_v3_1  # noqa: PLC0415
            r = run_v3_0_to_v3_1(
                world_root_resolved,
                snapshot=snapshot,
                walkthrough_decisions=decisions,
                detection=detection,
                started_iso=started_iso,
                tool_version_at_run=tool_version,
                halt_on_failure=True,
                dry_run=dry_run,
            )
            reports.append(r)
        if wv < _normalize_version("3.2"):
            from ..migrations.v3_1_to_v3_2 import run_v3_1_to_v3_2  # noqa: PLC0415
            r = run_v3_1_to_v3_2(
                world_root_resolved,
                snapshot=snapshot,
                walkthrough_decisions=decisions,
                detection=detection,
                started_iso=started_iso,
                tool_version_at_run=tool_version,
                plugin_root=plugin_root,
                halt_on_failure=True,
                dry_run=dry_run,
            )
            reports.append(r)
    except Exception as exc:  # noqa: BLE001
        if ctx is not None:
            _marker_failed(ctx, "plugin_migrate", str(exc))
        raise

    if ctx is not None:
        ctx.migration_reports = reports
        _marker_completed(ctx, "plugin_migrate")
    return reports
