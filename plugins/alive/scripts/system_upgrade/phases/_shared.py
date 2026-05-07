"""Shared helpers + exception types for ``system_upgrade.phases.*`` modules.

This module hosts symbols that every phase function needs but that
cannot live alongside the phase bodies in ``phases/<phase>.py`` without
creating a cycle. ``orchestrator.py`` re-exports each public symbol so
fn-20's documented import surface (``from .orchestrator import
_resume_step_for_phase`` / ``_phase_already_completed`` / etc.) keeps
resolving after the per-phase split.

Hoisted constants:

* ``REPLAY_PHASES`` — phase-name tuple for ``_phase_already_completed``.
  Module-level so ``from .orchestrator import REPLAY_PHASES`` resolves.
  Also explicitly attached as ``_phase_already_completed.REPLAY_PHASES``
  after the function definition (function objects do NOT proxy module
  globals via attribute lookup).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from _common import iso_now



# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------

class PhaseNotImplemented(NotImplementedError):
    """Raised by a fail-loud phase stub to surface "T1 didn't fill this in".

    Each later task replaces its phase stub; until then any premature
    invocation is a hard error.
    """


class PhaseWriteError(Exception):
    """Raised when a phase's filesystem write fails (PermissionError /
    OSError class), carrying the phase name so the CLI envelope can
    keep ``phase_reached`` in the documented namespace.

    The CLI catches this and emits ``exit_code=4`` /
    ``error_code="permission:<phase>_write"``.

    Attributes
    ----------
    phase : str
        Name of the phase the write was running for. MUST be one of
        the documented PHASE_NAMES values so callers keying on
        ``phase_reached`` keep working.
    cause : OSError
        The original ``OSError`` / ``PermissionError`` instance.
    """

    def __init__(self, phase: str, cause: OSError) -> None:
        super().__init__("{} write failed: {}".format(phase, cause))
        self.phase = phase
        self.cause = cause


# ---------------------------------------------------------------------------
# Record-path helper
# ---------------------------------------------------------------------------

def _unique_record_path(upgrades_dir: str, iso_ts: str) -> str:
    """Return a record path under *upgrades_dir* that does not collide
    with an existing canonical record at the same second.

    Two consecutive runs within the same second (e.g. an automated
    test that immediately re-runs a finished upgrade) would otherwise
    overwrite each other's canonical record. The collision-resolution
    appends ``-2``, ``-3``, ... before the ``.yaml`` extension; the
    canonical filename pattern's primary form (no suffix) is always
    preferred when free.

    Idempotency property runs hit the 1-second resolution boundary; the
    loader at ``surfaces.load_prior_final_record`` filename-sorts so any
    suffixed sibling still resolves chronologically when alphabetic
    order matches insertion order.
    """
    base = os.path.join(upgrades_dir, "{}.yaml".format(iso_ts))
    if not os.path.exists(base):
        return base
    seq = 2
    while True:
        candidate = os.path.join(
            upgrades_dir, "{}-{}.yaml".format(iso_ts, seq),
        )
        if not os.path.exists(candidate):
            return candidate
        seq += 1


# ---------------------------------------------------------------------------
# Resume-step mapping
# ---------------------------------------------------------------------------

def _resume_step_for_phase(phase_name: str) -> Any:
    """Map a PHASE_NAMES entry to its ``state.Step`` enum member.

    Local import keeps the resume module optional at orchestrator-load
    time (a broken resume.py shouldn't break ``import orchestrator``).
    Returns ``None`` when the resume layer is unavailable.
    """
    try:
        from ..state import Step  # noqa: PLC0415
    except ImportError:
        return None
    mapping = {
        "preflight": Step.PREFLIGHT,
        "snapshot": Step.SNAPSHOT,
        "detect": Step.DETECT,
        "probe_surfaces": Step.PROBE_SURFACES,
        "noop_short_circuit": Step.NOOP_SHORT_CIRCUIT,
        "backup": Step.BACKUP,
        "walkthrough_decide": Step.WALKTHROUGH_DECIDE,
        "plugin_cleanup": Step.PLUGIN_CLEANUP,
        "plugin_migrate": Step.PLUGIN_MIGRATE,
        "surface_dispatch": Step.SURFACE_DISPATCH,
        "verify": Step.VERIFY,
        "record": Step.RECORD,
        "release": Step.RELEASE_LOCK,
    }
    return mapping.get(phase_name)


# ---------------------------------------------------------------------------
# Marker transition helpers
# ---------------------------------------------------------------------------

def _marker_running(ctx: "PipelineContext", phase_name: str) -> None:
    """Emit a RUNNING marker for *phase_name* when the context carries one.

    Best-effort: a marker write failure is recorded on the pipeline
    context's ``stale_retry_dropped`` (mis-named but the only free slot
    for diagnostics) and the run continues. The resume marker is the
    LAST write per step -- never gate destructive ops on it.
    """
    marker = getattr(ctx, "resume_marker", None)
    if marker is None:
        return
    step = _resume_step_for_phase(phase_name)
    if step is None:
        return
    try:
        from .. import resume as _resume  # noqa: PLC0415
        new = _resume.mark_step_running(
            marker, step, halted_iso=iso_now(),
        )
        _resume.write_marker(ctx.world_root_resolved, new)
        ctx.resume_marker = new
    except Exception:  # noqa: BLE001
        # Marker writes are best-effort -- swallow rather than fail the
        # pipeline on a forensic side-effect.
        pass


def _marker_completed(ctx: "PipelineContext", phase_name: str) -> None:
    """Emit a COMPLETED marker for *phase_name* when the context carries one."""
    marker = getattr(ctx, "resume_marker", None)
    if marker is None:
        return
    step = _resume_step_for_phase(phase_name)
    if step is None:
        return
    try:
        from .. import resume as _resume  # noqa: PLC0415
        new = _resume.mark_step_completed(
            marker, step, halted_iso=iso_now(),
        )
        _resume.write_marker(ctx.world_root_resolved, new)
        ctx.resume_marker = new
    except Exception:  # noqa: BLE001
        pass


def _capture_world_fingerprint(
    ctx: "PipelineContext", detection: "DetectionReport",
) -> None:
    """Stamp ``ctx.resume_marker.world_fingerprint_at_start`` from detection.

    The fresh-marker path in ``_build_fresh_marker`` writes a
    placeholder ``{}`` because phase 3 has not yet produced
    ``DetectionReport.all_signals_raw`` at preflight-marker-write
    time. After phase 3 lands we re-stamp the marker so a future
    ``--resume`` run's world-fingerprint diff has a real anchor.

    Best-effort: a write failure here just skips the re-stamp; the
    pipeline continues. We never overwrite a non-empty fingerprint
    (the ``--resume`` path keeps the validator-confirmed original
    intact).
    """
    marker = getattr(ctx, "resume_marker", None)
    if marker is None:
        return
    existing = getattr(marker, "world_fingerprint_at_start", None) or {}
    if existing:
        return  # never overwrite a non-empty fingerprint
    fingerprint = getattr(detection, "all_signals_raw", None)
    if not fingerprint:
        return
    try:
        from .. import resume as _resume  # noqa: PLC0415
        from ..state import ResumeMarker  # noqa: PLC0415
        new = ResumeMarker(
            schema_version=marker.schema_version,
            started_iso=marker.started_iso,
            halted_iso=iso_now(),
            tool_version_at_run=marker.tool_version_at_run,
            world_fingerprint_at_start=dict(fingerprint),
            planned_ops=list(marker.planned_ops),
            completed_ops=list(marker.completed_ops),
            last_step=marker.last_step,
            last_status=marker.last_status,
            last_error=marker.last_error,
        )
        _resume.write_marker(ctx.world_root_resolved, new)
        ctx.resume_marker = new
    except Exception:  # noqa: BLE001
        # Best-effort -- never block the pipeline on a forensic
        # marker side-effect.
        pass


# ---------------------------------------------------------------------------
# Replay-phase set + completion predicate
# ---------------------------------------------------------------------------

# Phases that are safe to replay on ``--resume``. ``_phase_already_completed``
# treats these as "always re-run even if completed". Module-level so
# ``from .orchestrator import REPLAY_PHASES`` (fn-20 spec) resolves.
REPLAY_PHASES = (
    "walkthrough_decide",
    "plugin_cleanup",
    "plugin_migrate",
    "verify",
)


def _phase_already_completed(
    ctx: "PipelineContext", phase_name: str,
) -> bool:
    """Return True when *phase_name*'s step is already in ``completed_ops``.

    Used by ``run_pipeline`` on ``--resume`` runs so phases marked
    completed in the marker before the prior halt do NOT replay --
    R5: "pipeline restart from arbitrary marker resumes correctly".

    A phase whose step ordinal is BELOW ``ctx.resume_from_step``
    counts as already-completed even if the marker's
    ``completed_ops`` list missed it (defensive: a halt mid-write
    could leave a stale step in RUNNING). The validator computed
    ``resume_from_step`` from ``step_after(completed_ops[-1])``, so
    treating "ordinal < resume_from_step.value" as done is the
    contract.

    Returns False on fresh runs (no resume_from_step) and on the
    NOOP_SHORT_CIRCUIT step specifically -- per
    ``state.Step.NOOP_SHORT_CIRCUIT`` doc, the resumer MUST re-run
    detection + probe + gate evaluation; the marker's gate decision
    is NOT trusted. Phases 2/3/4 (the pre-gate detection trio) are
    similarly always replayed under ``--resume`` because the gate's
    inputs come from those phases.
    """
    target = getattr(ctx, "resume_from_step", None)
    if target is None:
        return False
    step = _resume_step_for_phase(phase_name)
    if step is None:
        return False
    # Pre-gate phases (snapshot, detect, probe_surfaces) and the
    # gate itself (noop_short_circuit) always re-run on resume.
    try:
        from ..state import Step  # noqa: PLC0415
    except ImportError:
        return False
    if step.value <= Step.NOOP_SHORT_CIRCUIT.value:
        return False
    # idempotent / report-
    # producing phases MUST re-run on resume so downstream phases
    # see hydrated state. Without replay, a fresh ``PipelineContext``
    # would have ``walkthrough_decisions=None``,
    # ``cleanup_report=None``, ``migration_reports=[]``, and
    # ``verify_report=None``, causing ``phase_record`` to emit a
    # final record that omits already-applied operations.
    #
    # Each phase in REPLAY_PHASES is safe to replay on disk that is
    # already in the desired state:
    #   * walkthrough_decide -- pure decision computation; no writes.
    #   * plugin_cleanup     -- catalog-driven deletions that
    #                            short-circuit on missing targets.
    #   * plugin_migrate     -- per-op idempotency guards documented
    #                            on every migration runner.
    #   * verify             -- read-only invariant check.
    #
    # ``backup``, ``surface_dispatch``, and ``record`` remain in the
    # skip set. Backup re-write would create a duplicate tarball;
    # ``ctx.backup_tarball_path`` is hydrated separately on resume
    # via :func:`_hydrate_backup_tarball_path_on_resume`. Surface
    # dispatch is not idempotent (real surface invocations side-
    # effect external systems) and must NOT replay -- the
    # ``dispatch_results`` slot stays empty, callers consuming it
    # downstream guard against ``None``. Record is the terminal
    # writer; on resume, if it was already applied the post-gate
    # loop will skip it.
    if phase_name in REPLAY_PHASES:
        return False
    return step.value < target.value


# Explicitly attach REPLAY_PHASES as a function attribute so fn-20's
# ``_phase_already_completed.REPLAY_PHASES`` access pattern resolves.
# Function objects do NOT proxy module globals via attribute lookup,
# so this assignment is required (not optional).
_phase_already_completed.REPLAY_PHASES = REPLAY_PHASES


def _marker_failed(ctx: "PipelineContext", phase_name: str, error: str) -> None:
    """Emit a FAILED marker when a phase raises mid-step."""
    marker = getattr(ctx, "resume_marker", None)
    if marker is None:
        return
    step = _resume_step_for_phase(phase_name)
    if step is None:
        return
    try:
        from .. import resume as _resume  # noqa: PLC0415
        new = _resume.mark_step_failed(
            marker, step, error, halted_iso=iso_now(),
        )
        _resume.write_marker(ctx.world_root_resolved, new)
        ctx.resume_marker = new
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Dispatch sidecar helpers
# ---------------------------------------------------------------------------

_DISPATCH_SIDECAR_SUFFIX = "-dispatch.json"


def _dispatch_sidecar_path(world_root_resolved: str, started_iso: str) -> str:
    """Path for the surface-dispatch sidecar JSON for *started_iso*."""
    ts = started_iso.replace(":", "-").replace("Z", "")
    return os.path.join(
        world_root_resolved, ".alive", "upgrades",
        ts + _DISPATCH_SIDECAR_SUFFIX,
    )


def _write_dispatch_results_sidecar(
    world_root_resolved: str,
    started_iso: str,
    dispatch_results: Any,
) -> None:
    """Persist dispatch_results to a sidecar JSON for resume hydration.

    The sidecar lives at
    ``<world>/.alive/upgrades/<filename-safe-iso>-dispatch.json`` and
    is atomic via os.rename. Callers must serialise dispatch_results
    to a JSON-friendly shape before calling; this helper leaves that
    coercion to the surface layer (which already has dataclass-to-
    dict serialisers) and accepts any object json.dumps can handle.
    """
    import json as _json  # noqa: PLC0415
    target = _dispatch_sidecar_path(world_root_resolved, started_iso)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    serialisable: List[Dict[str, Any]] = []
    for d in (dispatch_results or []):
        as_dict = getattr(d, "as_dict", None)
        if callable(as_dict):
            serialisable.append(as_dict())
        elif isinstance(d, dict):
            serialisable.append(d)
        else:
            # Last-resort: try a shallow attribute mirror.
            serialisable.append({
                k: getattr(d, k)
                for k in dir(d)
                if not k.startswith("_") and not callable(getattr(d, k))
            })
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(serialisable, f, sort_keys=True, indent=2)
    os.rename(tmp, target)


def _hydrate_dispatch_results_on_resume(
    ctx: "PipelineContext", world_root_resolved: str,
) -> None:
    """Restore ``ctx.dispatch_results`` from the sidecar on resume.

    Best-effort: errors are silently ignored. The record will fall
    back to an empty surfaces section if the sidecar is missing,
    which is the pre-T15 baseline behaviour. The sidecar is keyed
    by ``started_iso`` so a fresh run cannot accidentally pick up
    a stale prior run's dispatch outcomes -- the sidecar's
    filename-safe ISO matches the run's started_iso 1:1.
    """
    import json as _json  # noqa: PLC0415
    try:
        path = _dispatch_sidecar_path(
            world_root_resolved, ctx.started_iso,
        )
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            ctx.dispatch_results = _json.load(f)
    except (OSError, ValueError):
        # Best-effort; see helper docstring.
        pass


def _hydrate_backup_tarball_path_on_resume(
    ctx: "PipelineContext", world_root_resolved: str,
) -> None:
    """Restore ``ctx.backup_tarball_path`` from disk on resume.

    When ``--resume`` advances past phase 6 (BACKUP), the new
    ``PipelineContext`` is empty even though a tarball was written
    during the prior run. ``phase_record`` and the post-upgrade
    rollback pointer need the path; without hydration the resumed
    run emits a record with ``backup_tarball_path=null`` and the
    operator loses the rollback handle.

    Discovery rule: the most recent
    ``.alive/upgrades/pre-upgrade-<filename-safe-iso>.tar.gz`` under
    *world_root_resolved*; lexicographic sort is the canonical
    ordering (filename timestamps are filename-safe ISO so lex order
    == chronological). Best-effort: errors are silently ignored, the
    record will still emit with ``backup_tarball_path=null`` (no
    worse than the un-hydrated baseline).
    """
    try:
        upgrades_dir = os.path.join(
            world_root_resolved, ".alive", "upgrades",
        )
        if not os.path.isdir(upgrades_dir):
            return
        candidates = [
            n for n in os.listdir(upgrades_dir)
            if n.startswith("pre-upgrade-") and n.endswith(".tar.gz")
        ]
        if not candidates:
            return
        candidates.sort()
        ctx.backup_tarball_path = os.path.join(
            upgrades_dir, candidates[-1],
        )
    except OSError:
        # Best-effort: a permission error or disappearing dir is not
        # cause to abort the resumed run.
        pass


# Public symbol surface for ``orchestrator.py`` re-exports.
__all__ = (
    "PhaseNotImplemented",
    "PhaseWriteError",
    "REPLAY_PHASES",
    "_unique_record_path",
    "_resume_step_for_phase",
    "_marker_running",
    "_marker_completed",
    "_marker_failed",
    "_capture_world_fingerprint",
    "_phase_already_completed",
    "_dispatch_sidecar_path",
    "_write_dispatch_results_sidecar",
    "_hydrate_dispatch_results_on_resume",
    "_hydrate_backup_tarball_path_on_resume",
)
