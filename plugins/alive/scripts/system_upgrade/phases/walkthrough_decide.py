"""Phase 7: ``phase_walkthrough_decide``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from ._shared import _marker_completed, _marker_failed, _marker_running



def phase_walkthrough_decide(
    args: Any,
    *,
    world_root_resolved: str,
    pipeline_context: Optional["PipelineContext"] = None,
) -> Optional[Any]:
    """Phase 7: collect operator decisions for retired-pattern matches.

    Reads ``ctx.detection.walkthrough_eligible_matches`` from phase 3.
    Honours ``--non-interactive`` + ``--ext-migration`` combinations.
    Stores the resulting :class:`WalkthroughDecisions` on
    ``ctx.walkthrough_decisions``.
    """
    ctx = pipeline_context
    if ctx is not None:
        _marker_running(ctx, "walkthrough_decide")
    try:
        from ..walkthrough.decide import (  # noqa: PLC0415
            WalkthroughAbort, decide,
        )

        matches: List[Any] = []
        if ctx is not None and ctx.detection is not None:
            matches = list(ctx.detection.walkthrough_eligible_matches)

        # Pick the decide-mode from CLI flags.
        non_interactive = bool(getattr(args, "non_interactive", False))
        ext_migration = getattr(args, "ext_migration", None)
        dry_run = bool(getattr(args, "dry_run", False))
        if non_interactive:
            if ext_migration == "skip":
                mode = "non_interactive_skip"
            elif ext_migration == "backup-only":
                mode = "non_interactive_backup_only"
            elif ext_migration == "rewrite":
                mode = "non_interactive_rewrite"
            elif ext_migration == "abort":
                mode = "non_interactive_abort"
            else:
                # Default in non-interactive mode: ``rewrite``. The original
                # default was ``abort``; an interim fix used
                # ``backup-only`` (fixed walnut_equal but left
                # retired patterns in originals, breaking idempotency
                # and the canary zero-retired-pattern post-condition).
                # ``rewrite`` writes both a ``.bak.<ts>`` sibling AND
                # rewrites the original to the catalog's replacement,
                # giving the only default that satisfies all three
                # contracts:
                #   * walnut_equal tests pass rc=0
                #   * canary post-upgrade has zero retired-pattern
                #     signals AND at least one ``.bak.<ts>`` file
                #   * idempotency tests short-circuit on 2nd run
                # Operators who want hard refusal can pass
                # ``--ext-migration abort`` explicitly.
                # EXCEPTION: under --dry-run nothing is applied; the
                # plan-output captures the hits as "would skip" and
                # the run completes with rc=0 either way -- ``skip``
                # is fine here because no .bak files would be written
                # in a dry run anyway.
                if dry_run:
                    mode = "non_interactive_skip"
                else:
                    mode = "non_interactive_rewrite"
        else:
            mode = "interactive"

        try:
            decisions = decide(
                matches,
                snapshot=ctx.snapshot if ctx is not None else None,
                mode=mode,
            )
        except WalkthroughAbort as exc:
            # The orchestrator surfaces this through the standard
            # phase-failure path; the CLI envelope picks up the error.
            if ctx is not None:
                _marker_failed(ctx, "walkthrough_decide", str(exc))
            raise
    except Exception as exc:  # noqa: BLE001
        if ctx is not None:
            _marker_failed(ctx, "walkthrough_decide", str(exc))
        raise

    if ctx is not None:
        ctx.walkthrough_decisions = decisions
        _marker_completed(ctx, "walkthrough_decide")
    return decisions
