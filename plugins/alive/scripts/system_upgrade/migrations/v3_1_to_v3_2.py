"""v3.1 -> v3.2 migration runner (T10 of fn-18).

Public entry: :func:`run_v3_1_to_v3_2`. Consumes phase-3's
``DetectionReport``, phase-7's ``WalkthroughDecisions``, and the
post-backup world tree to execute v3.1 -> v3.2 operations in place.

What the v3.1 -> v3.2 transition actually did (per drift-inventory):

* ``/alive:demo`` skill scaffolds full demo worlds (additive, no
  user-state migration).
* ``validate.py`` enforces structural invariants. The validator lives
  on the plugin side under ``plugins/alive/skills/demo/validate.py``;
  it is not part of the user world. We record its presence on the
  upgrade record (forensic) but there is nothing to migrate user-side.
* Skill count: 19 -> 20 (CLAUDE.md updated). Plugin-side metadata only.
* Plugin manifest: 3.1.0 -> 3.2.0. Plugin-side metadata only.
* ``_stage_outputs/entities/`` post-install pruning -- the demo skill's
  internal cleanup contract. Stragglers from earlier demo runs are the
  ONLY user-world side-effect; :mod:`.demo_cleanup` handles them via
  the ``_stage_outputs/.demo-state.yaml`` marker.

So this task ships:

* No bespoke filesystem rewrite logic (additive transition;
  ``validate.py`` is plugin-side only).
* A ``validate_py_present`` detection op that records whether the
  plugin-side validator exists at the expected path (when the
  ``plugin_root`` argument is supplied; otherwise ``status="skipped"``
  with detail "plugin_root not provided").
* The :mod:`demo_cleanup` op which removes ``_stage_outputs/`` IFF the
  fn-17 marker file declares ``complete: true`` (or the marker is
  absent + ``entities/`` is present, signalling a pre-fn-17
  abandoned demo).

Idempotency
-----------
* The ``validate_py_present`` detection is read-only and produces the
  same result on identical input.
* :func:`demo_cleanup.run_demo_cleanup` short-circuits when nothing is
  there (no marker, no entities/) so a second run is a no-op.

Stdlib-only (R10): no PyYAML / ruamel; runstate I/O via
:mod:`system_upgrade._record_codec`.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from _common import iso_now

from . import _record, _retroactive, demo_cleanup
from ._record import MigrationReport, OpResult


__all__ = (
    "run_v3_1_to_v3_2",
)


_FROM_VERSION = "3.1"
_TO_VERSION = "3.2"


# Path of the v3.2 validator relative to the plugin root. Mirrors the
# physical layout under ``plugins/alive/skills/demo/validate.py``.
_VALIDATE_PY_RELPATH = os.path.join("skills", "demo", "validate.py")


# ---------------------------------------------------------------------------
# Validate.py presence (plugin-side, forensic-only)
# ---------------------------------------------------------------------------


def _detect_validate_py(
    plugin_root: Optional[str], now_provider,
) -> OpResult:
    """Record whether the plugin's ``validate.py`` is on disk.

    The validator is the v3.2 structural invariant for the demo skill.
    It lives on the plugin side, not the user world, so this is a
    detection-only forensic record. When ``plugin_root`` is not
    supplied (e.g. tests or worlds invoked without a discoverable
    plugin install), the op lands as ``status="skipped"``.
    """
    if not plugin_root:
        return OpResult(
            op_type="detect_validate_py",
            status="skipped",
            timestamp=now_provider(),
            detail="plugin_root not provided",
        )
    expected = os.path.join(plugin_root, _VALIDATE_PY_RELPATH)
    present = os.path.isfile(expected)
    return OpResult(
        op_type="detect_validate_py",
        from_path=expected,
        status="applied",
        timestamp=now_provider(),
        detail=(
            "validate.py present" if present
            else "validate.py absent at expected path"
        ),
    )


# ---------------------------------------------------------------------------
# Walkthrough-apply driver lives in ``migrations._record`` as
# :func:`_apply_walkthrough_decisions`; v3.1 -> v3.2 has no
# walkthrough-eligible catalog entries today (the transition was
# additive), but the shared helper is wired so a future catalog entry
# tagged ``source_version="3.2"`` slots in without code changes.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def run_v3_1_to_v3_2(
    world_root: str,
    snapshot: Any = None,
    plan: Any = None,
    walkthrough_decisions: Any = None,
    *,
    detection: Any = None,
    started_iso: Optional[str] = None,
    tool_version_at_run: str = "",
    session_id: str = "manual",
    plugin_root: Optional[str] = None,
    dry_run: bool = False,
    now_provider=None,
    resume_marker: Any = None,
    halt_on_failure: bool = True,
) -> MigrationReport:
    """Execute v3.1 -> v3.2 migration ops + demo cleanup.

    Parameters mirror :func:`run_v3_0_to_v3_1` plus:

    * ``plugin_root`` -- absolute path to the active plugin install.
      Used to resolve ``validate.py``'s expected location for the
      forensic presence record. ``None`` is tolerated (the detection
      op lands as ``status="skipped"``).

    Operations executed:

    1. ``detect_validate_py`` -- forensic presence record (plugin-side).
    2. ``demo_cleanup`` -- remove ``_stage_outputs/`` per the fn-17
       marker contract (cleanup, skip, or absent). The ``absent``
       branch yields no op record.

    Returns the ``MigrationReport`` the orchestrator (phase 12)
    merges into the canonical final upgrade record.
    """
    world_root = os.path.abspath(world_root)
    now_provider = now_provider or iso_now
    started_iso = started_iso or now_provider()
    timestamp_suffix = (
        started_iso.replace(":", "-").replace("Z", "")[:19]
        if started_iso else "00000000-000000"
    )

    report = MigrationReport(
        from_version=_FROM_VERSION,
        to_version=_TO_VERSION,
        started_iso=started_iso,
        dry_run=dry_run,
    )

    # ------------------------------------------------------------------
    # Initialise runstate (skipped under dry-run).
    # ------------------------------------------------------------------
    runstate_path: Optional[str] = None
    if not dry_run:
        try:
            runstate_path = _record.init_runstate(
                world_root,
                started_iso,
                tool_version_at_run=tool_version_at_run,
                from_version=_FROM_VERSION,
                to_version=_TO_VERSION,
            )
            report.runstate_path = runstate_path
        except OSError as exc:
            report.errors.append("runstate init failed: {}".format(exc))

    # ------------------------------------------------------------------
    # Resume marker handling (shared plumbing in
    # ``migrations._record.MigrationResumeTracker``).
    # ------------------------------------------------------------------
    from ..state import Step  # noqa: PLC0415

    marker_tracker = _record.MigrationResumeTracker(
        world_root=world_root,
        step=Step.PLUGIN_MIGRATE,
        now_provider=now_provider,
        dry_run=dry_run,
        error_sink=report.errors,
        initial_marker=resume_marker,
    )
    marker_tracker.begin_running()

    def _record_op(op: OpResult) -> bool:
        """Append op + advance marker. Returns False to halt the loop."""
        report.operations.append(op)
        if runstate_path is not None:
            try:
                _record.append_runstate_op(runstate_path, op.as_dict())
            except OSError as exc:
                report.errors.append(
                    "runstate append for {} failed: {}".format(op.op_type, exc)
                )
        if op.status == "failed":
            err_summary = "{}: {}".format(op.op_type, op.detail)
            report.errors.append(err_summary)
            marker_tracker.mark_failed(op.op_type, err_summary)
            if halt_on_failure:
                marker_tracker.set_halted()
                return False
            return True
        if op.status == "applied":
            marker_tracker.refresh_running()
        return True

    # ------------------------------------------------------------------
    # validate.py presence record (forensic).
    # ------------------------------------------------------------------
    validate_op = _detect_validate_py(plugin_root, now_provider)
    if not _record_op(validate_op):
        return _finalise(report, now_provider)

    # ------------------------------------------------------------------
    # Demo cleanup -- marker-file driven (fn-17 contract).
    # ------------------------------------------------------------------
    cleanup_decision = demo_cleanup.run_demo_cleanup(
        world_root, dry_run=dry_run,
    )
    if cleanup_decision is not None:
        # Translate the DemoCleanupResult into an OpResult so the
        # migration report carries a uniform shape across runners.
        # The structured payload (action, reason, marker_present,
        # entities_dir_present, removed) is preserved in the runstate
        # via DemoCleanupResult.as_dict() -- but report.operations
        # carries OpResult instances by contract, so we condense the
        # dict into the detail string for the upgrade record.
        #
        # Status mapping:
        #   * action == "cleanup" -> "applied" (directory removed)
        #   * action == "skip"    -> "skipped" (in-flight or
        #                            indeterminate marker; the runner
        #                            keeps going because nothing is
        #                            broken on disk)
        #   * action == "failed"  -> "failed"  (rmtree raised; the
        #                            directory is still on disk so we
        #                            MUST surface a failure so
        #                            ``halt_on_failure`` honours its
        #                            contract and the resume marker
        #                            does NOT promote PLUGIN_MIGRATE
        #                            to COMPLETED. Any other mapping
        #                            would let the v3.1 -> v3.2 phase
        #                            silently "succeed" with
        #                            ``_stage_outputs/`` left on disk
        #                            and the operator unaware.)
        if cleanup_decision.action == "cleanup":
            op_status = "applied"
        elif cleanup_decision.action == "failed":
            op_status = "failed"
        else:  # "skip"
            op_status = "skipped"
        detail_parts = [
            "action={}".format(cleanup_decision.action),
            "reason={}".format(cleanup_decision.reason),
            "marker_present={}".format(cleanup_decision.marker_present),
            "entities_dir_present={}".format(
                cleanup_decision.entities_dir_present
            ),
            "removed={}".format(cleanup_decision.removed),
        ]
        if cleanup_decision.detail:
            detail_parts.append(
                "detail={}".format(cleanup_decision.detail)
            )
        op = OpResult(
            op_type="demo_cleanup",
            from_path=cleanup_decision.stage_outputs_path,
            status=op_status,
            timestamp=now_provider(),
            detail="; ".join(detail_parts),
        )
        if not _record_op(op):
            return _finalise(report, now_provider)

    # ------------------------------------------------------------------
    # Walkthrough apply (currently no v3.2-eligible catalog entries;
    # wired for future-proofing).
    # ------------------------------------------------------------------
    try:
        applied, skipped = _record._apply_walkthrough_decisions(
            world_root,
            walkthrough_decisions,
            timestamp_suffix=timestamp_suffix,
            dry_run=dry_run,
        )
        report.walkthrough_applied = applied
        report.walkthrough_skipped = skipped
    except Exception as exc:  # noqa: BLE001
        err_summary = "walkthrough apply failed: {}".format(exc)
        report.errors.append(err_summary)
        marker_tracker.mark_failed("walkthrough_apply", err_summary)
        if halt_on_failure:
            marker_tracker.set_halted()
            return _finalise(report, now_provider)

    # ------------------------------------------------------------------
    # Retroactive synthesis (clean-finish only).
    # ------------------------------------------------------------------
    if (
        not dry_run
        and not marker_tracker.halted
        and not marker_tracker.had_failure
    ):
        try:
            retro = _retroactive.synthesize_retroactive_record(
                world_root,
                started_iso,
                inferred_source_version=_FROM_VERSION,
                target_version=_TO_VERSION,
                tool_version_at_run=tool_version_at_run,
                operations=[op.as_dict() for op in report.operations],
                detection_signals=(
                    detection.all_signals_raw
                    if detection is not None
                    and getattr(detection, "all_signals_raw", None)
                    else None
                ),
            )
            report.retroactive_path = retro
        except OSError as exc:
            report.errors.append("retroactive synthesis failed: {}".format(exc))

    marker_tracker.finalise_completed()

    return _finalise(report, now_provider)


def _finalise(report: MigrationReport, now_provider) -> MigrationReport:
    """Stamp ``finished_iso`` and return."""
    report.finished_iso = now_provider()
    return report
