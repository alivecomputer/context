"""Phase coordinator for ``alive system-upgrade``.

This module is the public surface of the system-upgrade pipeline:

* the dispatcher (:func:`run_pipeline`) for phases 2..13;
* the phase-5 ``should_short_circuit`` no-op gate (R20);
* dataclasses for :class:`DetectionReport`, :class:`ProbeResult`,
  :class:`SurfaceRetryRecord`, :class:`PipelineContext`, and the no-op
  record builder so consumers have a stable contract.

The 10 per-phase functions live under ``system_upgrade/phases/<name>.py``
(one phase per file). Each is re-exported from this module so fn-20's
documented import surface (``from .orchestrator import phase_record``,
etc.) keeps resolving. Shared helpers (marker transitions, exception
types, record-path helper, dispatch sidecar helpers,
``_resume_step_for_phase``, ``REPLAY_PHASES``) live in
``phases/_shared.py`` and are re-exported here for the same reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from _common import iso_now

from . import TARGET_WORLD_VERSION, _normalize_version
from ._noop_record_writer import write_noop_record
from ._phase_helpers import enumerate_operations

# Re-exports from phases/_shared.py: helpers + exception types that
# phase modules consume but external callers (fn-20, tests) import
# from this module by historic contract.
from .phases._shared import (  # noqa: F401  (re-exported)
    REPLAY_PHASES,
    PhaseNotImplemented,
    PhaseWriteError,
    _capture_world_fingerprint,
    _dispatch_sidecar_path,
    _hydrate_backup_tarball_path_on_resume,
    _hydrate_dispatch_results_on_resume,
    _marker_completed,
    _marker_failed,
    _marker_running,
    _phase_already_completed,
    _resume_step_for_phase,
    _unique_record_path,
    _write_dispatch_results_sidecar,
)

# Re-exports from phases/<name>.py: the 10 per-phase functions. These
# MUST stay re-exportable (fn-20 spec pins ``from .orchestrator import
# phase_record`` etc.). Tests reassign ``orchestrator.phase_<name>``
# directly to install fakes; the dispatcher resolves each call via
# ``getattr(self_mod, attr_name)`` so the reassignment lands.
from .phases.snapshot import phase_snapshot  # noqa: F401
from .phases.detect import phase_detect  # noqa: F401
from .phases.backup import phase_backup  # noqa: F401
from .phases.walkthrough_decide import phase_walkthrough_decide  # noqa: F401
from .phases.plugin_cleanup import phase_plugin_cleanup  # noqa: F401
from .phases.plugin_migrate import phase_plugin_migrate  # noqa: F401
from .phases.verify import phase_verify  # noqa: F401
from .phases.record import phase_record  # noqa: F401
from .phases.probe_surfaces import phase_probe_surfaces  # noqa: F401
from .phases.surface_dispatch import phase_surface_dispatch  # noqa: F401

if TYPE_CHECKING:
    # Imported for type hints only; ``surfaces/_base.py`` already imports
    # ``ProbeResult`` from this module at runtime, so a runtime import
    # would cycle. Field annotations referencing ``SubprocessOutcome``
    # MUST be string-quoted so they are not evaluated at class-creation
    # time.
    from .surfaces._base import SubprocessOutcome  # noqa: F401


__all__ = (
    "DetectionReport",
    "ProbeError",
    "ProbeResult",
    "SurfaceRetryRecord",
    "PipelineContext",
    "build_noop_record",
    "should_short_circuit",
    "PHASE_NAMES",
    "PHASE_NUMBERS",
    "PhaseNotImplemented",
    "PhaseWriteError",
    "POST_GATE_ORDER",
    "REPLAY_PHASES",
    "run_pipeline",
    "PipelineResult",
)


# Locked phase order (epic § Phase order). Phase 5 is the R20 no-op
# short-circuit, inserted between probe-surfaces and backup.
PHASE_NAMES: List[str] = [
    "preflight",            # 1
    "snapshot",             # 2
    "detect",               # 3
    "probe_surfaces",       # 4
    "noop_short_circuit",   # 5
    "backup",               # 6
    "walkthrough_decide",   # 7
    "plugin_cleanup",       # 8
    "plugin_migrate",       # 9
    "surface_dispatch",     # 10
    "verify",               # 11
    "record",               # 12
    "release",              # 13
]
PHASE_NUMBERS: Dict[str, int] = {
    name: idx + 1 for idx, name in enumerate(PHASE_NAMES)
}


# ---------------------------------------------------------------------------
# Phase output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DetectionReport:
    """Phase-3 output. Detection-only -- never carries phase-4 surface state.

    Attributes
    ----------
    world_version : str
        Lowest version inferred from the world's content fingerprints.
    per_walnut_versions : dict[str, str]
        Per-walnut version inferences (key: walnut basename).
    walkthrough_eligible_matches : list[tuple[str, str]]
        ``(path, pattern_id)`` pairs from T4's catalog matcher; phase 7
        renders prompts only for these candidates. T1 ships an empty
        default; T3 populates from the snapshot pre-scan.
    tool_version_at_run : str
        Plugin version read from ``plugin.json`` at run start. Recorded
        for ``--resume`` skew validation; NEVER feeds the no-op gate.
    all_signals_raw : dict
        Forensic-only payload of raw signal probes. Empty in T1.
    legacy_walnuts_discovered : list[str]
        T3's legacy-aware walnut discovery output. Empty in T1.
    """

    world_version: str = ""
    per_walnut_versions: Dict[str, str] = field(default_factory=dict)
    walkthrough_eligible_matches: List[Any] = field(default_factory=list)
    tool_version_at_run: str = ""
    all_signals_raw: Dict[str, Any] = field(default_factory=dict)
    legacy_walnuts_discovered: List[str] = field(default_factory=list)


@dataclass
class ProbeError:
    """Phase-4 surface probe error. ``is_hard_fail`` feeds the no-op gate.

    ``kind`` values:

    * ``parse_error`` -- subprocess exited 0 but stdout was not parseable
      JSON (or did not match the expected ``{version, compatible,
      state_paths, migrator_argv_prefix}`` shape).
    * ``non_zero_exit`` -- subprocess exited with a non-zero status.
    * ``timeout`` -- subprocess exceeded its timeout window and was killed.
    * ``missing_binary`` -- the surface's executable was not found on
      ``PATH`` (FileNotFoundError from subprocess). NOT a hard fail for
      no-op-gate purposes (a missing optional surface shouldn't force a
      run when the world is already at target version + nothing else
      pending).
    * ``migrator_argv_prefix_invalid`` -- the surface's
      ``--version --json`` returned a ``migrator_argv_prefix`` that
      violates the placeholder-free / list[str] contract. The
      surface is treated as ``compatible=False``.
    * ``not_yet_shipped`` -- the surface itself is a stub (Codex). Soft
      signal only.
    """

    kind: str  # parse_error | non_zero_exit | timeout | missing_binary | migrator_argv_prefix_invalid | not_yet_shipped
    message: str = ""

    @property
    def is_hard_fail(self) -> bool:
        # ``missing_binary``, ``migrator_argv_prefix_invalid``, and
        # ``not_yet_shipped`` are NOT hard fails: they describe a
        # surface that is absent or contractually incomplete, and an
        # already-current world should still be allowed to no-op
        # short-circuit when no other work is pending.
        return self.kind in ("parse_error", "non_zero_exit", "timeout")


@dataclass
class ProbeResult:
    """Phase-4 per-surface probe result.

    Phase-4 produces these for every surface in the active filter; T7's
    ``probe_all`` is the sole producer. Consumers:

    * The phase-5 no-op gate reads ``probe_error.is_hard_fail`` to refuse
      short-circuit when any surface had a hard fail.
    * The phase-8 cleanup sweep reads ``state_paths`` to build its
      union-of-surface-state exclusion set.
    * The phase-10 dispatch reads ``migrator_argv_prefix`` (when
      ``compatible``) to construct the migrator argv.

    Fields beyond the legacy three (``name`` / ``version`` /
    ``state_paths`` / ``probe_error``) carry sensible defaults so older
    tests that build ``ProbeResult(name="...", probe_error=...)`` keep
    working.

    Attributes
    ----------
    name : str
        Surface name. Stable identifier; matches the keys in the upgrade
        record's ``surfaces`` mapping.
    present : bool
        True iff the surface's executable was discoverable on PATH at
        probe time (or, for ``hermes``, the configuration-driven
        detect-only check passed). Independent of ``compatible``.
    version : str | None
        Reported version string. None when probe failed before reading
        a version.
    compatible : bool
        True iff the surface's reported version + JSON shape both pass
        validation. The orchestrator dispatches in phase 10 ONLY to
        compatible surfaces.
    state_paths : list[str]
        Absolute paths the surface owns. Phase 8 sweeps refuse to delete
        any path in the union of surface state_paths.
    migrator_argv_prefix : list[str] | None
        Validated argv prefix for the dispatch invocation. ``None``
        when probe failed or surface returned an invalid prefix;
        phase 10 refuses to dispatch when ``None``.
    probe_error : ProbeError | None
        Soft-fail diagnostic; ``None`` on success.
    probe_subprocess : SubprocessOutcome | None
        Captured subprocess outcome from the probe invocation. Carries
        the truncated stdout/stderr triples (per the Pure-JSON
        truncation contract), the timeout / missing-binary flags, and
        the error_kind classification. ``None`` when no subprocess ran
        (e.g. detect-only stubs like Hermes / Codex). Replaces the
        former six forensic dupe fields (``probe_stdout`` /
        ``probe_stdout_bytes`` / ``probe_stdout_truncated`` /
        ``probe_stderr`` / ``probe_stderr_bytes`` /
        ``probe_stderr_truncated``) — all six are reachable as
        attributes on ``probe_subprocess`` (e.g.
        ``result.probe_subprocess.stdout``). Annotation is a STRING
        because ``SubprocessOutcome`` lives in
        ``surfaces/_base.py``, which imports ``ProbeResult`` from
        this module at runtime; a runtime import would cycle.
    """

    name: str
    version: Optional[str] = None
    state_paths: List[str] = field(default_factory=list)
    probe_error: Optional[ProbeError] = None
    present: bool = False
    compatible: bool = False
    migrator_argv_prefix: Optional[List[str]] = None
    probe_subprocess: "Optional[SubprocessOutcome]" = None


@dataclass
class PipelineContext:
    """Shared state threaded through every phase of ``run_pipeline``.

    Phases populate or consume named slots on the context rather than
    returning ad-hoc tuples. T1 lands the contract: phase_snapshot
    populates ``snapshot``, phase_detect populates ``detection``,
    phase_probe_surfaces populates ``probe_results`` +
    ``surface_retry_map``, dry-run mutation phases populate
    ``overlay``, etc. Later tasks (T3-T11) read from the context
    rather than re-reading disk; verify (phase 11) reads through
    ``overlay.read_through(snapshot)`` under dry-run.

    The context is passed in BOTH the args-style positional slot AND
    via a ``pipeline_context=`` kwarg (T1 hands the same object via
    both vectors so future phases can pick whichever is more
    ergonomic). Dataclass field types are deliberately ``Any`` to
    avoid forcing ordering of imports against the FileSnapshot /
    PostStateOverlay symbols at orchestrator-import time.
    """

    args: Any = None
    world_root_resolved: str = ""
    tool_version: str = ""
    started_iso: str = ""
    dry_run: bool = False
    snapshot: Optional[Any] = None
    detection: Optional["DetectionReport"] = None
    probe_results: Optional[List["ProbeResult"]] = None
    surface_retry_map: Dict[str, "SurfaceRetryRecord"] = field(
        default_factory=dict,
    )
    overlay: Optional[Any] = None
    plan_output_path: Optional[str] = None
    # T7 fields populated by phase_probe_surfaces / phase_surface_dispatch.
    surfaces_none: bool = False
    prior_started_at: Optional[str] = None
    stale_retry_dropped: List[Dict[str, Any]] = field(default_factory=list)
    version_mismatch_check_skipped: List[str] = field(default_factory=list)
    dispatch_results: Optional[List[Any]] = None
    # T11 field populated by phase_backup (T5 wiring). When set, the
    # post-run summary path renders a one-line rollback pointer via
    # ``rollback.build_post_upgrade_pointer``. Stays None on no-op
    # short-circuit runs (no backup taken) and on failed backup
    # attempts.
    backup_tarball_path: Optional[str] = None
    # T15 wiring fields. Each phase populates its slot; phase 12
    # (record) folds them into the canonical UpgradeRecord.
    walkthrough_decisions: Optional[Any] = None
    cleanup_report: Optional[Any] = None
    backup_report: Optional[Any] = None
    migration_reports: List[Any] = field(default_factory=list)
    verification_report: Optional[Any] = None
    record_path: Optional[str] = None
    # T6 resume marker (when the orchestrator is driving a fresh run
    # with markers enabled). None means "no marker plumbing this run".
    resume_marker: Optional[Any] = None
    # T6 resume-target step. Populated only on ``--resume`` runs to
    # the value returned by ``resume.validate_resume(...).resume_from``
    # (a :class:`state.Step`). Phases whose Step ordinal is BELOW this
    # value are skipped by the dispatcher; phases at or after it run
    # normally. None on fresh runs (every phase runs).
    resume_from_step: Optional[Any] = None


@dataclass
class SurfaceRetryRecord:
    """Phase-4 prior-record extract: per-surface needs_retry[].

    ``items`` is the list of opaque retry-item dicts (the surface
    defines the shape). ``version_at_retry`` is required when ``items``
    is non-empty.
    """

    name: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    version_at_retry: Optional[str] = None


# ---------------------------------------------------------------------------
# No-op short-circuit (R20)
# ---------------------------------------------------------------------------

def should_short_circuit(
    detection: DetectionReport,
    surface_retry_map: Mapping[str, SurfaceRetryRecord],
    probe_results: Optional[List[ProbeResult]],
    args: Any,
) -> bool:
    """Return True iff phase 5 should skip phases 6-12.

    Predicate (ALL must hold; ``--force-run`` bypasses):
        (a) detection.world_version == TARGET_WORLD_VERSION (STRICT)
        (b) every per-walnut version == TARGET_WORLD_VERSION (STRICT)
        (c) detection.walkthrough_eligible_matches is empty
        (d) surface_retry_map has no items for any surface
        (e) probe_results contains no ``probe_error`` of class hard-fail
            (when probe_results is None -- ``--surfaces=none`` -- this
            clause is vacuously satisfied)

    ``tool_version`` is **never** part of this gate.

    Comparison semantics (R20): clauses (a) and (b) require STRICT
    equality on ``TARGET_WORLD_VERSION``. A future-version world
    (e.g. detection inferring "3.3.0") MUST NOT short-circuit -- the
    operator is running an older tool against a newer world and the
    safe posture is to refuse the no-op gate so a real downstream
    phase surfaces the version skew. A v3.1 walnut inside an
    at-target world MUST NOT short-circuit either, because the
    walkthrough-eligible-matches clause (c) is the only authority
    for "no pending migration" and the loose ``>= 3.1`` floor risked
    masking a stale walnut whose markers happened to under-report.

    Parameters
    ----------
    args:
        argparse Namespace; ``args.force_run`` (bool) is consulted.
        Other attributes are ignored at the gate -- side effects (record
        write etc.) happen in the orchestrator.
    """
    if getattr(args, "force_run", False):
        return False

    target = _normalize_version(TARGET_WORLD_VERSION)

    try:
        world_v = _normalize_version(detection.world_version)
    except (ValueError, TypeError):
        return False
    if world_v != target:
        return False

    for v in detection.per_walnut_versions.values():
        try:
            if _normalize_version(v) != target:
                return False
        except (ValueError, TypeError):
            return False

    if detection.walkthrough_eligible_matches:
        return False

    for record in surface_retry_map.values():
        if record.items:
            return False

    if probe_results is not None:
        for p in probe_results:
            if p.probe_error is not None and p.probe_error.is_hard_fail:
                return False

    return True


# ---------------------------------------------------------------------------
# No-op record builder
# ---------------------------------------------------------------------------

def build_noop_record(
    *,
    started_at: str,
    finished_at: str,
    tool_version_at_run: str,
    detection: DetectionReport,
    reason: str = "world already at target version",
) -> Dict[str, Any]:
    """Construct a schema-conforming UpgradeRecord for a no-op short-circuit.

    Every required top-level field is populated; collections that have
    no entries on a no-op are emitted as empty containers (``[]`` or
    ``{}``) so T7's loader never sees a missing key.
    """
    return {
        "schema_version": "1",
        "started_at": started_at,
        "finished_at": finished_at,
        "tool_version_at_run": tool_version_at_run,
        "world_version": detection.world_version,
        "per_walnut_versions": dict(detection.per_walnut_versions),
        "operations": [],
        "reason": reason,
        "surfaces": {},
        "stale_retry_dropped": [],
        "walkthrough_skipped": [],
    }


def write_noop_record_to_world(
    world_root_resolved: str, record: Mapping[str, Any], iso_ts: str,
) -> str:
    """Atomically write a no-op record under ``.alive/upgrades/<ts>.yaml``.

    Returns the absolute path of the written record. Caller owns
    timestamp formatting; ``iso_ts`` MUST already be filename-safe
    (the canonical pattern is ``YYYY-MM-DDTHH-MM-SS``, colons replaced
    with hyphens).

    Collision-resolution: when a canonical record already exists at
    the requested ``iso_ts`` (back-to-back runs within the same
    second), the record lands at ``<ts>-2.yaml`` / ``<ts>-3.yaml`` /
    ... so the prior run's record is preserved.
    """
    upgrades_dir = os.path.join(
        world_root_resolved, ".alive", "upgrades"
    )
    target = _unique_record_path(upgrades_dir, iso_ts)
    write_noop_record(target, dict(record))
    return target


# ---------------------------------------------------------------------------
# Phase stub helper (kept for back-compat; existing phase functions are
# no longer stubs but tests may still construct stubs via this helper).
# ---------------------------------------------------------------------------

def _stub(phase_name: str):
    """Return a phase function that raises ``PhaseNotImplemented``."""

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise PhaseNotImplemented(
            "phase {} ({}) is a fail-loud stub; later task wires it up."
            .format(PHASE_NUMBERS[phase_name], phase_name)
        )

    _raise.__name__ = "phase_{}".format(phase_name)
    return _raise


# ---------------------------------------------------------------------------
# Pipeline driver (phases 2..13)
# ---------------------------------------------------------------------------

# Module-level POST_GATE_ORDER. Third element renamed from ``should_run``
# (runtime-resolved expression) to ``should_run_in_dry_run`` (static bool):
# ``True`` means the phase runs under ``--dry-run`` too, ``False`` means
# the phase is skipped under ``--dry-run``. The dispatcher's use site
# checks ``if dry_run and not should_run_in_dry_run: continue`` which
# preserves the prior runtime semantics. fn-20's planned insertion of
# ``("canonical_ids_migrate", "phase_canonical_ids_migrate", True)``
# means the canonical-ids phase runs under both real and dry-run, which
# is exactly fn-20's documented intent.
POST_GATE_ORDER: Tuple[Tuple[str, str, bool], ...] = (
    # (phase_name, attr_name, should_run_in_dry_run)
    ("backup",             "phase_backup",             False),  # 6
    ("walkthrough_decide", "phase_walkthrough_decide", True),   # 7
    # Phases 8 + 9 run under dry-run too: their phase fns now read
    # ``args.dry_run`` and pass it to ``cleanup()`` and the per-step
    # migration runners, which already short-circuit disk writes
    # under ``dry_run=True`` while populating the report objects
    # the plan-output writer consumes (see _write_dry_run_plan).
    # Without this, a dry-run plan ships ``operations: []`` and the
    # operator has nothing to inspect.
    ("plugin_cleanup",     "phase_plugin_cleanup",     True),   # 8
    ("plugin_migrate",     "phase_plugin_migrate",     True),   # 9
    ("surface_dispatch",   "phase_surface_dispatch",   False),  # 10
    ("verify",             "phase_verify",             True),   # 11
    ("record",             "phase_record",             False),  # 12
)


@dataclass
class PipelineResult:
    """Aggregate outcome of ``run_pipeline``.

    Returned to the CLI so it can build the right exit envelope.

    Attributes
    ----------
    phase_reached : str
        Name of the last phase that ran (or attempted to run).
    noop_short_circuit : bool
        True iff phase 5's gate fired and the run wrote a no-op record.
    noop_record_path : str | None
        Absolute path of the no-op record when one was written.
    error_code : str | None
        ``phase_not_implemented:<phase>`` when a stub phase was hit;
        ``None`` on a successful no-op short-circuit run.
    error : str | None
        Human-readable explanation paired with ``error_code``.
    """

    phase_reached: str
    noop_short_circuit: bool = False
    noop_record_path: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    # T11: when phase_backup wrote a pre-upgrade tarball, this carries
    # the absolute path so the CLI's success envelope can render the
    # one-line rollback pointer (acceptance criterion 5).
    backup_tarball_path: Optional[str] = None


def run_pipeline(
    args: Any,
    *,
    world_root_resolved: str,
    tool_version: str,
    started_iso: str,
) -> PipelineResult:
    """Drive phases 2..13 against a preflighted, locked world.

    Phases skipped under ``--dry-run``:
        * 6 (backup), 8 (cleanup), 9 (migrate), 10 (surface dispatch),
          12 final-record write.
    Phases run under ``--dry-run``:
        * 1 (already done by caller), 2 (snapshot), 3 (detect),
          4 (probe), 5 (decision-only -- logs but does NOT write the
          no-op record), 7 (decisions only), 11 (overlay verify),
          12 substitute (writes ``--plan-output``), 13 (release).

    The caller owns release of the lock; this function only runs the
    in-lock phases.
    """
    dry_run = bool(getattr(args, "dry_run", False))
    plan_output = getattr(args, "plan_output", None)

    # Build the shared pipeline context. Phases populate / consume
    # named slots on this object instead of returning ad-hoc tuples.
    ctx = PipelineContext(
        args=args,
        world_root_resolved=world_root_resolved,
        tool_version=tool_version,
        started_iso=started_iso,
        dry_run=dry_run,
        plan_output_path=plan_output,
    )

    # T6 resume marker: under ``--resume`` load + validate the
    # existing marker so the pipeline can pick up where the prior
    # run halted; otherwise create a fresh marker with PREFLIGHT
    # already completed (the CLI ran the preflight chain before this
    # pipeline started). Markers are world-side artefacts -- under
    # ``--dry-run`` we skip the write to honour the dry-run "no
    # destructive marker writes" invariant. With no marker on the
    # context, the per-phase ``_marker_*`` helpers stay quiet
    # (a None marker short-circuits every transition write).
    if not dry_run:
        marker, resume_from_step = _initialise_resume_marker(
            args=args,
            world_root_resolved=world_root_resolved,
            tool_version=tool_version,
            started_iso=started_iso,
        )
        ctx.resume_marker = marker
        ctx.resume_from_step = resume_from_step

        # on a resumed run, ctx.started_iso
        # was populated from the NEW lock-meta sidecar, but every
        # filename-safe-ISO-keyed artefact written by the original
        # run uses the ORIGINAL marker's started_iso. Realign
        # ctx.started_iso to the marker so dispatch sidecar lookup,
        # the canonical record's started_at, and any future
        # iso-keyed helper see the same key the original run wrote
        # against. This is harmless on fresh runs (the resume
        # validator returned the freshly-built marker, so
        # marker.started_iso == ctx.started_iso == lock's started_iso).
        if marker is not None:
            marker_iso = getattr(marker, "started_iso", None)
            if isinstance(marker_iso, str) and marker_iso:
                ctx.started_iso = marker_iso

        # when resuming past phase_backup, the
        # context's ``backup_tarball_path`` is empty because phase_backup
        # is a destructive phase we deliberately skip on resume.
        # ``phase_record`` and the post-upgrade pointer need it, so
        # hydrate from disk by picking the most recent
        # ``pre-upgrade-<ts>.tar.gz`` under ``.alive/upgrades/``.
        #
        # similarly, when resuming past
        # phase_surface_dispatch, ``ctx.dispatch_results`` is empty
        # but the prior run already invoked the surfaces; hydrate
        # from the dispatch sidecar so the canonical record reflects
        # the actual dispatch outcomes rather than an empty section.
        try:
            from .state import Step  # noqa: PLC0415
            if (
                resume_from_step is not None
                and resume_from_step.value > Step.BACKUP.value
            ):
                _hydrate_backup_tarball_path_on_resume(
                    ctx, world_root_resolved,
                )
            if (
                resume_from_step is not None
                and resume_from_step.value > Step.SURFACE_DISPATCH.value
            ):
                _hydrate_dispatch_results_on_resume(
                    ctx, world_root_resolved,
                )
        except ImportError:
            pass

    # Look every phase callable up via the module namespace so
    # monkeypatched stubs (and tests that fake the phases) land
    # correctly. Resolving via ``getattr(self_mod, ...)`` is necessary
    # because Python's bytecode binds names at function-definition
    # time -- bare ``phase_snapshot()`` would always call the original
    # imported symbol even after a test reassigns
    # ``orchestrator.phase_snapshot``. With the per-phase split, the
    # symbols here are re-exports from ``phases/<name>.py``; the
    # ``getattr`` lookup resolves against the orchestrator namespace
    # (where tests reassign), so reassignment still works.
    import sys as _sys  # noqa: PLC0415
    self_mod = _sys.modules[__name__]

    # Phase 2: snapshot. Populates ctx.snapshot; the legacy return
    # value is also accepted for fakes that don't use the context.
    try:
        snap_result = getattr(self_mod, "phase_snapshot")(
            args, world_root_resolved=world_root_resolved,
            pipeline_context=ctx,
        )
        if snap_result is not None and ctx.snapshot is None:
            ctx.snapshot = snap_result
    except PhaseNotImplemented as exc:
        return PipelineResult(
            phase_reached="snapshot",
            error_code="phase_not_implemented:snapshot",
            error=str(exc),
        )

    # Phase 3: detect. Populates ctx.detection; legacy return
    # value also accepted.
    try:
        detection = getattr(self_mod, "phase_detect")(
            args, world_root_resolved=world_root_resolved,
            pipeline_context=ctx,
        )
        if detection is not None and ctx.detection is None:
            ctx.detection = detection
    except PhaseNotImplemented as exc:
        return PipelineResult(
            phase_reached="detect",
            error_code="phase_not_implemented:detect",
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return _detect_refusal_to_result(exc)
    if ctx.detection is None:
        # The phase is supposed to populate this. Treat absence as a
        # contract violation surfaceable as a clear stub-error.
        return PipelineResult(
            phase_reached="detect",
            error_code="phase_not_implemented:detect",
            error="phase_detect returned no DetectionReport",
        )
    detection = ctx.detection

    # T6 fingerprint capture: a fresh marker is created BEFORE
    # detection runs (the marker write is part of preflight), so
    # ``world_fingerprint_at_start`` lands empty. Now that phase 3
    # has produced ``detection.all_signals_raw`` we update the
    # marker in-place so a subsequent ``--resume`` validator can
    # diff fresh-run-then vs fresh-run-now and surface
    # ``resume_world_diverged`` correctly. We only re-stamp the
    # fingerprint when it's currently empty -- on ``--resume``
    # runs the original marker's fingerprint is the contract and
    # must not be overwritten.
    if not dry_run:
        _capture_world_fingerprint(ctx, detection)

    # Phase 4: probe + prior-record load. Populates ctx.probe_results
    # + ctx.surface_retry_map; legacy 2-tuple return shape is also
    # accepted for backward compat.
    try:
        probe_pair = getattr(self_mod, "phase_probe_surfaces")(
            args, world_root_resolved=world_root_resolved,
            pipeline_context=ctx,
        )
    except PhaseNotImplemented as exc:
        return PipelineResult(
            phase_reached="probe_surfaces",
            error_code="phase_not_implemented:probe_surfaces",
            error=str(exc),
        )
    # If the phase returned a (probe_results, surface_retry_map) tuple,
    # honor it for back-compat with T1 fakes.
    if probe_pair is not None and ctx.probe_results is None:
        try:
            probe_results_local, surface_retry_map_local = probe_pair
            ctx.probe_results = probe_results_local
            ctx.surface_retry_map = surface_retry_map_local
        except (TypeError, ValueError):
            pass
    probe_results = ctx.probe_results
    surface_retry_map = ctx.surface_retry_map

    # Phase 5: no-op short-circuit (R20).
    #
    # Resume-aware: the gate is
    # skipped when the resume marker places the dispatcher AFTER
    # the gate AND at least one DESTRUCTIVE post-gate step is
    # already in ``completed_ops``. The destructive-step requirement
    # closes a narrow crash window: a prior run could have completed
    # the no-op gate marker (writing an empty no-op record) and
    # crashed BEFORE the inline release marker, leaving
    # ``resume_from_step=BACKUP`` even though no migrations ran.
    # In that case we WANT to re-run the gate -- the world is at
    # target with nothing pending, so no-op should fire again.
    # Conversely, if BACKUP / PLUGIN_CLEANUP / PLUGIN_MIGRATE was
    # marked complete in the prior run, the world WAS mutated and
    # detection now sees a target-version world spuriously; we must
    # bypass the gate to resume the destructive chain.
    try:
        from .state import Step  # noqa: PLC0415
        _resume_from = getattr(ctx, "resume_from_step", None)
        _gate_after = (
            _resume_from is not None
            and getattr(_resume_from, "value", -1)
            > Step.NOOP_SHORT_CIRCUIT.value
        )
        # Did a destructive post-gate step actually complete?
        _DESTRUCTIVE_POST_GATE = (
            "BACKUP", "PLUGIN_CLEANUP", "PLUGIN_MIGRATE",
            "SURFACE_DISPATCH",
        )
        _completed_ops: List[str] = []
        marker = getattr(ctx, "resume_marker", None)
        if marker is not None:
            try:
                _completed_ops = list(getattr(marker, "completed_ops", ()))
            except (AttributeError, TypeError):
                _completed_ops = []
        _destructive_done = any(
            op in _DESTRUCTIVE_POST_GATE for op in _completed_ops
        )
        _gate_already_passed = bool(_gate_after and _destructive_done)
    except ImportError:
        _gate_already_passed = False

    _marker_running(ctx, "noop_short_circuit")
    if not _gate_already_passed and should_short_circuit(
        detection, surface_retry_map, probe_results, args,
    ):
        if dry_run:
            # Decision-only under --dry-run; do NOT write the no-op
            # record. BUT we MUST emit the --plan-output artifact when
            # the operator requested one -- the dry-run invariant
            # promises the plan file is the operator's consumable
            # output, and a silent exit-0 with no file is an honest
            # contract break.
            #
            # prefer ctx.started_iso so the
            # plan file's timestamp matches the resume marker's
            # started_iso (realigned in run_pipeline's resume init).
            # On fresh runs ctx.started_iso == started_iso so this is
            # a no-op; on resume it ensures the artefact is keyed to
            # the original run.
            ctx_started_iso = (
                ctx.started_iso if ctx is not None and ctx.started_iso
                else started_iso
            )
            plan_output = getattr(args, "plan_output", None)
            if plan_output:
                try:
                    _write_dry_run_plan(
                        plan_output,
                        world_root_resolved=world_root_resolved,
                        detection=detection,
                        tool_version=tool_version,
                        started_iso=ctx_started_iso,
                        decision="noop_short_circuit",
                    )
                except OSError as exc:
                    _marker_failed(
                        ctx, "noop_short_circuit", str(exc),
                    )
                    raise PhaseWriteError(
                        "noop_short_circuit", exc,
                    ) from exc
            _marker_completed(ctx, "noop_short_circuit")
            # mark RELEASE on the resume marker
            # before returning so a successful no-op leaves a fully-
            # complete marker. Without this, a later ``--resume``
            # would treat the marker as "still has work" (resume_from
            # = BACKUP), bypass the no-op gate via the round-4 M2
            # guard, and run post-gate phases on an already-current
            # world.
            _marker_running(ctx, "release")
            _marker_completed(ctx, "release")
            # R20 idempotency: a no-op short-circuit reaches "release"
            # semantically -- the canonical no-op record is the
            # forensic; the in-flight ``-resume.yaml`` /
            # ``-runstate.yaml`` sidecars are stale. The full-run
            # release path cleans these; the no-op path must do the
            # same so a second no-op run against the same world
            # doesn't mutate the prior run's marker (idempotency
            # property tests would catch the leak).
            _cleanup_release_sidecars(world_root_resolved, ctx, started_iso)
            return PipelineResult(
                phase_reached="noop_short_circuit",
                noop_short_circuit=True,
                error_code=None,
                error=None,
            )
        # Real run: write the no-op record atomically. Filesystem
        # write failures (ENOSPC, EROFS, EACCES, ...) are wrapped in
        # PhaseWriteError so the CLI keeps ``phase_reached`` in the
        # documented PHASE_NAMES namespace ("noop_short_circuit") and
        # still translates to the exit-code-4 envelope.
        #
        # started_at uses ctx.started_iso so
        # the no-op record on a resumed run is keyed to the original
        # run's started_iso (realigned in run_pipeline's resume
        # init), not the new lock's timestamp.
        finished_iso = iso_now()
        ctx_started_iso = (
            ctx.started_iso if ctx is not None and ctx.started_iso
            else started_iso
        )
        record = build_noop_record(
            started_at=ctx_started_iso,
            finished_at=finished_iso,
            tool_version_at_run=tool_version,
            detection=detection,
        )
        # Filename-safe ISO: replace ":" with "-".
        ts = finished_iso.replace(":", "-").replace("Z", "")
        try:
            record_path = write_noop_record_to_world(
                world_root_resolved, record, ts,
            )
        except OSError as exc:
            _marker_failed(ctx, "noop_short_circuit", str(exc))
            raise PhaseWriteError("noop_short_circuit", exc) from exc
        _marker_completed(ctx, "noop_short_circuit")
        # mark RELEASE so a successful no-op
        # leaves a fully-complete marker (see comment on the dry-run
        # branch above for the resume-correctness rationale).
        _marker_running(ctx, "release")
        _marker_completed(ctx, "release")
        # R20 idempotency: mirror the full-run release path -- clean
        # the in-flight resume marker + runstate sidecar so a
        # subsequent no-op run on the same world doesn't mutate the
        # prior run's marker file (idempotency property test catches
        # the leak).
        _cleanup_release_sidecars(world_root_resolved, ctx, started_iso)
        return PipelineResult(
            phase_reached="noop_short_circuit",
            noop_short_circuit=True,
            noop_record_path=record_path,
        )
    # Gate did not fire -> we passed through phase 5 cleanly.
    _marker_completed(ctx, "noop_short_circuit")

    # Past the gate: dispatch each remaining phase. When a phase raises
    # ``PhaseNotImplemented`` we surface the offending phase name as
    # ``phase_reached`` so the operator (and tests) know exactly which
    # task needs to ship next.
    #
    # ``--dry-run`` skips writes-only phases (those with
    # ``should_run_in_dry_run=False`` in POST_GATE_ORDER): backup,
    # surface dispatch, final record. Phases 7 (decisions), 8/9
    # (cleanup + migrate, dry-run-aware), and 11 (verify via overlay)
    # still run.
    for phase_name, attr_name, should_run_in_dry_run in POST_GATE_ORDER:
        if dry_run and not should_run_in_dry_run:
            continue
        # ``--resume``: skip phases the marker already records as
        # completed; the orchestrator picks up at the first
        # not-yet-done step. Pre-gate phases (snapshot/detect/probe)
        # and the gate (noop_short_circuit) always replay --
        # _phase_already_completed enforces that exception.
        if _phase_already_completed(ctx, phase_name):
            continue
        phase_fn = getattr(self_mod, attr_name)
        try:
            phase_fn(
                args, world_root_resolved=world_root_resolved,
                pipeline_context=ctx,
            )
        except PhaseNotImplemented as exc:
            return PipelineResult(
                phase_reached=phase_name,
                error_code="phase_not_implemented:{}".format(phase_name),
                error=str(exc),
            )
        except PhaseWriteError:
            # Bubble up so the CLI maps to the exit-4 permission envelope.
            raise
        except Exception as exc:  # noqa: BLE001
            # Per-phase exception -> structured PipelineResult with a
            # phase-tagged error_code. The CLI keeps phase_reached in
            # the PHASE_NAMES namespace so receivers grepping on
            # error_code stay sharp.
            error_code = _phase_exception_to_code(phase_name, exc)
            return PipelineResult(
                phase_reached=phase_name,
                error_code=error_code,
                error=str(exc),
                backup_tarball_path=ctx.backup_tarball_path,
            )

    # Phase 12 substitute under --dry-run: write the plan-output file
    # in lieu of the real .alive/upgrades/<ts>.yaml record. The
    # invariant guard at handle()-entry already enforced that
    # plan_output is set when --dry-run was passed without --json,
    # so an absent plan_output here means --json was supplied and
    # the operator's consumable output is the JSON envelope itself.
    if dry_run and plan_output:
        try:
            _write_dry_run_plan(
                plan_output,
                world_root_resolved=world_root_resolved,
                detection=detection,
                tool_version=tool_version,
                started_iso=started_iso,
                decision="full_run",
                backup_tarball_path=ctx.backup_tarball_path,
                cleanup_report=getattr(ctx, "cleanup_report", None),
                migration_reports=getattr(ctx, "migration_reports", None) or [],
            )
        except OSError as exc:
            raise PhaseWriteError("record", exc) from exc

    # Phase 13: release. The lock release itself is handled by the
    # caller (`handle()` in this module), but we mark the transition
    # here so the resume marker reflects a fully-complete pipeline
    # before the lock-side teardown runs.
    _marker_running(ctx, "release")
    _marker_completed(ctx, "release")
    # On a successful release the canonical record at
    # ``.alive/upgrades/<ts>.yaml`` IS the forensic; the in-flight
    # ``-resume.yaml`` and ``-runstate.yaml`` sidecars are stale
    # artefacts that confuse downstream consumers (they share the
    # upgrades dir + .yaml extension but carry different schemas, so
    # a sloppy "*.yaml" listing picks the latest sidecar instead of
    # the canonical record).
    _cleanup_release_sidecars(world_root_resolved, ctx, started_iso)
    return PipelineResult(
        phase_reached="release",
        backup_tarball_path=ctx.backup_tarball_path,
    )


def _cleanup_release_sidecars(
    world_root_resolved: str,
    ctx: Optional["PipelineContext"],
    started_iso: str,
) -> None:
    """Remove the resume marker + runstate sidecar for the just-
    completed run.

    A successful run leaves the canonical record at
    ``.alive/upgrades/<ts>.yaml`` as the durable forensic. The
    ``-resume.yaml`` marker is meant for in-flight tracking (lets a
    crashed run resume from where it stopped); the ``-runstate.yaml``
    sidecar is the migrations runner's incremental op log. Once the
    canonical record lands, both sidecars are stale.

    Failures are swallowed -- sidecar cleanup is best-effort.
    """
    upgrades_dir = os.path.join(
        world_root_resolved, ".alive", "upgrades"
    )
    if not os.path.isdir(upgrades_dir):
        return
    # Sidecars use the started_iso's filename-safe form. The resume
    # marker uses the form `<filename-safe-iso>-resume.yaml`; the
    # runstate sidecar uses `<filename-safe-iso>-runstate.yaml`. The
    # marker source-of-truth is `state.MARKER_SUFFIX` /
    # `migrations._record.RUNSTATE_SUFFIX` -- mirror those constants
    # via a lazy import so a broken state/_record module doesn't
    # break release cleanup.
    suffixes: List[str] = []
    try:
        from .state import MARKER_SUFFIX  # noqa: PLC0415
        suffixes.append(MARKER_SUFFIX)
    except ImportError:
        suffixes.append("-resume.yaml")
    try:
        from .migrations._record import RUNSTATE_SUFFIX  # noqa: PLC0415
        suffixes.append(RUNSTATE_SUFFIX)
    except ImportError:
        suffixes.append("-runstate.yaml")
    # The sidecars use the started_iso (not finished_iso) form. The
    # marker writes use `<filename-safe-started-iso>-resume.yaml`,
    # so we replicate the helper's filename-safe transformation.
    if not started_iso:
        return
    fname_iso = started_iso.replace(":", "-")
    # The marker keeps the trailing Z (per state.MARKER_SUFFIX
    # examples); the runstate likewise. We do NOT strip the Z so the
    # filenames match the writers' output. If a sibling lands without
    # the Z (older write paths) the listdir fallback below catches it.
    candidates = set()
    for suffix in suffixes:
        candidates.add(os.path.join(
            upgrades_dir, "{}{}".format(fname_iso, suffix),
        ))
    # Also sweep any sibling matching the started_iso prefix + a
    # known sidecar suffix, in case the writer used a slightly
    # different filename-safe form. This is best-effort; the
    # canonical-pattern matcher in surfaces/ is the authority for
    # which records reach the no-op gate carry-forward path.
    try:
        for entry in os.listdir(upgrades_dir):
            if entry.startswith(fname_iso) and any(
                entry.endswith(s) for s in suffixes
            ):
                candidates.add(os.path.join(upgrades_dir, entry))
    except OSError:
        pass
    for path in candidates:
        try:
            os.unlink(path)
        except OSError:
            # best-effort
            pass


def _write_dry_run_plan(
    plan_output: str,
    *,
    world_root_resolved: str,
    detection: "DetectionReport",
    tool_version: str,
    started_iso: str,
    decision: str,
    backup_tarball_path: Optional[str] = None,
    cleanup_report: Any = None,
    migration_reports: Optional[List[Any]] = None,
) -> None:
    """Write a deterministic, human-readable dry-run plan file.

    Two shapes:
      * ``noop_short_circuit``: single decision line + detection
        summary. ``operations`` is intentionally empty -- nothing to
        do. ``cleanup_report`` and ``migration_reports`` are ignored.
      * ``full_run``: enumerates the planned operations the orchestrator
        WOULD apply (cleanup deletes/skips + per-walnut migration ops).
        ``cleanup_report`` and ``migration_reports`` are sourced from
        the dry-run-mode invocations of phases 8 + 9; under dry-run
        those phases call their runners with ``dry_run=True`` and
        populate the report objects without mutating disk.

    Atomic write so the plan file is always either complete or absent
    (no partial-plan ambiguity for the operator).

    When *backup_tarball_path* is set (a phase-6 backup landed via
    a non-stub ``phase_backup``), the plan file appends a one-line
    rollback pointer (T11 acceptance criterion 5).
    """
    from _atomic_io import atomic_write_text  # noqa: PLC0415
    from .rollback import build_post_upgrade_pointer  # noqa: PLC0415
    finished_iso = iso_now()
    operations = _enumerate_planned_operations(
        cleanup_report=cleanup_report,
        migration_reports=migration_reports,
    ) if decision != "noop_short_circuit" else []
    lines = [
        "# alive system-upgrade --dry-run plan",
        "# Generated: {}".format(finished_iso),
        "# Started:   {}".format(started_iso),
        "world_root: {}".format(world_root_resolved),
        "tool_version_at_run: {}".format(tool_version),
        "world_version: {}".format(detection.world_version),
        "decision: {}".format(decision),
    ]
    if operations:
        lines.append("operations:")
        for op in operations:
            # Emit each op as a YAML mapping. Values are stringified
            # defensively (the underlying op dataclasses already produce
            # str fields, but None slots are normalized to the empty
            # string for plan-file lexer safety).
            lines.append("  - op_type: {}".format(op["op_type"]))
            lines.append("    status: {}".format(op["status"]))
            for key in ("walnut_root", "path", "to_path", "detail"):
                if op.get(key):
                    lines.append("    {}: {}".format(key, op[key]))
    else:
        lines.append("operations: []")
    lines.append("reason: {}".format(
        "world already at target version"
        if decision == "noop_short_circuit"
        else "{} planned operation(s) populated from dry-run cleanup + migration phases"
            .format(len(operations))
    ))
    if detection.per_walnut_versions:
        lines.append("per_walnut_versions:")
        for walnut, version in sorted(detection.per_walnut_versions.items()):
            lines.append("  {}: {}".format(walnut, version))
    else:
        lines.append("per_walnut_versions: {}")
    if backup_tarball_path:
        # Appended as a hash-comment so plan-file readers that lex
        # YAML key-value lines don't misparse it; the format helper
        # produces a single-line string.
        lines.append("# {}".format(
            build_post_upgrade_pointer(backup_tarball_path),
        ))
    text = "\n".join(lines) + "\n"
    atomic_write_text(plan_output, text, mode=0o644)


def _enumerate_planned_operations(
    *,
    cleanup_report: Any = None,
    migration_reports: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    """Aggregate planned ops from dry-run cleanup + migration reports.

    Thin wrapper over ``_phase_helpers.enumerate_operations`` (no
    backup-tarball op -- planning doesn't take a tarball) so the
    plan file and the canonical full-run record agree on the
    operation vocabulary.
    """
    return enumerate_operations(
        cleanup_report=cleanup_report,
        migration_reports=migration_reports,
    )


def _detect_refusal_to_result(exc: Exception) -> "PipelineResult":
    """Translate a phase-3 detect-time exception into a structured result.

    The version_detect module surfaces ``DetectionRefusal`` with a
    stable ``code`` (``no_signals`` / ``assume_empty_world_invalid``).
    Other exceptions get a generic ``detect_error`` code so the CLI
    envelope still surfaces something parseable.
    """
    code = getattr(exc, "code", None)
    if code:
        return PipelineResult(
            phase_reached="detect",
            error_code="detect_refusal:{}".format(code),
            error=str(exc),
        )
    return PipelineResult(
        phase_reached="detect",
        error_code="detect_error",
        error="{}: {}".format(type(exc).__name__, exc),
    )


def _initialise_resume_marker(
    *,
    args: Any,
    world_root_resolved: str,
    tool_version: str,
    started_iso: str,
) -> Tuple[Optional[Any], Optional[Any]]:
    """Construct or load the ResumeMarker for this run.

    On ``--resume`` (``args.resume`` truthy): load + validate the
    existing marker via ``resume.validate_resume`` so the pipeline
    inherits prior ``completed_ops`` and the operator's ``--force`` /
    ``--resume-staleness`` flags are honoured. On a clean run: build
    a fresh marker, mark PREFLIGHT completed (the CLI ran preflight
    before invoking this pipeline), and persist it.

    Returns ``(marker, resume_from_step)`` where:

    * ``marker`` is the live :class:`ResumeMarker` ready for the
      per-phase ``_marker_*`` helpers to advance, or ``None`` when
      the resume layer is unavailable (broken module / fresh-run
      write failure -- the pipeline still runs but loses the
      resume-marker UX for this run).
    * ``resume_from_step`` is the :class:`state.Step` the dispatcher
      should resume FROM (steps strictly below this ordinal are
      skipped). ``None`` on fresh runs (every phase runs) and on
      validator fallback to a fresh marker.
    """
    try:
        from . import resume as _resume  # noqa: PLC0415
        from .state import Step  # noqa: PLC0415  # noqa: F401
    except ImportError:
        return (None, None)
    if getattr(args, "resume", False):
        # ``--resume`` path. We delegate to the validator chain so
        # the operator gets the documented refusal codes
        # (resume_marker_missing / _unreadable / _stale /
        # _world_diverged / _tool_version_skew) rather than a silent
        # fallback to a fresh marker. ``ResumeRefusal`` propagates
        # to the caller -- the CLI envelope translates it into the
        # exit-code-1 refusal envelope.
        try:
            plan = _resume.validate_resume(
                world_root_resolved,
                force=bool(getattr(args, "force", False)),
                staleness_hours=int(
                    getattr(args, "resume_staleness", _resume.DEFAULT_STALENESS_HOURS),
                ),
            )
        except _resume.ResumeRefusal:
            raise
        except Exception:  # noqa: BLE001
            # Validator unexpectedly broken: fall through to a fresh
            # marker rather than blocking the pipeline.
            return (
                _build_fresh_marker(
                    world_root_resolved=world_root_resolved,
                    tool_version=tool_version,
                    started_iso=started_iso,
                ),
                None,
            )
        # Reuse the marker the validator parsed. The pipeline
        # advances completed_ops via the per-phase _marker_*
        # helpers; the validator already confirmed plugin-version
        # parity + world fingerprint divergence (or --force bypass).
        return (plan.marker, plan.resume_from)
    return (
        _build_fresh_marker(
            world_root_resolved=world_root_resolved,
            tool_version=tool_version,
            started_iso=started_iso,
        ),
        None,
    )


def _build_fresh_marker(
    *,
    world_root_resolved: str,
    tool_version: str,
    started_iso: str,
) -> Optional[Any]:
    """Construct a fresh ResumeMarker + persist the PREFLIGHT-completed
    transition. Returns the marker on success, ``None`` on resume-
    layer failure."""
    try:
        from . import resume as _resume  # noqa: PLC0415
        from .state import Step  # noqa: PLC0415
        planned_ops = [s.name for s in Step]
        marker = _resume.new_marker(
            started_iso=started_iso,
            tool_version_at_run=tool_version,
            world_fingerprint_at_start={},
            planned_ops=planned_ops,
        )
        marker = _resume.mark_step_completed(
            marker, Step.PREFLIGHT,
            halted_iso=iso_now(),
        )
        try:
            _resume.write_marker(world_root_resolved, marker)
        except OSError:
            # Best-effort; subsequent _marker_* calls will retry the
            # write at the next phase boundary.
            pass
        return marker
    except Exception:  # noqa: BLE001
        return None


def _phase_exception_to_code(phase_name: str, exc: Exception) -> str:
    """Map an unexpected per-phase exception to a stable error_code.

    Recognised exception types get a phase-tagged sub-class for
    grepability (``walkthrough_abort``, ``migration_failed``,
    ``cleanup_error`` etc.). Everything else falls through to
    ``phase_error:<phase>`` so the CLI envelope still carries a
    structured token.
    """
    name = type(exc).__name__
    if name == "WalkthroughAbort":
        return "walkthrough_abort"
    if name == "DetectionRefusal":
        return "detect_refusal:{}".format(getattr(exc, "code", "unknown"))
    return "phase_error:{}".format(phase_name)
