"""Per-operation runstate + retroactive record I/O for migrations (T9).

Two record kinds live alongside the canonical final upgrade record
under ``<world>/.alive/upgrades/``:

* ``<iso-ts>-runstate.yaml`` -- forensic-only incremental log of what
  each migrate/dispatch op did. Owned by T9/T10. Written by
  :func:`append_runstate_op` after each per-operation fsync. Consumed
  by no production code path (``--resume`` reads ONLY T6's
  ``-resume.yaml``; ``surfaces.load_prior_final_record`` excludes both
  suffixes by strict regex). The runstate exists for post-mortem
  debugging when a partial failure leaves a confused world.

* ``<iso-ts>-retroactive.yaml`` -- backfilled last-upgrade record for
  messy worlds with no ``.alive/upgrades/`` history but a fingerprint
  resolution implies prior upgrades happened. Carries
  ``synthesized_from: fingerprint`` to mark backfill provenance.
  Consumed only by T9's own retroactive synthesis path for
  de-duplication; T7's ``load_prior_final_record`` explicitly excludes
  this suffix.

This module is the sole writer of these two suffixes. It NEVER writes
the canonical final-record filename (``<iso-ts>.yaml`` with no
suffix); a unit test in ``test_migration_v2_to_v3_0.py`` asserts the
property by enumerating files written under ``.alive/upgrades/`` and
matching against the strict final-record regex from
``surfaces.__init__``.

Stdlib-only (R10): YAML I/O via :mod:`system_upgrade._record_codec`
(JSON-as-YAML); no PyYAML / ruamel / yaml_emit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import _record_codec


__all__ = (
    "RUNSTATE_SUFFIX",
    "RETROACTIVE_SUFFIX",
    "filename_safe_iso",
    "runstate_path_for",
    "retroactive_path_for",
    "init_runstate",
    "append_runstate_op",
    "write_retroactive",
    "MigrationResumeTracker",
    "_apply_walkthrough_decisions",
    "OpResult",
    "MigrationReport",
)


# ---------------------------------------------------------------------------
# Migration result + report dataclasses
# ---------------------------------------------------------------------------
#
# These types live here (rather than ``v2_to_v3_0.py``) so a bare
# ``import system_upgrade.migrations`` can resolve ``MigrationReport``
# and ``OpResult`` without dragging in ``v2_to_v3_0.py``'s 1,540 LOC
# body. Sibling runners (``v3_0_to_v3_1``, ``v3_1_to_v3_2``) import the
# types from here directly; the package ``__init__`` exposes them as
# eager attributes while ``run_v2_to_v3_0`` resolves lazily via PEP 562.


@dataclass(frozen=True)
class OpResult:
    """One discrete migration op.

    Attributes
    ----------
    op_type : str
        Stable identifier (``"flatten_kernel_generated"``,
        ``"flatten_bundles"``, ``"convert_tasks_md"``,
        ``"merge_duplicate_now"``, ``"rename_inputs_inbox"``,
        ``"rename_walnut_alive"``, ``"remove_observations"``).
    from_path : str
        Pre-migration absolute path the op consumed. Empty for ops
        that synthesise (e.g. ``"create_completed_json"``).
    to_path : str
        Post-migration absolute path the op produced. Empty for ops
        that purely remove.
    status : str
        ``"applied"`` (op did real work), ``"skipped"`` (precondition
        absent -- already migrated), ``"failed"`` (op raised; the
        runner records the error and continues).
    timestamp : str
        ISO 8601 UTC timestamp captured at op completion.
    detail : str
        Human-readable note (warning text, error message, count of
        rewritten files, etc.). Empty when the op was a clean apply.
    walnut_root : str
        Absolute walnut root the op acted within. Empty for
        world-level ops.
    """

    op_type: str
    from_path: str = ""
    to_path: str = ""
    status: str = "applied"
    timestamp: str = ""
    detail: str = ""
    walnut_root: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict form suitable for runstate codec serialisation."""
        return {
            "op_type": self.op_type,
            "from_path": self.from_path,
            "to_path": self.to_path,
            "status": self.status,
            "timestamp": self.timestamp,
            "detail": self.detail,
            "walnut_root": self.walnut_root,
        }


@dataclass
class MigrationReport:
    """In-memory output of one migration runner.

    The orchestrator (phase 12) merges every runner's report into the
    canonical final upgrade record -- the runner itself NEVER writes
    the final record. The runstate file is written by the runner for
    forensics; the retroactive file is written when the world is
    messy.
    """

    from_version: str = ""
    to_version: str = ""
    started_iso: str = ""
    finished_iso: str = ""
    operations: List["OpResult"] = field(default_factory=list)
    walkthrough_applied: List[Dict[str, Any]] = field(default_factory=list)
    walkthrough_skipped: List[Dict[str, Any]] = field(default_factory=list)
    runstate_path: Optional[str] = None
    retroactive_path: Optional[str] = None
    walnuts_migrated: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict form (consumed by orchestrator phase-12 merge)."""
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "started_iso": self.started_iso,
            "finished_iso": self.finished_iso,
            "operations": [op.as_dict() for op in self.operations],
            "walkthrough_applied": list(self.walkthrough_applied),
            "walkthrough_skipped": list(self.walkthrough_skipped),
            "runstate_path": self.runstate_path,
            "retroactive_path": self.retroactive_path,
            "walnuts_migrated": list(self.walnuts_migrated),
            "errors": list(self.errors),
            "dry_run": self.dry_run,
        }


#: Filename suffix for the forensic incremental runstate file.
RUNSTATE_SUFFIX: str = "-runstate.yaml"

#: Filename suffix for the synthesized backfill record.
RETROACTIVE_SUFFIX: str = "-retroactive.yaml"

#: Subdirectory under the world root where every upgrade-record family
#: lives. Mirrors ``state.MARKER_SUBDIR`` and the orchestrator's
#: ``.alive/upgrades/`` convention -- a single retention sweep covers
#: the canonical final record AND the suffixed siblings.
_UPGRADES_SUBDIR = os.path.join(".alive", "upgrades")


def filename_safe_iso(iso_ts: str) -> str:
    """Convert ``2026-05-04T01:23:45Z`` to ``2026-05-04T01-23-45Z``.

    Mirrors the convention used by ``orchestrator.write_noop_record_to_world``
    and ``resume._filename_safe_iso``: colons are not portable in
    filenames on every filesystem, so swap them for hyphens. The
    trailing ``Z`` is preserved so the ISO profile remains
    recognisable.
    """
    return iso_ts.replace(":", "-")


def runstate_path_for(world_root: str, started_iso: str) -> str:
    """Return the absolute runstate path for a given run-start timestamp.

    The pattern ``<filename-safe-iso>-runstate.yaml`` matches the
    overall ``<filename-safe-iso>{suffix}.yaml`` family used by T6's
    resume marker and T9's retroactive record. Lexical sort of the
    directory yields the most-recent runstate last (consistent with
    ``find_latest_marker`` in :mod:`..resume`).
    """
    fname = "{}{}".format(filename_safe_iso(started_iso), RUNSTATE_SUFFIX)
    return os.path.join(world_root, _UPGRADES_SUBDIR, fname)


def retroactive_path_for(world_root: str, started_iso: str) -> str:
    """Return the absolute retroactive-record path for a run-start ts."""
    fname = "{}{}".format(
        filename_safe_iso(started_iso), RETROACTIVE_SUFFIX,
    )
    return os.path.join(world_root, _UPGRADES_SUBDIR, fname)


def _ensure_upgrades_dir(path: str) -> None:
    """Create the parent ``.alive/upgrades/`` directory if absent.

    The codec's atomic-write primitive does NOT mkdir, so a fresh
    world (no prior upgrade run) needs the directory created on first
    write. Using ``exist_ok=True`` keeps the call idempotent across
    successive op-appends within a single run.
    """
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def init_runstate(
    world_root: str,
    started_iso: str,
    *,
    tool_version_at_run: str,
    from_version: str,
    to_version: str,
    walnut_root: Optional[str] = None,
) -> str:
    """Initialise the runstate file for a fresh migration run.

    Writes an empty ``operations: []`` payload with the run header.
    Subsequent calls to :func:`append_runstate_op` append individual
    op entries, each landing on disk via the codec's atomic
    write-replace cycle (so a crash mid-append leaves the prior
    runstate state intact).

    Returns the absolute runstate path for the caller to thread into
    the per-op append loop.
    """
    target = runstate_path_for(world_root, started_iso)
    _ensure_upgrades_dir(target)
    payload: Dict[str, Any] = {
        "schema_version": "1",
        "kind": "runstate",
        "started_at": started_iso,
        "tool_version_at_run": tool_version_at_run,
        "from_version": from_version,
        "to_version": to_version,
        "walnut_root": walnut_root,
        "operations": [],
    }
    _record_codec.write_atomic(target, payload)
    return target


def append_runstate_op(
    runstate_path: str,
    op: Dict[str, Any],
) -> None:
    """Append one op entry to the runstate file via read-modify-write.

    Read the current payload, append the op to ``operations``, and
    re-emit the whole file atomically. The atomic-replace cycle is the
    fsync barrier the spec calls for ("write each result to the
    runstate file via ``_record_codec.write_atomic()`` after fsync").

    The runstate is written ONCE per op rather than streamed because
    the codec is JSON-as-YAML (parser expects a single document). A
    streaming-append format would force a non-codec writer; the
    runstate MUST go through the codec (zero ``yaml_emit`` imports in
    this module -- enforced by T14 audit grep).
    """
    try:
        existing = _record_codec.read(runstate_path)
    except (OSError, ValueError):
        # Best-effort recovery: if the runstate file is unreadable we
        # rebuild a minimal payload around the new op rather than
        # crashing the whole migration. The original file (and any
        # codec error) is preserved for forensics by the surrounding
        # backup tarball; this branch protects forward progress.
        existing = {
            "schema_version": "1",
            "kind": "runstate",
            "operations": [],
        }
    if not isinstance(existing, dict):
        existing = {
            "schema_version": "1",
            "kind": "runstate",
            "operations": [],
        }
    operations = existing.get("operations")
    if not isinstance(operations, list):
        operations = []
    operations.append(dict(op))
    existing["operations"] = operations
    _record_codec.write_atomic(runstate_path, existing)


def write_retroactive(
    world_root: str,
    started_iso: str,
    *,
    inferred_source_version: str,
    target_version: str,
    tool_version_at_run: str,
    operations: Optional[List[Dict[str, Any]]] = None,
    detection_signals: Optional[Dict[str, Any]] = None,
) -> str:
    """Write the synthesized backfill record for a messy world.

    The retroactive record carries ``synthesized_from: fingerprint``
    to mark backfill provenance (vs ``synthesized_from: live_run`` on
    the canonical final record T9 contributes to via the in-memory
    ``MigrationReport``). Only T9's own retroactive synthesis path
    consumes the file (de-duplication); ``load_prior_final_record``
    excludes the ``-retroactive.yaml`` suffix by strict regex.

    Returns the absolute path written.
    """
    target = retroactive_path_for(world_root, started_iso)
    _ensure_upgrades_dir(target)
    payload: Dict[str, Any] = {
        "schema_version": "1",
        "kind": "retroactive",
        "synthesized_from": "fingerprint",
        "started_at": started_iso,
        "tool_version_at_run": tool_version_at_run,
        "inferred_source_version": inferred_source_version,
        "target_version": target_version,
        "operations": list(operations) if operations else [],
        "detection_signals": dict(detection_signals)
            if detection_signals else {},
    }
    _record_codec.write_atomic(target, payload)
    return target


# ---------------------------------------------------------------------------
# Resume-marker progress tracker (shared by every migration runner)
# ---------------------------------------------------------------------------
#
# The three migration runners (``run_v2_to_v3_0``, ``run_v3_0_to_v3_1``,
# ``run_v3_1_to_v3_2``) own identical resume-marker plumbing: ONE
# RUNNING write at runner entry, per-op halted_iso refresh, ONE
# COMPLETED write at clean finish, FAILED write on first op-level
# failure. Class below is the single owner. Runners stay as functions
# (the public ``run_v*`` API contract); composition is local --
# instantiate ``MigrationResumeTracker(...)`` at function entry and
# delegate marker writes to it.
#
# Class name is ``MigrationResumeTracker`` (not ``ResumeMarker``) to
# avoid collision with the existing ``state.ResumeMarker`` dataclass
# which IS the wire-format the codec serialises. The tracker OWNS a
# ``state.ResumeMarker`` instance and rebinds it after each
# ``mark_step_*`` transition.


class MigrationResumeTracker:
    """Shared resume-marker progress tracker for migration runners.

    Encapsulates the marker-write plumbing the three runners used to
    duplicate as nested ``_refresh_marker_running`` /
    ``_advance_marker_failed`` / ``_finalise_marker_completed``
    closures. Runners construct one tracker per call inside their
    function body; the tracker owns the mutable ``current`` marker
    reference, the ``halted`` / ``had_failure`` flags, and the
    ``Step`` enum value the marker advances against (always
    ``Step.PLUGIN_MIGRATE`` for migration runners today, but the
    field is parametric so a future post-migrate phase can reuse the
    same plumbing without forking the class).

    Marker semantics:
      * :meth:`begin_running` fires ONCE at runner entry. Records the
        initial RUNNING transition so resume sees the in-flight step.
      * :meth:`refresh_running` fires after each successful op.
        Re-emits the RUNNING marker with a fresh ``halted_iso`` so a
        later ``--resume-staleness`` check sees recent progress. Does
        NOT promote the step into ``completed_ops``.
      * :meth:`mark_failed` fires on the FIRST op-level failure or
        walkthrough exception. Records the FAILED marker via T6's
        ``mark_step_failed`` and sets ``had_failure``. Subsequent
        ``finalise_completed`` calls become no-ops because of this flag.
      * :meth:`finalise_completed` fires ONCE at clean finish (no
        halts, no failures). Promotes the step to COMPLETED so
        ``--resume`` correctly advances past the migration phase.
      * :meth:`set_halted` flips the ``halted`` flag the runner uses
        to gate retroactive-record synthesis (a halted run must NOT
        write a backfill that misrepresents the world's state).

    All marker-write failures are caught and routed through
    ``error_sink`` (the runner's ``report.errors`` list). A broken
    resume module MUST NOT take down the runner -- the marker is
    forensic; the runstate + report are the load-bearing outputs.
    """

    def __init__(
        self,
        *,
        world_root: str,
        step: Any,
        now_provider: Callable[[], str],
        dry_run: bool,
        error_sink: List[str],
        initial_marker: Any = None,
    ) -> None:
        self._world_root = world_root
        self._step = step
        self._now_provider = now_provider
        self._dry_run = dry_run
        self._errors = error_sink
        self.current = initial_marker
        self.halted = False
        self.had_failure = False

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------

    def _disabled(self) -> bool:
        """Marker writes are skipped under dry-run or no initial marker."""
        return self.current is None or self._dry_run

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def begin_running(self) -> None:
        """Record the initial RUNNING transition once at runner entry."""
        if self._disabled():
            return
        try:
            from .. import resume as _resume_module  # noqa: PLC0415

            running = _resume_module.mark_step_running(
                self.current,
                self._step,
                halted_iso=self._now_provider(),
            )
            _resume_module.write_marker(self._world_root, running)
            self.current = running
        except Exception as exc:  # noqa: BLE001
            self._errors.append(
                "resume marker initial RUNNING write failed: {}".format(exc)
            )

    def refresh_running(self) -> None:
        """Refresh the in-flight marker after a successful op.

        Re-emits the RUNNING marker with an advanced ``halted_iso``.
        Does NOT promote the step into ``completed_ops`` -- that
        happens exactly once via :meth:`finalise_completed`.
        """
        if self._disabled():
            return
        try:
            from .. import resume as _resume_module  # noqa: PLC0415

            refreshed = _resume_module.mark_step_running(
                self.current,
                self._step,
                halted_iso=self._now_provider(),
            )
            _resume_module.write_marker(self._world_root, refreshed)
            self.current = refreshed
        except Exception as exc:  # noqa: BLE001
            self._errors.append(
                "resume marker progress refresh failed: {}".format(exc)
            )

    def mark_failed(self, step_label: str, error_summary: str) -> None:
        """Mark the marker FAILED on op-level failure.

        ``step_label`` is the op_type / phase label that failed (used
        only in the error-sink message; the wire-format step is the
        ``Step`` enum value bound at construction time). Sets
        ``had_failure`` so a later ``finalise_completed`` becomes a
        no-op.
        """
        self.had_failure = True
        if self._disabled():
            return
        try:
            from .. import resume as _resume_module  # noqa: PLC0415

            new_marker = _resume_module.mark_step_failed(
                self.current,
                self._step,
                error_summary,
                halted_iso=self._now_provider(),
            )
            _resume_module.write_marker(self._world_root, new_marker)
            self.current = new_marker
        except Exception as exc:  # noqa: BLE001
            self._errors.append(
                "resume marker FAILED-write for {} failed: {}".format(
                    step_label, exc,
                )
            )

    def finalise_completed(self) -> None:
        """Promote the marker to COMPLETED on clean finish.

        No-op when the runner halted or recorded a failure; in that
        case the marker stays in FAILED / RUNNING so ``--resume``
        correctly re-enters the migration phase.
        """
        if self._disabled():
            return
        if self.had_failure or self.halted:
            return
        try:
            from .. import resume as _resume_module  # noqa: PLC0415

            completed = _resume_module.mark_step_completed(
                self.current,
                self._step,
                halted_iso=self._now_provider(),
            )
            _resume_module.write_marker(self._world_root, completed)
            self.current = completed
        except Exception as exc:  # noqa: BLE001
            self._errors.append(
                "resume marker COMPLETED finalise failed: {}".format(exc)
            )

    def set_halted(self) -> None:
        """Flip the halted flag (gates retroactive synthesis)."""
        self.halted = True


# ---------------------------------------------------------------------------
# Walkthrough-apply driver (shared by every migration runner)
# ---------------------------------------------------------------------------


def _apply_walkthrough_decisions(
    world_root: str,
    decisions: Any,
    *,
    timestamp_suffix: str,
    dry_run: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run T8's walkthrough/apply step and project the result to plain dicts.

    Returns ``(applied, skipped)`` -- both lists of plain dicts so the
    in-memory ``MigrationReport`` survives JSON-as-YAML
    serialisation. Under ``dry_run=True`` the apply step is skipped
    entirely (matches orchestrator phase 9 ``--dry-run`` semantics:
    walkthrough writes are mutating). When ``decisions is None`` the
    apply step is also skipped -- the runner had no walkthrough plan
    surfaced from phase 7.

    The catalog entries that drive the apply step are filtered
    server-side by phase 7's pre-scan and the operator's per-match
    decisions; this helper blindly applies whatever phase 7 surfaced.
    """
    if dry_run:
        return [], []
    if decisions is None:
        return [], []

    # Local import keeps walkthrough/ optional at module-load time
    # (the apply submodule has its own transitive imports we don't
    # want to pull into the runner module's import graph until we
    # actually need them).
    from ..walkthrough.apply import apply as walkthrough_apply  # noqa: PLC0415

    report = walkthrough_apply(
        world_root,
        decisions,
        timestamp=timestamp_suffix,
    )
    applied = [
        {
            "path": r.path,
            "backup_path": r.backup_path,
            "pattern_ids": list(r.pattern_ids),
            "rewrite_kinds": list(r.rewrite_kinds),
            "spans_applied": r.spans_applied,
            "backup_only": r.backup_only,
        }
        for r in report.applied
    ]
    skipped = [
        {
            "path": s.path,
            "pattern_id": s.pattern_id,
            "reason": s.reason,
            "detail": s.detail,
        }
        for s in report.skipped
    ]
    return applied, skipped
