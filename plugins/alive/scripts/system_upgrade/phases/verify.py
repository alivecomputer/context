"""Phase 11: ``phase_verify``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from ._shared import _marker_completed, _marker_failed, _marker_running



def phase_verify(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional[Any]:
    """Phase 11: live-read verification of the post-migration world.

    Runs :func:`verify.verify` with ``Path.read_bytes`` (real run) or
    the :class:`PostStateOverlay` read-through (dry-run) as the read
    provider. Stores the resulting :class:`VerificationReport` on
    ``ctx.verification_report``.
    """
    ctx = pipeline_context
    if ctx is not None:
        _marker_running(ctx, "verify")
    try:
        from pathlib import Path  # noqa: PLC0415

        from ..verify import PluginSurfacePaths, verify  # noqa: PLC0415

        plugin_root_override = getattr(args, "plugin_root", None)
        try:
            from _common import resolve_plugin_root  # noqa: PLC0415
            plugin_root = resolve_plugin_root(plugin_root_override)
        except (ImportError, FileNotFoundError):
            plugin_root = plugin_root_override or None

        # Keep "no plugin root" as None, never Path("") (which is
        # truthy and resolves to CWD -- that would silently read
        # ./hooks/hooks.json from the operator's current directory).
        plugin_root_path: Optional[Any] = (
            Path(plugin_root) if plugin_root else None
        )

        # Skill manifests (every SKILL.md under plugin /skills/).
        skill_manifests: Tuple[Any, ...] = ()
        if plugin_root_path is not None:
            skills_dir = plugin_root_path / "skills"
            if skills_dir.is_dir():
                skill_manifests = tuple(
                    sorted(skills_dir.glob("**/SKILL.md")),
                )

        # User-extension paths under the world.
        world_path = Path(world_root_resolved)
        user_ext: List[Any] = []
        for sub in ("skills", "rules", "hooks"):
            base = world_path / ".alive" / sub
            if base.is_dir():
                for p in sorted(base.rglob("*")):
                    if p.is_file():
                        user_ext.append(p)

        psp = PluginSurfacePaths(
            hooks_json=(
                plugin_root_path / "hooks" / "hooks.json"
                if plugin_root_path is not None else Path("/dev/null")
            ),
            plugin_json=(
                plugin_root_path / ".claude-plugin" / "plugin.json"
                if plugin_root_path is not None else Path("/dev/null")
            ),
            skill_manifests=skill_manifests,
            user_extension_paths=tuple(user_ext),
        )

        dry_run = bool(getattr(args, "dry_run", False))
        if dry_run and ctx is not None and ctx.overlay is not None:
            overlay = ctx.overlay
            snapshot = ctx.snapshot

            def _read_provider(p: Any) -> bytes:
                # Overlay read-through, falling back to live disk for
                # files the snapshot didn't capture (verify reads
                # plugin-side files that the world snapshot doesn't
                # cover).
                try:
                    return overlay.read_through(str(p), snapshot)
                except (FileNotFoundError, KeyError, ValueError):
                    return Path(p).read_bytes()

            report = verify(_read_provider, world_path, psp)
        else:
            report = verify(
                lambda p: Path(p).read_bytes(),
                world_path,
                psp,
            )
    except Exception as exc:  # noqa: BLE001
        if ctx is not None:
            _marker_failed(ctx, "verify", str(exc))
        raise

    if ctx is not None:
        ctx.verification_report = report
        _marker_completed(ctx, "verify")
    return report
