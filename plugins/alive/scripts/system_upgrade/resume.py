"""Resume-marker plumbing for ``alive system-upgrade`` (T6 of fn-18).

Two halves:

1. **Marker write side** (``write_marker``, ``new_marker``,
   ``mark_step_completed``, ``mark_step_failed``). Atomic
   per-transition writes via ``_record_codec.write_atomic`` -- the
   resume marker is the LAST write per step (gap practice-scout: never
   advance the checkpoint before the destructive op fsyncs). The
   T6-owned ``state.ResumeMarker`` is the single source of truth for
   layout; we hand it to the codec, not raw dicts.

2. **Resume validation side** (``find_latest_marker``, ``load_marker``,
   ``ResumeValidator``, ``ResumePlan``). Read the most-recent
   ``*-resume.yaml`` under ``<world>/.alive/upgrades/``, run the four
   validation chains the spec calls out (R8):
     1. Staleness refusal (``--resume-staleness <hours>``;
        ``--force`` bypasses).
     2. Re-run T3's FileSnapshot + DetectionReport on the live world
        (do NOT trust the marker's fingerprint -- gap practice-scout).
     3. Diff fresh fingerprint vs marker; soft-refuse divergence
        (``--force`` bypasses).
     4. Compare ``tool_version_at_run`` to the live ``plugin.json``
        version. Hard-refuse skew -- ``--force`` does NOT bypass.
     5. On all-pass: caller resumes from
        ``Step[completed_ops[-1] + 1]`` (or ``Step[last_step]`` when
        no step has completed yet).

The validator surfaces ``ResumeRefusal`` (with a stable ``code`` for
the CLI envelope) on every failure mode the spec calls out. Concurrent-
session locking on the actual resumed run is owned by ``UpgradeLock``
from T1 -- this module does NOT acquire / release the lock; the
caller (cli.py / orchestrator wiring in T7-T11) wraps the validated
plan inside an ``UpgradeLock`` context.

Stdlib-only (R10).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from _common import iso_now

from . import _record_codec
from .state import (
    MARKER_SCHEMA_VERSION,
    MARKER_SUBDIR,
    MARKER_SUFFIX,
    ResumeMarker,
    Step,
    StepStatus,
    parse_aware_iso,
)
from .tool_version import read_tool_version


__all__ = (
    "DEFAULT_STALENESS_HOURS",
    "ResumeRefusal",
    "ResumePlan",
    "find_latest_marker",
    "load_marker",
    "marker_path_for",
    "new_marker",
    "validate_resume",
    "write_marker",
    "mark_step_running",
    "mark_step_completed",
    "mark_step_failed",
)


#: Default ``--resume-staleness`` cutoff in hours; spec-locked at 24h.
#: Mirrors ``cli.py``'s ``--resume-staleness`` flag default so
#: programmatic callers see the same number when they don't override.
DEFAULT_STALENESS_HOURS: int = 24


# ---------------------------------------------------------------------------
# Refusal type
# ---------------------------------------------------------------------------

class ResumeRefusal(Exception):
    """Raised by :func:`validate_resume` to surface a structured refusal.

    Carries a stable ``code`` for the CLI envelope so the operator can
    grep without parsing prose. Codes (locked):

    * ``"resume_marker_missing"``   -- ``--resume`` requested but no
                                        ``*-resume.yaml`` is present
                                        under ``<world>/.alive/upgrades/``.
    * ``"resume_marker_unreadable"`` -- marker file present but parse
                                        / shape failed; surfaced as a
                                        hard refusal (``--force`` does
                                        NOT bypass; rerun fresh).
    * ``"resume_marker_stale"``     -- marker older than the
                                        ``--resume-staleness`` cutoff;
                                        ``--force`` bypasses.
    * ``"resume_world_diverged"``   -- world fingerprint differs from
                                        marker; ``--force`` bypasses.
    * ``"resume_tool_version_skew"`` -- plugin updated since the
                                        original run; HARD refusal
                                        (``--force`` does NOT bypass).
    * ``"resume_step_unknown"``     -- marker references a Step name
                                        the current code doesn't know
                                        (refactor mismatch); HARD
                                        refusal -- rerun fresh.
    * ``"resume_already_done"``     -- the run completed successfully;
                                        ``RELEASE_LOCK`` was the last
                                        completed step. Distinct code
                                        so the operator sees "nothing
                                        to do" rather than a confusing
                                        diff.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostic: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostic: Dict[str, Any] = (
            dict(diagnostic) if diagnostic is not None else {}
        )


# ---------------------------------------------------------------------------
# Marker path helpers
# ---------------------------------------------------------------------------

def _filename_safe_iso(iso_ts: str) -> str:
    """Convert ``2026-05-04T01:23:45Z`` to ``2026-05-04T01-23-45Z``.

    Mirrors the convention used by ``orchestrator.write_noop_record_to_world``:
    colons are not portable in filenames on all filesystems, so swap
    them for hyphens. The trailing ``Z`` is preserved so the ISO
    profile remains recognisable.
    """
    return iso_ts.replace(":", "-")


def marker_path_for(world_root: str, started_iso: str) -> str:
    """Return the absolute marker path for a given run-start timestamp.

    The pattern ``<filename-safe-iso>-resume.yaml`` deliberately leads
    with the timestamp so a lexical sort of the directory yields the
    most-recent marker last (matches ``find_latest_marker``).
    """
    fname = "{}{}".format(_filename_safe_iso(started_iso), MARKER_SUFFIX)
    return os.path.join(world_root, MARKER_SUBDIR, fname)


def find_latest_marker(world_root: str) -> Optional[str]:
    """Return the most-recent marker path under *world_root*, or None.

    Selection criteria:
      1. Filename ends with :data:`MARKER_SUFFIX`.
      2. Among matches, the lexical-sort winner is returned.

    Lexical sort is sound because filenames embed the run-start ISO
    timestamp at the head; for two markers in the same world the
    later run-start wins. Mtime is NOT consulted -- a marker copied
    in from another machine carries its origin mtime, which is wrong
    for our "most-recent run" semantic.
    """
    upgrades_dir = os.path.join(world_root, MARKER_SUBDIR)
    if not os.path.isdir(upgrades_dir):
        return None
    candidates: List[str] = []
    try:
        names = os.listdir(upgrades_dir)
    except OSError:
        return None
    for name in names:
        if name.endswith(MARKER_SUFFIX):
            candidates.append(os.path.join(upgrades_dir, name))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1]


# ---------------------------------------------------------------------------
# Marker write side
# ---------------------------------------------------------------------------

def new_marker(
    *,
    started_iso: str,
    tool_version_at_run: str,
    world_fingerprint_at_start: Dict[str, Any],
    planned_ops: List[str],
    halted_iso: Optional[str] = None,
) -> ResumeMarker:
    """Construct the marker for a fresh run start.

    ``halted_iso`` defaults to ``started_iso`` -- the marker is also
    valid AS the prelude before any step body runs.
    """
    return ResumeMarker(
        schema_version=MARKER_SCHEMA_VERSION,
        started_iso=started_iso,
        halted_iso=halted_iso if halted_iso is not None else started_iso,
        tool_version_at_run=tool_version_at_run,
        world_fingerprint_at_start=dict(world_fingerprint_at_start),
        planned_ops=list(planned_ops),
        completed_ops=[],
        last_step=None,
        last_status=StepStatus.RUNNING.value,
        last_error=None,
    )


def write_marker(
    world_root: str,
    marker: ResumeMarker,
) -> str:
    """Atomically write *marker* to ``<world>/.alive/upgrades/<ts>-resume.yaml``.

    Returns the absolute path written. Caller is responsible for
    advancing ``halted_iso`` / ``last_step`` / ``completed_ops``
    BEFORE the call -- this function does NOT mutate the marker.

    Marker writes go through :mod:`._record_codec` (JSON-as-YAML)
    rather than ``_alive_common.yaml_emit`` -- the latter cannot
    round-trip nested dicts like ``world_fingerprint_at_start`` (per
    the audit at task acceptance
    point #11).

    Atomicity comes from the codec's ``atomic_write_text`` chain:
    mkstemp + write + fsync(fd) + chmod + replace + fsync(parent).
    Crash mid-write leaves either the prior marker file (if it
    existed) or no marker file -- never a half-written one.
    """
    target = marker_path_for(world_root, marker.started_iso)
    _record_codec.write_atomic(target, marker.to_dict())
    return target


def mark_step_running(
    marker: ResumeMarker,
    step: Step,
    *,
    halted_iso: Optional[str] = None,
) -> ResumeMarker:
    """Return a copy of *marker* with ``last_step`` set + RUNNING status.

    Pure transformation; caller pipes the result into :func:`write_marker`.
    Persisted BEFORE the step body runs so a crash mid-step lands the
    marker at ``RUNNING`` for that step, not COMPLETED.

    ``halted_iso`` defaults to ``iso_now()`` so callers don't need
    to thread the timestamp from outside.
    """
    return ResumeMarker(
        schema_version=marker.schema_version,
        started_iso=marker.started_iso,
        halted_iso=halted_iso if halted_iso is not None else iso_now(),
        tool_version_at_run=marker.tool_version_at_run,
        world_fingerprint_at_start=dict(marker.world_fingerprint_at_start),
        planned_ops=list(marker.planned_ops),
        completed_ops=list(marker.completed_ops),
        last_step=step.name,
        last_status=StepStatus.RUNNING.value,
        last_error=None,  # cleared on every transition into RUNNING
    )


def mark_step_completed(
    marker: ResumeMarker,
    step: Step,
    *,
    halted_iso: Optional[str] = None,
) -> ResumeMarker:
    """Return a copy of *marker* with *step* appended to ``completed_ops``.

    Idempotent for the same step (re-completing a step that's already
    last in ``completed_ops`` is a no-op-ish: the list does NOT grow).
    Avoids accidental duplicates when the orchestrator's recovery
    path replays a step.

    Caller writes via :func:`write_marker` AFTER the step's destructive
    fsync has landed -- the marker is the LAST write per step.
    """
    completed = list(marker.completed_ops)
    if not completed or completed[-1] != step.name:
        completed.append(step.name)
    return ResumeMarker(
        schema_version=marker.schema_version,
        started_iso=marker.started_iso,
        halted_iso=halted_iso if halted_iso is not None else iso_now(),
        tool_version_at_run=marker.tool_version_at_run,
        world_fingerprint_at_start=dict(marker.world_fingerprint_at_start),
        planned_ops=list(marker.planned_ops),
        completed_ops=completed,
        # Once a step completes, ``last_step`` clears -- the run is
        # between steps. The next mark_step_running sets it again.
        last_step=None,
        last_status=StepStatus.COMPLETED.value,
        last_error=None,
    )


def mark_step_failed(
    marker: ResumeMarker,
    step: Step,
    error_summary: str,
    *,
    halted_iso: Optional[str] = None,
) -> ResumeMarker:
    """Return a copy of *marker* with FAILED status + error summary.

    ``error_summary`` should be a single short line -- we trim to a
    reasonable length so a 10-page traceback doesn't bloat the marker.
    The full traceback belongs in the run's progress log; the marker
    just needs enough to identify the failure mode at resume time.
    """
    summary = error_summary if error_summary else "(no error summary)"
    if len(summary) > 1024:
        summary = summary[:1021] + "..."
    return ResumeMarker(
        schema_version=marker.schema_version,
        started_iso=marker.started_iso,
        halted_iso=halted_iso if halted_iso is not None else iso_now(),
        tool_version_at_run=marker.tool_version_at_run,
        world_fingerprint_at_start=dict(marker.world_fingerprint_at_start),
        planned_ops=list(marker.planned_ops),
        completed_ops=list(marker.completed_ops),
        last_step=step.name,
        last_status=StepStatus.FAILED.value,
        last_error=summary,
    )


# ---------------------------------------------------------------------------
# Marker read side
# ---------------------------------------------------------------------------

def load_marker(marker_path: str) -> ResumeMarker:
    """Parse *marker_path* into a :class:`ResumeMarker`.

    Raises
    ------
    ResumeRefusal
        ``code="resume_marker_unreadable"`` -- file missing, JSON
        parse error, or layout-shape mismatch. Hard refusal --
        ``--force`` does NOT bypass; resuming an unreadable marker
        risks correctness, so we surface "rerun fresh".
    """
    if not os.path.isfile(marker_path):
        raise ResumeRefusal(
            "resume_marker_missing",
            "no resume marker at {}".format(marker_path),
            diagnostic={"marker_path": marker_path},
        )
    try:
        raw = _record_codec.read(marker_path)
    except (OSError, ValueError) as exc:
        raise ResumeRefusal(
            "resume_marker_unreadable",
            "could not read resume marker {}: {}".format(marker_path, exc),
            diagnostic={"marker_path": marker_path, "cause": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001 -- json.JSONDecodeError is OSError-ish
        raise ResumeRefusal(
            "resume_marker_unreadable",
            "could not parse resume marker {}: {}".format(marker_path, exc),
            diagnostic={"marker_path": marker_path, "cause": str(exc)},
        ) from exc
    try:
        return ResumeMarker.from_dict(raw)
    except ValueError as exc:
        raise ResumeRefusal(
            "resume_marker_unreadable",
            "resume marker {} has an unexpected layout: {}".format(
                marker_path, exc,
            ),
            diagnostic={"marker_path": marker_path, "cause": str(exc)},
        ) from exc


# ---------------------------------------------------------------------------
# Resume validator
# ---------------------------------------------------------------------------

@dataclass
class ResumePlan:
    """Outcome of a successful :func:`validate_resume` call.

    Carries the marker, the resume-from step, and the diagnostics the
    CLI may want to surface (e.g. on ``--force``-bypassed divergence).

    ``divergence_diagnostic`` is non-empty only when ``--force``
    bypassed a world-fingerprint divergence; the validator records
    exactly which keys differed so the operator's verbose output can
    show the diff.

    ``stale_diagnostic`` mirrors the above for ``--force``-bypassed
    staleness.
    """

    marker_path: str
    marker: ResumeMarker
    resume_from: Step
    forced_divergence: bool = False
    forced_stale: bool = False
    divergence_diagnostic: Dict[str, Any] = field(default_factory=dict)
    stale_diagnostic: Dict[str, Any] = field(default_factory=dict)


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse ``2026-05-04T01:23:45Z`` -> aware datetime, or None on error.

    Wraps :func:`state.parse_aware_iso` to keep the validator's
    interface tolerant (None on miss) while inheriting the strict
    require-aware-offset rule. A naive timestamp returns None just
    like an unparseable one, so :func:`_staleness_hours` never reaches
    the ``aware - naive`` subtraction that would raise ``TypeError``.
    """
    if not ts:
        return None
    try:
        return parse_aware_iso(ts)
    except ValueError:
        return None


def _staleness_hours(now_iso: str, halted_iso: str) -> Optional[float]:
    """Return the staleness delta in hours, or None when either ts is invalid.

    Both ``now_iso`` and ``halted_iso`` are parsed via :func:`_parse_iso`,
    which guarantees offset-aware datetimes (or None). Any non-numeric
    arithmetic failure is caught defensively so the validator never
    crashes on a malformed marker -- the caller proceeds as if the
    staleness check is inconclusive, which is consistent with the
    "load_marker should already have rejected this" precondition.
    """
    now = _parse_iso(now_iso)
    halted = _parse_iso(halted_iso)
    if now is None or halted is None:
        return None
    try:
        delta = now - halted
    except TypeError:
        # Defence-in-depth: ``_parse_iso`` already rejects naive
        # timestamps, so the only way to land here is a future bug
        # bypassing that gate. Surface as "inconclusive" rather than
        # crashing the validator.
        return None
    return delta.total_seconds() / 3600.0


def _diff_fingerprints(
    expected: Dict[str, Any],
    actual: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a human-readable diff of two ``all_signals_raw`` dicts.

    The diff is intentionally shallow (top-level keys + their string
    repr) -- a full-recursive diff explodes log size for the
    forensic-only fields ``walnut_probes`` / ``world_path_probes``
    which are huge. Operators who need the full delta can compare
    the two dicts at the codec level.

    Returns a dict shaped::

        {
            "added_keys":   [str, ...],
            "removed_keys": [str, ...],
            "changed_keys": [{"key": str, "before": "...", "after": "..."}, ...],
        }
    """
    expected_keys = set(expected.keys())
    actual_keys = set(actual.keys())
    added = sorted(actual_keys - expected_keys)
    removed = sorted(expected_keys - actual_keys)
    changed: List[Dict[str, str]] = []
    for key in sorted(expected_keys & actual_keys):
        if expected[key] != actual[key]:
            before = repr(expected[key])
            after = repr(actual[key])
            # Trim each side so the marker's huge probe arrays don't
            # produce 50KB diff messages -- the operator only needs to
            # see WHICH key drifted, not the full payload.
            if len(before) > 256:
                before = before[:253] + "..."
            if len(after) > 256:
                after = after[:253] + "..."
            changed.append({"key": key, "before": before, "after": after})
    return {
        "added_keys": added,
        "removed_keys": removed,
        "changed_keys": changed,
    }


def validate_resume(
    world_root: str,
    *,
    plugin_root: Optional[str] = None,
    fresh_fingerprint: Optional[Dict[str, Any]] = None,
    force: bool = False,
    staleness_hours: int = DEFAULT_STALENESS_HOURS,
    now_iso: Optional[str] = None,
    marker_path: Optional[str] = None,
) -> ResumePlan:
    """Run the full resume validation chain against *world_root*.

    The caller is responsible for re-running phase-2 FileSnapshot +
    phase-3 detection on the live world AHEAD of this call and
    threading ``DetectionReport.all_signals_raw`` in via
    *fresh_fingerprint*. Doing the detection here would make the
    function untestable in unit-test isolation -- the spec calls out
    "re-run FileSnapshot + DetectionReport" as a step the validator
    sequences but does not have to OWN.

    Validation chain (in order; first failure short-circuits):

    1. Locate marker. ``marker_path`` overrides; otherwise
       :func:`find_latest_marker` is called against *world_root*. A
       missing marker raises ``code="resume_marker_missing"`` (hard --
       ``--force`` does NOT bypass; nothing to resume to).
    2. Parse marker via :func:`load_marker`. Parse / shape errors
       raise ``code="resume_marker_unreadable"`` (hard).
    3. Already-done check. When ``Step[completed_ops[-1]]`` is
       ``RELEASE_LOCK``, raise ``code="resume_already_done"`` (hard --
       there is no remaining work).
    4. Plugin-version skew. Compare ``marker.tool_version_at_run`` to
       a fresh ``read_tool_version(plugin_root)``. Skew raises
       ``code="resume_tool_version_skew"`` (HARD -- ``--force`` does
       NOT bypass; user must run a fresh upgrade).
    5. Staleness. ``halted_iso`` older than *staleness_hours* before
       *now_iso* raises ``code="resume_marker_stale"`` UNLESS
       ``force`` is True (soft refusal).
    6. World-fingerprint divergence. When *fresh_fingerprint* is
       supplied AND it differs from ``marker.world_fingerprint_at_start``,
       raise ``code="resume_world_diverged"`` UNLESS ``force`` is True
       (soft refusal).
    7. Resume-target validation. Compute ``marker.resume_from_step()``;
       a ValueError (Step name not in current enum) raises
       ``code="resume_step_unknown"`` (hard refusal -- the orchestrator
       refactored under us; rerun fresh).

    Returns a :class:`ResumePlan` on all-pass. On a successful resume
    from ``NOOP_SHORT_CIRCUIT``, the orchestrator MUST re-run detection
    + probe + gate evaluation fresh (the marker's gate decision is
    NOT trusted -- gap practice-scout); this contract is documented on
    :class:`Step.NOOP_SHORT_CIRCUIT` itself.
    """
    # Step 1: locate marker.
    if marker_path is None:
        marker_path = find_latest_marker(world_root)
    if marker_path is None or not marker_path:
        raise ResumeRefusal(
            "resume_marker_missing",
            "no resume marker found under {}/{}; nothing to resume from."
            .format(world_root, MARKER_SUBDIR),
            diagnostic={
                "world_root": world_root,
                "upgrades_dir": os.path.join(world_root, MARKER_SUBDIR),
            },
        )

    # Step 2: parse marker (raises resume_marker_unreadable on failure).
    marker = load_marker(marker_path)

    # Step 3: already-done short-circuit. completed_ops carries
    # RELEASE_LOCK (phase 13) iff the run finished cleanly. The CLI
    # surfaces this as a distinct error_code so the operator sees
    # "nothing to do" rather than the generic divergence-diff prose.
    if marker.completed_ops:
        last_completed_name = marker.completed_ops[-1]
        if last_completed_name == Step.RELEASE_LOCK.name:
            raise ResumeRefusal(
                "resume_already_done",
                "resume marker at {} reports the prior run completed "
                "successfully (last completed step: {}); nothing to do."
                .format(marker_path, last_completed_name),
                diagnostic={
                    "marker_path": marker_path,
                    "last_completed_step": last_completed_name,
                },
            )

    # Step 4: plugin-version skew (HARD -- --force does NOT bypass).
    live_tool_version = read_tool_version(plugin_root) if plugin_root else "unknown"
    marker_tool_version = marker.tool_version_at_run or "unknown"
    if (
        live_tool_version != "unknown"
        and marker_tool_version != "unknown"
        and live_tool_version != marker_tool_version
    ):
        raise ResumeRefusal(
            "resume_tool_version_skew",
            (
                "plugin updated since halt: marker tool_version_at_run="
                "{!r}, current plugin.json version={!r}. The plugin's "
                "world-format expectations may have changed -- rerun "
                "fresh `/alive:system-upgrade` rather than resuming. "
                "(--force does NOT bypass this refusal.)"
            ).format(marker_tool_version, live_tool_version),
            diagnostic={
                "marker_tool_version": marker_tool_version,
                "live_tool_version": live_tool_version,
                "marker_path": marker_path,
            },
        )

    # Step 5: staleness (soft -- --force bypasses).
    now_iso_resolved = now_iso if now_iso is not None else iso_now()
    delta_hours = _staleness_hours(now_iso_resolved, marker.halted_iso)
    forced_stale = False
    stale_diagnostic: Dict[str, Any] = {}
    if delta_hours is not None and delta_hours > float(staleness_hours):
        stale_diagnostic = {
            "halted_iso": marker.halted_iso,
            "now_iso": now_iso_resolved,
            "staleness_hours_observed": round(delta_hours, 3),
            "staleness_hours_threshold": int(staleness_hours),
        }
        if not force:
            raise ResumeRefusal(
                "resume_marker_stale",
                (
                    "resume marker {} is {:.1f}h old (threshold {}h); pass "
                    "--force to resume anyway, or rerun fresh."
                ).format(marker_path, delta_hours, staleness_hours),
                diagnostic=stale_diagnostic,
            )
        forced_stale = True

    # Step 6: world-fingerprint divergence (soft -- --force bypasses).
    forced_divergence = False
    divergence_diagnostic: Dict[str, Any] = {}
    if fresh_fingerprint is not None:
        if fresh_fingerprint != marker.world_fingerprint_at_start:
            divergence_diagnostic = _diff_fingerprints(
                marker.world_fingerprint_at_start,
                fresh_fingerprint,
            )
            divergence_diagnostic["marker_path"] = marker_path
            if not force:
                raise ResumeRefusal(
                    "resume_world_diverged",
                    (
                        "world content has changed since halt; pass --force "
                        "to resume anyway, or rerun fresh. Diff summary: "
                        "added={} removed={} changed={}"
                    ).format(
                        len(divergence_diagnostic["added_keys"]),
                        len(divergence_diagnostic["removed_keys"]),
                        len(divergence_diagnostic["changed_keys"]),
                    ),
                    diagnostic=divergence_diagnostic,
                )
            forced_divergence = True

    # Step 7: resume-target validation. ValueError -> resume_step_unknown.
    try:
        resume_from = marker.resume_from_step()
    except ValueError as exc:
        raise ResumeRefusal(
            "resume_step_unknown",
            str(exc),
            diagnostic={
                "marker_path": marker_path,
                "completed_ops": list(marker.completed_ops),
                "last_step": marker.last_step,
            },
        ) from exc

    # ``resume_from is None`` only when ``completed_ops[-1]`` was
    # RELEASE_LOCK -- handled at step 3. Belt-and-suspenders: surface
    # as already-done if we somehow reach here with None.
    if resume_from is None:
        raise ResumeRefusal(
            "resume_already_done",
            "resume target step is None (run already completed).",
            diagnostic={
                "marker_path": marker_path,
                "completed_ops": list(marker.completed_ops),
            },
        )

    return ResumePlan(
        marker_path=marker_path,
        marker=marker,
        resume_from=resume_from,
        forced_divergence=forced_divergence,
        forced_stale=forced_stale,
        divergence_diagnostic=divergence_diagnostic,
        stale_diagnostic=stale_diagnostic,
    )
