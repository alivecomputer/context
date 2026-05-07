"""Resume marker state primitives for ``alive system-upgrade`` (T6 of fn-18).

Owns the ``Step`` enum (the locked 13-phase order from the epic spec),
``StepStatus``, and the ``ResumeMarker`` dataclass plus its serialization
contract.

Why this module exists
----------------------
The orchestrator (``orchestrator.py``) carries phase entry-point names
as Python identifiers (``phase_snapshot``, ``phase_detect``, ...) for
dispatch. The resume-marker layer needs a *stable* string identifier
per phase that survives orchestrator-side renames and refactors -- T6
uses ``Step.name`` (e.g. ``"DETECT"``, ``"NOOP_SHORT_CIRCUIT"``) as the
cross-run wire format. Persisted markers therefore round-trip through
``Step[<name>]`` lookups, which fail loud the moment a future refactor
silently drops or renames a step.

The 13-step order mirrors the locked ``orchestrator.PHASE_NAMES``
list. Phase 5 is the ``NOOP_SHORT_CIRCUIT`` gate; on resume from this
step we MUST re-run detection + probe + gate evaluation fresh per the
practice-scout note in the spec ("never trust the marker's gate
decision"). The only state that crosses the resume boundary from this
step is "resume here", not "the gate said X last time".

Stdlib-only (R10): no PyYAML / ruamel.
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


__all__ = (
    "MARKER_SCHEMA_VERSION",
    "MARKER_SUBDIR",
    "MARKER_SUFFIX",
    "ResumeMarker",
    "Step",
    "StepStatus",
    "parse_aware_iso",
    "step_after",
)


def parse_aware_iso(value: str) -> datetime:
    """Parse an ISO 8601 timestamp string, requiring an explicit UTC offset.

    The marker schema records timestamps as UTC ISO 8601 with a
    trailing ``Z`` (e.g. ``"2026-05-04T01:23:45Z"``). We accept the
    explicit ``+00:00`` form too -- the rest of the upgrade-record
    toolchain emits ``Z``, but a future emitter that switches form
    should still validate. Naive timestamps (no offset) are REJECTED
    because:

    * Resume math (``_staleness_hours``) compares against ``iso_now()``
      which is always offset-aware. Subtracting a naive datetime from
      an aware one raises ``TypeError`` in Python -- a malformed
      marker would crash the validator instead of surfacing as a
      structured ``resume_marker_unreadable`` refusal.

    * The skew check trusts ``tool_version_at_run`` to identify the
      run; if the timestamps are naive, "halted" semantics are
      ambiguous (local? UTC? converted?). Resume cannot reason about
      staleness against an unknown clock.

    Raises
    ------
    ValueError
        On unparseable input or naive (offset-less) timestamps.
    """
    if not isinstance(value, str):
        raise ValueError(
            "ISO timestamp must be a string (got {})".format(
                type(value).__name__,
            )
        )
    if not value:
        raise ValueError("ISO timestamp is empty")
    raw = value
    # Python's ``fromisoformat`` accepts ``+00:00`` but did not accept
    # ``Z`` until 3.11. Translate for portability across the Python
    # versions the plugin targets (3.10+).
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            "{!r} is not a parseable ISO 8601 timestamp: {}".format(value, exc)
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            "{!r} is a naive ISO timestamp (no UTC offset); "
            "resume math requires offset-aware timestamps so a Z or "
            "+00:00 suffix is mandatory".format(value)
        )
    return parsed


#: Schema version persisted on every marker. Bumped on incompatible
#: layout changes; consumers compare via integer equality.
MARKER_SCHEMA_VERSION: int = 1

#: Marker files live under ``<world>/.alive/upgrades/`` per epic spec
#: ("Codex M5 standardized path"). Uses the same directory the no-op
#: record writer + final upgrade record share so a single retention
#: sweep covers both.
MARKER_SUBDIR: str = os.path.join(".alive", "upgrades")

#: Filename suffix for marker files. The full filename pattern is
#: ``<filename-safe-iso>-resume.yaml`` (colons replaced with hyphens
#: for cross-platform safety -- mirrors the pattern used by the no-op
#: record writer in ``orchestrator.write_noop_record_to_world``).
MARKER_SUFFIX: str = "-resume.yaml"


# ---------------------------------------------------------------------------
# Step enum -- locked 13-phase order
# ---------------------------------------------------------------------------

class Step(enum.Enum):
    """The 13 phases of the upgrade pipeline, in dispatch order.

    Names are the wire format for marker persistence. ``Step[name]``
    lookups (used during resume) fail loud when a future orchestrator
    refactor renames or drops one of these entries -- which is exactly
    the contract guard the spec asks for ("Step enum stable: rename
    test ensures ``Step['DETECT']`` and ``Step['NOOP_SHORT_CIRCUIT']``
    resolve regardless of internal ordering refactors"; T14 enforces).

    Ordering ("phase number") is captured by ``Step.phase_number``
    (1-indexed) and ``step_after(step)``; do not rely on
    ``list(Step).index(step)`` because Python guarantees enum *member*
    order matches definition order, but a future refactor that adds a
    new phase at position N silently shifts every later phase by 1
    when read via ``index()``. The explicit numbering carried in the
    enum value is the load-bearing contract.

    Phase 5 (``NOOP_SHORT_CIRCUIT``) is non-mutating except for the
    no-op record write at the end of the gate-pass branch. On resume
    from this step, the caller MUST re-run detection + probe + gate
    evaluation; the marker's gate decision is NOT trusted (gap
    practice-scout: re-detect on resume).
    """

    PREFLIGHT = 1            # phase 1
    SNAPSHOT = 2             # phase 2
    DETECT = 3               # phase 3 (carries walkthrough_eligible_matches[])
    PROBE_SURFACES = 4       # phase 4
    NOOP_SHORT_CIRCUIT = 5   # phase 5 -- gate; on pass: emit no-op record
    BACKUP = 6               # phase 6
    WALKTHROUGH_DECIDE = 7   # phase 7
    PLUGIN_CLEANUP = 8       # phase 8
    PLUGIN_MIGRATE = 9       # phase 9
    SURFACE_DISPATCH = 10    # phase 10
    VERIFY = 11              # phase 11
    RECORD = 12              # phase 12
    RELEASE_LOCK = 13        # phase 13

    @property
    def phase_number(self) -> int:
        """1-indexed phase number; mirrors orchestrator.PHASE_NUMBERS."""
        return int(self.value)


def step_after(step: "Step") -> Optional["Step"]:
    """Return the next step in the locked dispatch order, or None at the end.

    ``RELEASE_LOCK`` (phase 13) returns None -- there is no step after
    release. Callers that resume after a fully-completed run treat
    ``step_after(RELEASE_LOCK) is None`` as the signal "nothing to do".
    """
    next_value = step.value + 1
    for candidate in Step:
        if candidate.value == next_value:
            return candidate
    return None


# ---------------------------------------------------------------------------
# StepStatus
# ---------------------------------------------------------------------------

class StepStatus(enum.Enum):
    """Per-step status as observed at marker write time.

    The marker carries a list of ``completed_ops`` (steps that finished
    successfully -- their fsync landed) and a single ``last_step``
    (the step that was running at halt). ``StepStatus`` is the wire
    label for the latter; ``completed_ops`` entries are always
    implicitly ``COMPLETED`` (they would not be in the list otherwise).

    * ``RUNNING``  -- step started, no terminal state observed yet.
                       Marker write happens BEFORE step body runs so a
                       crash mid-step lands here.
    * ``COMPLETED`` -- step body ran to completion AND the destructive
                       fsync (if any) landed. Crash AFTER fsync but
                       BEFORE marker append still leaves the prior
                       marker state on disk -- gap practice-scout: the
                       resume marker is the LAST write per step, never
                       advance the checkpoint before the destructive op
                       fsyncs.
    * ``FAILED``   -- step raised; ``last_error`` carries the summary.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# ResumeMarker dataclass
# ---------------------------------------------------------------------------

@dataclass
class ResumeMarker:
    """Structured snapshot of an in-flight upgrade run.

    Written once per step transition (state.write_marker -> _record_codec).
    Read at ``--resume`` start to validate plugin-version equivalence,
    world-fingerprint equivalence, and staleness. See ``resume.py`` for
    the validation chain.

    Fields
    ------
    schema_version : int
        Layout version. Equals :data:`MARKER_SCHEMA_VERSION` for fresh
        markers.
    started_iso : str
        Run start timestamp (UTC ISO 8601). Matches the lock-meta
        sidecar's ``started_iso``.
    halted_iso : str
        Most recent marker-write timestamp; doubles as the staleness
        anchor (``--resume-staleness <hours>`` compares against this).
    tool_version_at_run : str
        Plugin version captured by ``tool_version.read_tool_version``
        at run start. Hard-refused on skew at resume time -- ``--force``
        does NOT bypass.
    world_fingerprint_at_start : dict
        ``DetectionReport.all_signals_raw`` from phase 3 at run start.
        Resume re-runs detection + probes against the live world and
        diffs; divergence is soft-refused (``--force`` bypasses).
    planned_ops : list[str]
        Step names (``Step.name``) the run intends to execute. Captured
        once at the START of the run and never mutated; the diff
        between this and ``completed_ops`` tells the operator how
        much work remains.
    completed_ops : list[str]
        Step names that finished AND fsynced (per-step destructive
        operation, where applicable). Append-only between marker
        writes. Resume picks up at ``step_after(Step[completed_ops[-1]])``.
    last_step : str | None
        Name of the step that was running at halt, or None when the
        marker was written BEFORE any step body ran (e.g. PREFLIGHT
        marker prelude). Distinct from ``completed_ops`` because a
        running step is NOT counted as completed.
    last_status : str
        StepStatus value (``running`` / ``completed`` / ``failed``).
        ``last_step is None`` implies the run hasn't started any step
        yet; we still persist a status string for ergonomic JSON.
    last_error : str | None
        One-line summary of the exception that halted the run, when
        applicable. Populated only on ``last_status == "failed"``.
    """

    schema_version: int = MARKER_SCHEMA_VERSION
    started_iso: str = ""
    halted_iso: str = ""
    tool_version_at_run: str = ""
    world_fingerprint_at_start: Dict[str, Any] = field(default_factory=dict)
    planned_ops: List[str] = field(default_factory=list)
    completed_ops: List[str] = field(default_factory=list)
    last_step: Optional[str] = None
    last_status: str = StepStatus.RUNNING.value
    last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return the canonical dict form for ``_record_codec.write_atomic``.

        Output is a plain dict tree (lists / strings / ints / dicts);
        no enum instances leak through. Keys are sorted by
        ``json.dumps(sort_keys=True)`` at write time, so the in-memory
        order does not affect on-disk byte layout.
        """
        return {
            "schema_version": int(self.schema_version),
            "started_iso": self.started_iso,
            "halted_iso": self.halted_iso,
            "tool_version_at_run": self.tool_version_at_run,
            "world_fingerprint_at_start": dict(self.world_fingerprint_at_start),
            "planned_ops": list(self.planned_ops),
            "completed_ops": list(self.completed_ops),
            "last_step": self.last_step,
            "last_status": self.last_status,
            "last_error": self.last_error,
        }

    #: Schema-required keys -- their absence is a hard parse-failure.
    #: ``last_step`` / ``last_error`` / ``last_status`` are explicitly
    #: optional (a marker written before any step body has run carries
    #: ``last_step is None`` legitimately; ``last_status`` defaults to
    #: ``RUNNING``).
    _REQUIRED_KEYS: tuple = (
        "started_iso",
        "halted_iso",
        "tool_version_at_run",
        "world_fingerprint_at_start",
        "planned_ops",
        "completed_ops",
    )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResumeMarker":
        """Re-hydrate a marker from a parsed JSON-as-YAML dict.

        Schema-required fields (``started_iso``, ``halted_iso``,
        ``tool_version_at_run``, ``world_fingerprint_at_start``,
        ``planned_ops``, ``completed_ops``) MUST be present; their
        absence is a parse-failure. ``schema_version`` is also required
        but has a single-version fallback (we surface a clear shape
        error if the value is not int-coercible). Optional fields:
        ``last_step``, ``last_error`` (both nullable) and
        ``last_status`` (defaults to ``RUNNING`` when omitted).

        Empty strings for the required ISO / version fields are
        rejected -- a marker without a real ``started_iso`` cannot
        anchor staleness math, and empty ``tool_version_at_run`` would
        let the skew check silently pass on legitimately-corrupt
        markers. The resume validator catches the resulting
        ``ValueError`` and surfaces a structured
        ``ResumeRefusal(code="resume_marker_unreadable")`` -- a
        permissive ``from_dict`` would let a half-written `{}` marker
        pretend to be a valid fresh run.

        Unknown keys are IGNORED rather than rejected so future T6+1
        fields can be added without breaking T6's reader.

        ``planned_ops`` / ``completed_ops`` are validated for shape
        (must be ``list[str]``); their step names are NOT cross-checked
        against the ``Step`` enum here (that is ``resume.py``'s job at
        validation time, where the failure mode is a structured
        skew-refusal rather than a parse error).

        Raises
        ------
        ValueError
            On missing required fields, wrong types, or empty strings
            where a real value is required. The resume validator
            catches and translates to ``resume_marker_unreadable``.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "marker payload is not a dict: got {}".format(type(data).__name__)
            )

        # Required-key presence check up front -- a single missing
        # required key is a parse failure regardless of what the
        # other fields look like.
        missing = [k for k in cls._REQUIRED_KEYS if k not in data]
        if missing:
            raise ValueError(
                "marker missing required field(s): {}".format(
                    ", ".join(sorted(missing))
                )
            )

        def _as_str_list(key: str) -> List[str]:
            raw = data[key]
            if not isinstance(raw, list):
                raise ValueError(
                    "marker.{} is not a list (got {})".format(
                        key, type(raw).__name__,
                    )
                )
            out: List[str] = []
            for entry in raw:
                if not isinstance(entry, str):
                    raise ValueError(
                        "marker.{} contains a non-string entry: {!r}".format(
                            key, entry,
                        )
                    )
                out.append(entry)
            return out

        def _as_str_or_none(key: str) -> Optional[str]:
            raw = data.get(key)
            if raw is None:
                return None
            if not isinstance(raw, str):
                raise ValueError(
                    "marker.{} is not a string (got {})".format(
                        key, type(raw).__name__,
                    )
                )
            return raw

        def _as_required_nonempty_str(key: str) -> str:
            raw = data[key]
            if not isinstance(raw, str):
                raise ValueError(
                    "marker.{} is not a string (got {})".format(
                        key, type(raw).__name__,
                    )
                )
            if not raw:
                raise ValueError(
                    "marker.{} is empty; required for resume validation".format(key)
                )
            return raw

        def _as_optional_str(key: str, default: str) -> str:
            raw = data.get(key, default)
            if not isinstance(raw, str):
                raise ValueError(
                    "marker.{} is not a string (got {})".format(
                        key, type(raw).__name__,
                    )
                )
            return raw

        schema_raw = data.get("schema_version", MARKER_SCHEMA_VERSION)
        try:
            schema_version = int(schema_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "marker.schema_version is not an int-coercible value: {!r}"
                .format(schema_raw)
            ) from exc

        fingerprint = data["world_fingerprint_at_start"]
        if not isinstance(fingerprint, dict):
            raise ValueError(
                "marker.world_fingerprint_at_start is not a dict (got {})"
                .format(type(fingerprint).__name__)
            )

        # Validate the two timestamp fields up front -- bad ISO values
        # would otherwise reach ``_staleness_hours`` and either return
        # ``None`` (silently skipping staleness check) or raise
        # ``TypeError`` on naive-vs-aware subtraction. We surface the
        # corruption here so the caller's ``load_marker`` translates
        # to ``ResumeRefusal(code="resume_marker_unreadable")``.
        started_iso = _as_required_nonempty_str("started_iso")
        halted_iso = _as_required_nonempty_str("halted_iso")
        try:
            parse_aware_iso(started_iso)
        except ValueError as exc:
            raise ValueError(
                "marker.started_iso is not a valid ISO 8601 timestamp: {}".format(exc)
            ) from exc
        try:
            parse_aware_iso(halted_iso)
        except ValueError as exc:
            raise ValueError(
                "marker.halted_iso is not a valid ISO 8601 timestamp: {}".format(exc)
            ) from exc

        return cls(
            schema_version=schema_version,
            started_iso=started_iso,
            halted_iso=halted_iso,
            tool_version_at_run=_as_required_nonempty_str("tool_version_at_run"),
            world_fingerprint_at_start=dict(fingerprint),
            planned_ops=_as_str_list("planned_ops"),
            completed_ops=_as_str_list("completed_ops"),
            last_step=_as_str_or_none("last_step"),
            last_status=_as_optional_str(
                "last_status", default=StepStatus.RUNNING.value,
            ),
            last_error=_as_str_or_none("last_error"),
        )

    # ------------------------------------------------------------------
    # Resume-target helpers
    # ------------------------------------------------------------------

    def resume_from_step(self) -> Optional[Step]:
        """Return the Step the resume run should pick up from.

        Decision tree:
            * ``completed_ops`` empty AND ``last_step is None``
                -> resume from the first step (PREFLIGHT). Equivalent
                to running fresh.
            * ``completed_ops`` non-empty AND last completed step is
                ``NOOP_SHORT_CIRCUIT``
                -> resume from ``NOOP_SHORT_CIRCUIT`` itself. The gate
                step is non-mutating (modulo the no-op record write
                that ends the gate-pass branch); per the spec the
                resumed run MUST re-run detection + probe + gate
                evaluation fresh rather than trusting the marker's
                gate decision (gap practice-scout: re-detect on
                resume). Advancing to ``BACKUP`` here would silently
                skip the gate re-evaluation and start destructive
                work against a possibly-changed world.
            * ``completed_ops`` non-empty (any other step)
                -> resume from ``step_after(Step[completed_ops[-1]])``.
                Returns None when the last completed step is
                ``RELEASE_LOCK`` (the run was already done).
            * ``completed_ops`` empty BUT ``last_step`` set
                -> resume from ``Step[last_step]`` (re-run the step
                that was running at halt, since it never reached
                COMPLETED).

        Raises
        ------
        ValueError
            ``completed_ops`` or ``last_step`` carries a name not in
            the ``Step`` enum -- caller surfaces as a structured
            refusal in resume.py.
        """
        if self.completed_ops:
            last_name = self.completed_ops[-1]
            try:
                last_step = Step[last_name]
            except KeyError as exc:
                raise ValueError(
                    "marker.completed_ops[-1]={!r} is not a known Step name "
                    "(orchestrator step renamed?). Run a fresh upgrade."
                    .format(last_name)
                ) from exc
            # Special case: the no-op short-circuit gate is the one
            # step where "I completed it" does NOT imply "advance to
            # the next step". The gate's decision is run-bound and
            # MUST be re-evaluated against the live world on resume.
            # See class docstring on ``Step.NOOP_SHORT_CIRCUIT``.
            if last_step is Step.NOOP_SHORT_CIRCUIT:
                return Step.NOOP_SHORT_CIRCUIT
            return step_after(last_step)

        if self.last_step is not None:
            try:
                return Step[self.last_step]
            except KeyError as exc:
                raise ValueError(
                    "marker.last_step={!r} is not a known Step name "
                    "(orchestrator step renamed?). Run a fresh upgrade."
                    .format(self.last_step)
                ) from exc

        # Both empty -- caller resumes from the first step.
        return Step.PREFLIGHT
