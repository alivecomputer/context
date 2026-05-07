"""Phase 4 + phase 10 multi-surface orchestrator (T7 of fn-18).

Three phase-distinct concerns split per epic spec / Codex M6 +
round 5 M2:

1. ``load_prior_final_record`` -- phase 4, runs UNCONDITIONALLY (even
   under ``--surfaces=none``). Loads the most recent strict-pattern
   final upgrade record at ``<world>/.alive/upgrades/`` and extracts
   each surface's ``needs_retry[]`` + ``version_at_retry`` into an
   in-memory ``surface_retry_map``. The no-op gate (phase 5) consults
   this map; a non-empty map suppresses short-circuit even on a
   current-version world.

2. ``probe_all`` -- phase 4, SKIPPED entirely when
   ``--surfaces=none``. Detects each surface's presence + reads
   ``<surface> --version --json`` to learn ``state_paths`` (so
   phase 8 cleanup can exclude them) and ``migrator_argv_prefix``
   (so phase 10 can dispatch the migrator).

3. ``dispatch_all`` -- phase 10, runs after plugin migration.
   SKIPPED when ``--surfaces=none``. Invokes each surface's migrator
   subprocess with the orchestrator-controlled normative tail
   (``--world <resolved-path> --json``) plus per-surface retry items
   carried over from ``surface_retry_map``. Soft-fails on parse /
   non-zero / timeout / missing-binary; never raises.

Phase-12 record-emission helpers also live here:

* ``apply_stale_drop`` -- AGE + version-mismatch predicate.
* ``build_surfaces_record_section`` -- assembles the final record's
  ``surfaces`` mapping from the run's probe + dispatch outputs (or
  the carry-forward path under ``--surfaces=none``).

Stdlib-only (R10).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# Re-export the surface-facing API so callers `from system_upgrade.surfaces
# import probe_all, dispatch_all, ...` works.
from system_upgrade.orchestrator import (
    ProbeError,
    ProbeResult,
    SurfaceRetryRecord,
)
from system_upgrade.surfaces._base import (
    DispatchResult,
    MigratorArgvPrefixInvalid,
    Surface,
    build_dispatch_argv,
    parse_version_json,
    run_subprocess_capture,
    truncate_for_record,
    DEFAULT_SUBPROCESS_TIMEOUT,
)


__all__ = (
    "Surface",
    "ProbeError",
    "ProbeResult",
    "DispatchResult",
    "SurfaceRetryRecord",
    "load_prior_final_record",
    "probe_all",
    "dispatch_all",
    "apply_stale_drop",
    "build_surfaces_record_section",
    "registered_surfaces",
    "parse_surfaces_filter",
    "STALE_RETRY_AGE_DAYS",
)


#: AGE clause for the stale-drop predicate. Retry records older than
#: this are dropped on the next run regardless of version.
STALE_RETRY_AGE_DAYS: int = 7


#: Strict filename pattern for final upgrade records. Excludes the
#: ``-resume.yaml`` (T6), ``-runstate.yaml`` (T9/T10), and
#: ``-retroactive.yaml`` (T9) suffixed siblings -- only canonical
#: ``YYYY-MM-DDTHH-MM-SS.yaml`` files match. An optional numeric
#: suffix (``-2``, ``-3``, ...) is permitted for collision-resolution
#: when two consecutive runs land in the same second (codex
#: completion-review fix).
_FINAL_RECORD_FILENAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})(?:-\d+)?\.yaml$"
)


#: Record ``schema_version`` values this tool knows how to read. A
#: prior record carrying any other value (e.g. ``"99"`` from a future
#: tool) is treated as "no prior retries" by ``load_prior_final_record``
#: -- the items' interpretation is undefined for this tool, and
#: blindly replaying them risks running an older tool against newer
#: record semantics. The validator at
#: ``tests/upgrade/schema/upgrade_record.py`` enforces the same
#: constraint on the WRITE path; this set is the READ-path counterpart.
_SUPPORTED_RECORD_SCHEMA_VERSIONS: frozenset = frozenset({"1"})


# ---------------------------------------------------------------------------
# Surface registry
# ---------------------------------------------------------------------------


def registered_surfaces() -> List[Surface]:
    """Return the canonical list of ``Surface`` instances.

    Order matters for deterministic output: alive-mcp, hermes, codex.
    Imports are local to keep the registry resilient to per-surface
    import errors (a broken stub shouldn't take down the orchestrator).
    """
    from system_upgrade.surfaces.alive_mcp import AliveMcpSurface
    from system_upgrade.surfaces.hermes import HermesSurface
    from system_upgrade.surfaces.codex import CodexSurface

    return [AliveMcpSurface(), HermesSurface(), CodexSurface()]


def parse_surfaces_filter(value: Optional[str]) -> Optional[List[str]]:
    """Translate the ``--surfaces`` CLI value into an active surface list.

    * ``None`` or ``"all"`` -- include every registered surface.
    * ``"none"`` -- return ``None`` (sentinel meaning "skip phase 4
      probe + phase 10 dispatch entirely"). The caller distinguishes
      "all surfaces" from "no surfaces" via this None/list distinction.
    * ``"alive-mcp,hermes"`` -- comma-separated allowlist.

    Whitespace is tolerated; unknown surfaces are passed through
    (``probe_all`` filters by membership; an unknown name produces no
    probe at all rather than an error -- matches the existing CLI's
    permissive flag handling).
    """
    if value is None:
        return [s.name for s in registered_surfaces()]
    norm = str(value).strip().lower()
    if norm == "none":
        return None
    if norm == "all" or norm == "":
        return [s.name for s in registered_surfaces()]
    return [tok.strip() for tok in norm.split(",") if tok.strip()]


# ---------------------------------------------------------------------------
# Phase 4a: load prior final record (R21)
# ---------------------------------------------------------------------------


def load_prior_final_record(
    world_root_resolved: str,
) -> Tuple[Dict[str, SurfaceRetryRecord], Optional[str], Optional[str]]:
    """Read the most recent final upgrade record's per-surface retry state.

    Runs UNCONDITIONALLY in phase 4 -- even under ``--surfaces=none``
    so the no-op gate sees pending retries from prior runs.

    Discovery rules (per epic spec § Canonical paths):

    * Walks ``<world>/.alive/upgrades/`` (returns empty if missing).
    * MUST match the strict filename pattern
      ``^\\d{4}-\\d{2}-\\d{2}T\\d{2}-\\d{2}-\\d{2}\\.yaml$`` -- excludes
      ``*-resume.yaml`` (T6), ``*-runstate.yaml`` (T9/T10), and
      ``*-retroactive.yaml`` (T9 -- never carries retry items).
    * Filename-timestamp sort (mtime is unreliable per LD16); the
      lexicographically-greatest filename is the most recent.

    Returns
    -------
    (surface_retry_map, started_at, record_path)
        ``surface_retry_map`` is keyed by surface name; values are
        ``SurfaceRetryRecord`` with the surface's retry items +
        ``version_at_retry``. Surfaces in the record with empty
        ``needs_retry`` are NOT included in the map.

        ``started_at`` is the record's ``started_at`` ISO string (used
        for the AGE clause of the stale-drop predicate); ``None`` when
        no prior record exists or the field is missing.

        ``record_path`` is the absolute path to the prior record (None
        when no prior record exists). Useful for diagnostics.
    """
    upgrades_dir = os.path.join(world_root_resolved, ".alive", "upgrades")
    if not os.path.isdir(upgrades_dir):
        return {}, None, None
    candidates: List[str] = []
    try:
        for entry in os.listdir(upgrades_dir):
            if _FINAL_RECORD_FILENAME_RE.match(entry):
                candidates.append(entry)
    except OSError:
        # Directory unreadable -- treat as no prior record.
        return {}, None, None
    if not candidates:
        return {}, None, None
    # Filename-timestamp sort: lexicographic == chronological for the
    # canonical ISO-like pattern.
    candidates.sort()
    latest_name = candidates[-1]
    latest_path = os.path.join(upgrades_dir, latest_name)
    try:
        # Local import keeps surfaces/ resilient to a broken codec
        # (it's caught by the IOError/JSON branches below either way).
        from system_upgrade._record_codec import read as read_record
        record = read_record(latest_path)
    except (OSError, ValueError) as exc:  # ValueError covers JSONDecodeError
        # Corrupt or unreadable prior record -- treat as no prior
        # retries. The operator gets a clean run; the record itself
        # remains on disk for forensics. (Don't propagate the failure;
        # the no-op gate must still be allowed to fire for
        # already-current worlds whose only blot is an unreadable
        # historical record.)
        del exc
        return {}, None, latest_path

    # Schema-version gating (codex completion-review fix for R21).
    # An older tool MUST NOT replay retry items from a record whose
    # ``schema_version`` it does not understand -- the items'
    # interpretation is undefined for this version of the tool.
    # Forgiving posture: blank the surfaces section rather than
    # crashing. ``started_at`` and ``record_path`` STILL flow through
    # so the stale-drop predicate (which only consumes ``started_at``)
    # and diagnostics remain truthful that a record exists on disk.
    # An ABSENT ``schema_version`` is treated as legacy and tolerated:
    # records written before the schema_version field existed are
    # still readable so upgrade history pre-R21 isn't invalidated.
    started_at = record.get("started_at") if isinstance(record, dict) else None
    schema_version_unknown = False
    if isinstance(record, dict):
        prior_schema_version = record.get("schema_version")
        if (
            prior_schema_version is not None
            and prior_schema_version not in _SUPPORTED_RECORD_SCHEMA_VERSIONS
        ):
            schema_version_unknown = True
    if schema_version_unknown:
        return (
            {},
            started_at if isinstance(started_at, str) else None,
            latest_path,
        )
    surfaces_section = (
        record.get("surfaces") if isinstance(record, dict) else None
    )
    out: Dict[str, SurfaceRetryRecord] = {}
    if isinstance(surfaces_section, dict):
        for surface_name, surface_entry in surfaces_section.items():
            if not isinstance(surface_entry, dict):
                continue
            items = surface_entry.get("needs_retry") or []
            if not isinstance(items, list) or not items:
                continue
            version_at_retry = surface_entry.get("version_at_retry")
            if not isinstance(version_at_retry, str):
                # Per spec: version_at_retry is REQUIRED whenever
                # needs_retry is non-empty. A record missing it is
                # malformed; surface as no-retry rather than crashing.
                continue
            out[str(surface_name)] = SurfaceRetryRecord(
                name=str(surface_name),
                items=list(items),
                version_at_retry=version_at_retry,
            )
    return out, started_at if isinstance(started_at, str) else None, latest_path


# ---------------------------------------------------------------------------
# Phase 4b: probe all surfaces
# ---------------------------------------------------------------------------


def probe_all(
    surfaces_filter: Optional[Sequence[str]],
) -> List[ProbeResult]:
    """Probe each surface in ``surfaces_filter``.

    ``surfaces_filter`` is the post-``parse_surfaces_filter`` list:

    * ``None`` is the ``--surfaces=none`` sentinel; the orchestrator
      handles that decision OUTSIDE this function (it skips probe
      entirely). Defensively, if ``None`` is passed here, return an
      empty list.
    * Empty list -- nothing probed.
    * Otherwise: each registered surface whose ``name`` is in the
      filter is probed via its ``probe()`` method.

    NEVER raises. Any exception from a surface's ``probe()`` becomes a
    soft-fail ``ProbeError(kind="non_zero_exit")``-style entry.
    """
    if surfaces_filter is None:
        return []
    active_names = {n for n in surfaces_filter}
    results: List[ProbeResult] = []
    for surface in registered_surfaces():
        if surface.name not in active_names:
            continue
        try:
            result = surface.probe()
        except Exception as exc:  # noqa: BLE001 -- surface-level robustness
            # Last-resort soft-fail: the surface broke its own contract.
            # Convert into a parse_error result so the no-op gate
            # treats it as a hard fail.
            result = ProbeResult(
                name=surface.name,
                present=False,
                compatible=False,
                probe_error=ProbeError(
                    kind="parse_error",
                    message=(
                        "surface raised during probe(): {}: {}".format(
                            type(exc).__name__, exc
                        )
                    ),
                ),
            )
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Phase 10: dispatch all surfaces
# ---------------------------------------------------------------------------


def dispatch_all(
    world_root_resolved: str,
    probe_results: Sequence[ProbeResult],
    surface_retry_map: Mapping[str, SurfaceRetryRecord],
    *,
    timeout: float = 60.0,
) -> List[DispatchResult]:
    """Invoke each compatible surface's migrator.

    For each ``ProbeResult`` in ``probe_results``:

    * If ``probe.compatible`` is False or ``probe.migrator_argv_prefix``
      is None: skip dispatch -- emit a ``DispatchResult`` with
      ``status="skipped"`` and ``error_kind`` carrying the reason.
    * Otherwise: invoke the surface's ``dispatch()`` with any retry
      items carried over from ``surface_retry_map``. The surface
      builds the full argv via ``build_dispatch_argv`` and invokes
      the migrator subprocess.

    The orchestrator copies ``probe.version`` to
    ``DispatchResult.version_at_retry`` BEFORE returning so phase-12
    record emission never has to look up the probe again. (This is
    the version that will be recorded with the new run's
    ``needs_retry[]``; the next run uses it for version-mismatch
    stale-drop.)

    NEVER raises. Surface-level exceptions are converted into
    ``status="failed"`` results.
    """
    surface_by_name = {s.name: s for s in registered_surfaces()}
    out: List[DispatchResult] = []
    for probe in probe_results:
        retry_record = surface_retry_map.get(probe.name)
        retry_items = list(retry_record.items) if retry_record else None
        if not probe.compatible or not probe.migrator_argv_prefix:
            # Refuse dispatch on incompatible or contract-violating
            # surfaces. Phase 12's record emission still surfaces
            # carried-over retry items via the stale-drop /
            # carry-forward path; we don't lose them here.
            reason_kind = (
                probe.probe_error.kind
                if probe.probe_error is not None
                else "skipped:incompatible"
            )
            reason_msg = (
                probe.probe_error.message
                if probe.probe_error is not None
                else "probe reported compatible=False"
            )
            out.append(
                DispatchResult(
                    name=probe.name,
                    status="skipped",
                    version_at_retry=probe.version,
                    error_kind=reason_kind,
                    error_message=reason_msg,
                )
            )
            continue
        surface = surface_by_name.get(probe.name)
        if surface is None:
            out.append(
                DispatchResult(
                    name=probe.name,
                    status="skipped",
                    version_at_retry=probe.version,
                    error_kind="missing_surface",
                    error_message=(
                        "surface {!r} not in registry; phase-10 cannot "
                        "dispatch".format(probe.name)
                    ),
                )
            )
            continue
        try:
            result = surface.dispatch(
                world_root_resolved=world_root_resolved,
                retry_items=retry_items,
                timeout=timeout,
                migrator_argv_prefix=probe.migrator_argv_prefix,
            )
        except Exception as exc:  # noqa: BLE001
            result = DispatchResult(
                name=probe.name,
                status="failed",
                version_at_retry=probe.version,
                error_kind="parse_error",
                error_message=(
                    "surface raised during dispatch(): {}: {}".format(
                        type(exc).__name__, exc
                    )
                ),
            )
        # Orchestrator owns version_at_retry -- always overwrite from
        # the probe so the surface can't lie about it.
        result.version_at_retry = probe.version
        out.append(result)
    return out


# ---------------------------------------------------------------------------
# Phase 12: stale-drop + record assembly
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Best-effort ISO-8601 parse; returns None on any failure.

    Crucially, returns None for offset-NAIVE results too -- a record
    like ``{"started_at": "2026-05-04T12:00:00"}`` (no ``Z``, no
    ``+00:00``) parses to a naive datetime, and subtracting it from
    an aware ``now`` would raise ``TypeError`` deep inside
    ``apply_stale_drop``. That's a contract break: the rest of the
    code is explicitly tolerant of corrupt/unreadable prior records,
    so a naive timestamp must degrade cleanly to "no age check".
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        # ``datetime.fromisoformat`` accepts ``YYYY-MM-DDTHH:MM:SS+00:00``
        # since Python 3.7+. Filename-pattern timestamps use ``-`` for
        # the time separator; that's the FILENAME-safe variant, NOT the
        # value stored in started_at, which keeps colons.
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # Offset-naive -- refuse rather than crash later.
        return None
    return parsed


def apply_stale_drop(
    surface_retry_map: Mapping[str, SurfaceRetryRecord],
    *,
    prior_started_at: Optional[str],
    probe_results: Optional[Sequence[ProbeResult]],
    now: Optional[datetime] = None,
    age_days: int = STALE_RETRY_AGE_DAYS,
) -> Tuple[Dict[str, SurfaceRetryRecord], List[Dict[str, Any]], List[str]]:
    """Apply the AGE + version-mismatch stale-drop predicate.

    Per round 8 M8:

    * The AGE clause (a) drops a surface when
      ``now - prior_started_at > age_days``. Applies under all run
      modes including ``--surfaces=none``.
    * The version-mismatch clause (b) drops a surface when the current
      probe's version differs from the record's ``version_at_retry``.
      CANNOT run under ``--surfaces=none`` (no probe was performed) --
      the function returns the surface unchanged in that case and
      appends its name to ``version_mismatch_check_skipped``.

    Returns
    -------
    (surviving_map, stale_retry_dropped, version_mismatch_check_skipped)
        ``surviving_map`` is the post-drop ``SurfaceRetryRecord`` dict.
        ``stale_retry_dropped`` is a list of ``{surface, reason, from,
        to}`` dicts ready for inclusion in the upgrade record.
        ``version_mismatch_check_skipped`` lists surfaces whose
        version-mismatch check was deferred because probe was skipped.
    """
    now_dt = now or datetime.now(timezone.utc)
    prior_dt = _parse_iso_timestamp(prior_started_at)
    # Use full timedelta comparison rather than ``.days`` so a record
    # that is 7 days + 1 hour old is treated as "older than 7 days".
    # ``timedelta.days`` floors the fractional component and would
    # silently keep records up to ~8 days.
    age_exceeded = (
        prior_dt is not None
        and (now_dt - prior_dt) > timedelta(days=age_days)
    )

    # Build a {name: version} index from probe_results when available.
    version_by_name: Dict[str, Optional[str]] = {}
    if probe_results is not None:
        for p in probe_results:
            version_by_name[p.name] = p.version

    surviving: Dict[str, SurfaceRetryRecord] = {}
    dropped: List[Dict[str, Any]] = []
    version_check_skipped: List[str] = []

    for name, record in surface_retry_map.items():
        # AGE clause -- universal.
        if age_exceeded:
            dropped.append(
                {
                    "surface": name,
                    "reason": "age",
                    "from": record.version_at_retry,
                    "to": version_by_name.get(name),
                }
            )
            continue
        # Version-mismatch clause -- only when probe ran.
        if probe_results is None:
            # --surfaces=none: defer the check; carry the entry forward.
            version_check_skipped.append(name)
            surviving[name] = record
            continue
        current_version = version_by_name.get(name)
        if current_version is None:
            # Surface not probed (filter excluded it) but we have a
            # retry record. Carry forward unchanged; defer to a later
            # run that probes it.
            version_check_skipped.append(name)
            surviving[name] = record
            continue
        if current_version != record.version_at_retry:
            dropped.append(
                {
                    "surface": name,
                    "reason": "version_mismatch",
                    "from": record.version_at_retry,
                    "to": current_version,
                }
            )
            continue
        surviving[name] = record
    return surviving, dropped, version_check_skipped


def build_surfaces_record_section(
    *,
    probe_results: Optional[Sequence[ProbeResult]],
    dispatch_results: Optional[Sequence[DispatchResult]],
    carried_forward: Mapping[str, SurfaceRetryRecord],
    version_mismatch_check_skipped: Sequence[str],
    surfaces_none: bool,
) -> Dict[str, Any]:
    """Assemble the upgrade record's ``surfaces`` mapping (phase 12).

    Combines three sources:

    * ``probe_results`` -- per-surface probe info (version,
      compatible, state_paths, probe_error, probe_stdout family).
      ``None`` under ``--surfaces=none``.
    * ``dispatch_results`` -- per-surface dispatch outcome (status,
      completed, needs_retry, errors, dispatch_stdout family).
      ``None`` under ``--surfaces=none``.
    * ``carried_forward`` -- surface_retry_map entries that survived
      the stale-drop predicate. Under ``--surfaces=none`` these are
      copied verbatim into the new record (preserving original
      ``version_at_retry``); under a normal run, these are merged
      back into the surface's record alongside fresh dispatch
      output (the migrator may have re-attempted them).

    Output shape per surface (when present):

        ``{
            "status": <str>,
            "version": <str|None>,
            "version_at_retry": <str|None>,    # only when needs_retry
            "completed": [...],
            "errors": [...],
            "needs_retry": [...],
            "state_paths": [...],
            "compatible": <bool>,
            "probe_stdout": <str>,
            "probe_stdout_bytes": <int>,
            "probe_stdout_truncated": <bool>,
            "probe_stderr": ...,
            "dispatch_stdout": ...,
            "dispatch_argv": [...],
            "dispatch_skipped": <bool>,        # under --surfaces=none
            "version_mismatch_check_skipped": <bool>,
        }``
    """
    out: Dict[str, Any] = {}
    if surfaces_none:
        # --surfaces=none: emit one entry per carried-forward surface;
        # no probe / dispatch info.
        for name, record in carried_forward.items():
            entry: Dict[str, Any] = {
                "status": "skipped",
                "version": None,
                "version_at_retry": record.version_at_retry,
                "needs_retry": list(record.items),
                "completed": [],
                "errors": [],
                "state_paths": [],
                "dispatch_skipped": True,
                "version_mismatch_check_skipped": True,
            }
            out[name] = entry
        return out

    # Probed run: assemble from probe_results + dispatch_results.
    probe_by_name: Dict[str, ProbeResult] = {}
    if probe_results is not None:
        for p in probe_results:
            probe_by_name[p.name] = p
    dispatch_by_name: Dict[str, DispatchResult] = {}
    if dispatch_results is not None:
        for d in dispatch_results:
            dispatch_by_name[d.name] = d

    union_names = set(probe_by_name.keys()) | set(dispatch_by_name.keys())
    union_names.update(carried_forward.keys())

    version_skip_set = set(version_mismatch_check_skipped)

    for name in sorted(union_names):
        probe = probe_by_name.get(name)
        disp = dispatch_by_name.get(name)
        carry = carried_forward.get(name)
        entry: Dict[str, Any] = {}
        if disp is not None:
            entry["status"] = disp.status
            entry["completed"] = list(disp.completed)
            entry["errors"] = list(disp.errors)
            entry["needs_retry"] = list(disp.needs_retry)
            entry["dispatch_argv"] = list(disp.argv)
            entry["dispatch_stdout"] = disp.dispatch_stdout
            entry["dispatch_stdout_bytes"] = disp.dispatch_stdout_bytes
            entry["dispatch_stdout_truncated"] = disp.dispatch_stdout_truncated
            entry["dispatch_stderr"] = disp.dispatch_stderr
            entry["dispatch_stderr_bytes"] = disp.dispatch_stderr_bytes
            entry["dispatch_stderr_truncated"] = disp.dispatch_stderr_truncated
            if disp.error_kind:
                entry["dispatch_error_kind"] = disp.error_kind
                entry["dispatch_error_message"] = disp.error_message
        else:
            entry["status"] = "skipped"
            entry["completed"] = []
            entry["errors"] = []
            entry["needs_retry"] = []
        # Probe-side fields (always present when probe ran).
        if probe is not None:
            entry["version"] = probe.version
            entry["compatible"] = probe.compatible
            entry["state_paths"] = list(probe.state_paths)
            # The forensic stdout/stderr triple now lives on the
            # composed ``probe_subprocess`` object (None when no
            # subprocess ran -- e.g. Hermes / Codex stubs); fall back
            # to empty defaults that match the legacy field shape so
            # the recorded entry is byte-identical to the pre-trim
            # output.
            sub = probe.probe_subprocess
            entry["probe_stdout"] = sub.stdout if sub is not None else ""
            entry["probe_stdout_bytes"] = (
                sub.stdout_bytes if sub is not None else 0
            )
            entry["probe_stdout_truncated"] = (
                sub.stdout_truncated if sub is not None else False
            )
            entry["probe_stderr"] = sub.stderr if sub is not None else ""
            entry["probe_stderr_bytes"] = (
                sub.stderr_bytes if sub is not None else 0
            )
            entry["probe_stderr_truncated"] = (
                sub.stderr_truncated if sub is not None else False
            )
            if probe.probe_error is not None:
                entry["probe_error_kind"] = probe.probe_error.kind
                entry["probe_error_message"] = probe.probe_error.message
        else:
            entry["version"] = None
            entry["compatible"] = False
            entry["state_paths"] = []
        # Carry-forward merge: when this surface had retries on prior
        # run AND dispatch did NOT produce authoritative replacement
        # retry state, preserve the prior items so they survive to
        # the next run. "No authoritative replacement" covers four
        # cases: dispatch was absent (None), dispatch was skipped
        # (incompatible / filtered-out), dispatch FAILED at the
        # orchestrator level (timeout, parse error, missing binary,
        # non-zero exit -- the migrator never got a chance to declare
        # what the new retry set is), or dispatch returned an empty
        # ``needs_retry`` together with one of those failure modes.
        # Only ``status="ok"`` and ``status="partial"`` produce
        # authoritative retry state from the migrator itself.
        dispatch_authoritative = (
            disp is not None and disp.status in ("ok", "partial")
        )
        if (
            carry is not None
            and not dispatch_authoritative
            and not entry["needs_retry"]
        ):
            entry["needs_retry"] = list(carry.items)
            entry["version_at_retry"] = carry.version_at_retry
            entry["dispatch_skipped"] = True
        # When the dispatch produced retries OR carry survived, emit
        # version_at_retry. Per-surface schema requires the field
        # whenever needs_retry is non-empty.
        if entry.get("needs_retry"):
            if "version_at_retry" not in entry:
                # Fresh retries from this run: use the probed version.
                entry["version_at_retry"] = (
                    probe.version if probe is not None else None
                )
        # Mark version-mismatch-check-skipped surfaces (carried over
        # from prior runs but probe didn't run for them this run --
        # rare in a probed run, common under partial filters).
        if name in version_skip_set:
            entry["version_mismatch_check_skipped"] = True
        out[name] = entry
    return out
