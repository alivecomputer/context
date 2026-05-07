"""Phase 2: ``phase_snapshot``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from ._shared import _marker_completed, _marker_failed, _marker_running



def phase_snapshot(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional[Any]:
    """Phase 2: build a frozen :class:`FileSnapshot` of the world.

    Aggregates snapshot rule contributions from every signal source
    (T3's ``version_detect.snapshot_rule_contributions``) plus the
    user-extension trees + plugin-surface paths verify (T4) needs to
    read. Stores the result on ``ctx.snapshot``.
    """
    ctx = pipeline_context
    if ctx is not None:
        _marker_running(ctx, "snapshot")
    try:
        from ..file_snapshot import FileSnapshot, SnapshotRule  # noqa: PLC0415
        from ..version_detect import (  # noqa: PLC0415
            snapshot_rule_contributions,
        )

        # Resolve the plugin root once -- snapshot rules use the
        # ``<plugin_root>`` template to capture plugin-side files
        # (hooks.json, plugin.json, skill manifests).
        plugin_root_override = getattr(args, "plugin_root", None)
        try:
            from _common import resolve_plugin_root  # noqa: PLC0415
            plugin_root = resolve_plugin_root(plugin_root_override)
        except (ImportError, FileNotFoundError):
            plugin_root = plugin_root_override or ""

        rules: List[SnapshotRule] = list(snapshot_rule_contributions())

        # Plugin-surface paths (verify reads these via the snapshot in
        # dry-run; real runs call ``Path.read_bytes`` directly but the
        # snapshot still captures them so detection / forensic callers
        # see the same content).
        rules.append(SnapshotRule(
            glob="<plugin_root>/.claude-plugin/plugin.json",
            max_bytes=64 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<plugin_root>/hooks/hooks.json",
            max_bytes=64 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<plugin_root>/skills/**/SKILL.md",
            max_bytes=64 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<plugin_root>/skills/**/*.md",
            max_bytes=64 * 1024,
        ))

        # User-extension trees (T4 catalog matcher reads these; T8
        # walkthrough re-reads via the snapshot for dry-run apply).
        rules.append(SnapshotRule(
            glob="<world>/.alive/skills/**/*.md",
            max_bytes=128 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<world>/.alive/rules/**/*.md",
            max_bytes=128 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<world>/.alive/hooks/**/*",
            max_bytes=128 * 1024,
        ))
        # World-root v1/v2 walnut markers + canonical-domain walnuts.
        rules.append(SnapshotRule(
            glob="<world>/companion.md", max_bytes=64 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<world>/now.md", max_bytes=64 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<world>/key.md", max_bytes=64 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<world>/log.md", max_bytes=64 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<world>/insights.md", max_bytes=64 * 1024,
        ))
        rules.append(SnapshotRule(
            glob="<world>/_core/**/*", exists_only=True,
        ))

        snapshot = FileSnapshot.populate(
            world_root_resolved, plugin_root or "", rules,
        )
    except Exception as exc:  # noqa: BLE001
        if ctx is not None:
            _marker_failed(ctx, "snapshot", str(exc))
        raise

    if ctx is not None:
        ctx.snapshot = snapshot
        _marker_completed(ctx, "snapshot")
    return snapshot
