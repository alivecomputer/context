"""Coherence invariant checker -- the unified facade over per-stage validators.

This module is the **stage-local** entry point each Stage 0/2/3/4
dispatcher calls after writing its outputs to disk. It is deliberately
**stdlib-only** -- no `jsonschema`, no `pyyaml`, no third-party imports.
That posture is locked at the epic level (see
`.flow/specs/fn-2-2zz.md` § "Why stdlib-only validation") and matches
the rest of `_common.py`.

Architecture: this module does NOT re-implement the structural and
cross-reference checks that already live in
`stages/stage0.preflight_spine`, `stages/stage2.validate_entity_outputs`,
`stages/stage3.validate_timeline`, and `stages/stage4.validate_insights`.
Those validators are exhaustive within a single stage's outputs.
`validate.py` adds three concerns the per-stage validators intentionally
defer:

1. **Result normalisation** -- each per-stage validator returns its own
   shape (Stage 0 raises, Stages 2/3/4 return findings lists). This
   module unifies them under a single ``ValidationResult`` dataclass-like
   envelope with stable ``status`` + ``errors`` + ``warnings`` fields.

2. **Cross-stage cross-references** -- the per-stage validators are
   scoped to one stage's outputs and cannot resolve refs that point at
   another stage's artefacts. The big one is **Stage 4 citation
   RESOLUTION**: every insight bullet's ``(YYYY-MM-DD, squirrel:<8-hex>)``
   citation must point at a real log entry in the Stage 3 outputs.
   Stage 4's own validator only checks FORMAT (per its docstring at
   `stages/stage4.py:1135`, "Citation RESOLUTION (does the cited entry
   actually exist in the Stage 3 logs?) is the fn-2-2zz.10 validator's
   job; this stage validates only FORMAT"). That resolution lives here.

3. **Failure classification + retry feedback** -- per-stage validators
   emit raw findings. This module classifies failures into ``fatal``
   (structural / schema_version / re-implementation needed) versus
   ``retryable`` (cross-ref errors a stage subagent can fix with
   feedback context) and renders a truncated retry-feedback block the
   per-stage dispatcher's existing ``retry_dispatch`` helpers consume.

The retry LOOP itself is owned by each stage dispatcher (per spec:
"validate.py does NOT manage the retry loop itself. Each stage
dispatcher already has a retry_dispatch entry point"). This module
classifies; the dispatchers decide.

Public surface
--------------

* ``ValidationResult`` -- the unified envelope.
* ``validate_stage(stage_id, partial_dir)`` -- dispatch entry point.
* ``format_retry_feedback(result, max_chars)`` -- truncated feedback
  block for the retry prompt.
* ``three_option_surface_block(result)`` -- bordered-block rendering for
  the second-failure ``AskUserQuestion`` surface.

Stage IDs
---------

Stage 1 is UX-only (anchor confirmation; no LLM, no validation needed).
``validate_stage`` accepts ``"0"``, ``"2"``, ``"3"``, ``"4"`` only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Path bootstrap -- mirror state.py / stage*.py loader pattern.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = _HERE
_PLUGIN_ROOT = os.path.normpath(os.path.join(_DEMO_DIR, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
_STAGES_DIR = os.path.join(_DEMO_DIR, "stages")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _load_stage(stage_id: str):
    """Load stages/stage{stage_id}.py via importlib under a namespaced key.

    ``alive_demo.stage0`` etc. -- avoids polluting top-level ``sys.modules``
    with generic ``stage0`` keys that could collide if another plugin
    ships its own stage modules.
    """
    full_name = f"alive_demo.stage{stage_id}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = os.path.join(_STAGES_DIR, f"stage{stage_id}.py")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Stage IDs validate_stage accepts. Stage 1 is UX-only (anchor
#: confirmation) and has no LLM-driven artefact to validate.
SUPPORTED_STAGES = ("0", "2", "3", "4")

#: Expected schema_version per stage output. Each stage stamps this on
#: its primary artefact (spine.json / stage{N}_done.json) and the
#: validator rejects mismatches with a fatal error.
EXPECTED_SCHEMA_VERSION = "0.1"

#: Stage 0 produces spine.json directly (no done marker yet during
#: pre-flight). The other stages produce a done marker that carries
#: schema_version. We check both shapes.
_STAGE_SCHEMA_TARGETS: Dict[str, Tuple[str, ...]] = {
    "0": ("spine.json",),
    "2": ("stage2_done.json",),
    "3": ("stage3_done.json",),
    "4": ("stage4_done.json",),
}

#: Statuses ValidationResult can carry.
_STATUS_OK = "ok"
_STATUS_RETRYABLE = "retryable"
_STATUS_FATAL = "fatal"

#: Issue codes the validator classifies as ``fatal`` (re-dispatch will
#: not fix them; the human must intervene). Anything else is retryable.
_FATAL_ISSUE_CODES = frozenset({
    "schema_version_mismatch",
    "schema_version_unreadable",
    "spine_unparseable",
    "anchors_unparseable",
    "stage_marker_unparseable",
    "stage_marker_not_frozen",
    "structural_pre_flight_failed",  # spine.preflight_spine raised
    "internal_error",
})


# ---------------------------------------------------------------------------
# Citation regex (mirrors stage4.CITATION_RE -- duplicated here so this
# module can run without importing stage4 transitively when only Stage 0
# or 2 validation is requested. Stage 4 validation reaches in for the
# full stage4 module anyway.)
# ---------------------------------------------------------------------------

_CITATION_RE = re.compile(
    r"\("
    r"(?P<body>"
    r"\d{4}-\d{2}-\d{2}, squirrel:[a-f0-9]{8}"
    r"(?:; \d{4}-\d{2}-\d{2}, squirrel:[a-f0-9]{8})*"
    r")"
    r"\)"
)
_CITATION_PAIR_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}), squirrel:(?P<sid>[a-f0-9]{8})$"
)
_BULLET_LINE_RE = re.compile(r"^\s*-\s+\S", re.MULTILINE)
_SECTION_HEADING_RE = re.compile(r"(?m)^##\s+(?P<title>.+?)\s*$")
_LOG_ENTRY_HEADER_RE = re.compile(
    r"^##\s+(?P<date>\S+)\s+--\s+squirrel:(?P<sid>[0-9a-f]{16})\s*$",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# ValidationResult envelope
# ---------------------------------------------------------------------------

class ValidationResult:
    """Unified result envelope for stage-local validation.

    Shape::

        {
          "status":   "ok" | "retryable" | "fatal",
          "stage":    "0" | "2" | "3" | "4",
          "errors":   [{"code": str, "where": str, "evidence": str}, ...],
          "warnings": [{"code": str, "where": str, "evidence": str}, ...],
        }

    Construct from per-stage finding lists via :meth:`from_findings` or
    fresh-build via the plain constructor.
    """

    __slots__ = ("status", "stage", "errors", "warnings")

    def __init__(
        self,
        *,
        status: str,
        stage: str,
        errors: Optional[List[Dict[str, Any]]] = None,
        warnings: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if status not in (_STATUS_OK, _STATUS_RETRYABLE, _STATUS_FATAL):
            raise ValueError(
                f"status must be one of ok/retryable/fatal; got {status!r}"
            )
        self.status = status
        self.stage = stage
        self.errors = list(errors or [])
        self.warnings = list(warnings or [])

    def is_ok(self) -> bool:
        """True iff there are zero errors."""
        return self.status == _STATUS_OK and not self.errors

    def is_fatal(self) -> bool:
        return self.status == _STATUS_FATAL

    def is_retryable(self) -> bool:
        return self.status == _STATUS_RETRYABLE

    def to_json(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }

    def format_block(self) -> str:
        """Render a bordered block summarising the result.

        Used by the CLI surface (``alive demo validate``) and by the
        skill router when surfacing the result inline.
        """
        title = f"validate stage {self.stage}: {self.status}"
        lines: List[str] = [f"╭─ 🐿️ {title}"]
        if self.is_ok():
            lines.append("│  no errors, no warnings")
            if not self.warnings:
                lines.append("╰─")
                return "\n".join(lines)
        if self.errors:
            lines.append(f"│  {len(self.errors)} error(s)")
            for err in self.errors[:8]:
                code = err.get("code", "?")
                where = err.get("where", "?")
                evidence = err.get("evidence", "")
                trimmed = evidence if len(evidence) <= 120 else evidence[:117] + "..."
                lines.append(f"│   - [{code}] {where}: {trimmed}")
            if len(self.errors) > 8:
                lines.append(f"│   ... and {len(self.errors) - 8} more")
        if self.warnings:
            lines.append(f"│  {len(self.warnings)} warning(s)")
            for warn in self.warnings[:5]:
                code = warn.get("code", "?")
                where = warn.get("where", "?")
                lines.append(f"│   - [{code}] {where}")
            if len(self.warnings) > 5:
                lines.append(f"│   ... and {len(self.warnings) - 5} more")
        lines.append("╰─")
        return "\n".join(lines)

    @classmethod
    def ok(cls, stage: str, *, warnings: Optional[List[Dict[str, Any]]] = None) -> "ValidationResult":
        return cls(status=_STATUS_OK, stage=stage, errors=[], warnings=warnings)

    @classmethod
    def fatal(
        cls,
        stage: str,
        errors: List[Dict[str, Any]],
        *,
        warnings: Optional[List[Dict[str, Any]]] = None,
    ) -> "ValidationResult":
        return cls(status=_STATUS_FATAL, stage=stage, errors=errors, warnings=warnings)

    @classmethod
    def retryable(
        cls,
        stage: str,
        errors: List[Dict[str, Any]],
        *,
        warnings: Optional[List[Dict[str, Any]]] = None,
    ) -> "ValidationResult":
        return cls(status=_STATUS_RETRYABLE, stage=stage, errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Helpers: classification + finding normalisation
# ---------------------------------------------------------------------------

def _classify(errors: Sequence[Dict[str, Any]]) -> str:
    """Pick status from a list of errors.

    Empty list -> ok. Any error with a fatal code -> fatal. Otherwise
    retryable. The classification is conservative: we only mark fatal
    when we know the issue is structural / unreadable / version-skew,
    because retrying a fatal failure wastes a subagent dispatch.
    """
    if not errors:
        return _STATUS_OK
    for err in errors:
        if err.get("code") in _FATAL_ISSUE_CODES:
            return _STATUS_FATAL
    return _STATUS_RETRYABLE


def _normalise_finding(
    finding: Dict[str, Any],
    *,
    where_keys: Sequence[str] = ("slug", "log", "file", "where"),
) -> Dict[str, Any]:
    """Normalise a per-stage finding to the validate.py envelope shape.

    Per-stage validators emit findings with stage-specific top-level
    keys (``slug`` for Stage 2, ``log`` for Stage 3, ``file`` for
    Stage 4). This helper picks the first present key as the ``where``
    locator so the unified envelope always carries a meaningful pointer.
    """
    where = "?"
    for k in where_keys:
        v = finding.get(k)
        if isinstance(v, str) and v:
            where = v
            break
    return {
        "code": finding.get("issue", "unknown_issue"),
        "where": where,
        "evidence": str(finding.get("evidence", "")),
    }


def _split_severity(
    findings: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a finding list into (errors, warnings) by severity field."""
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    for f in findings:
        normalised = _normalise_finding(f)
        if f.get("severity") == "warning" or f.get("severity") == "warn":
            warnings.append(normalised)
        else:
            errors.append(normalised)
    return errors, warnings


# ---------------------------------------------------------------------------
# Schema-version probe
# ---------------------------------------------------------------------------

def _read_json(path: str) -> Optional[Dict[str, Any]]:
    """Read + parse a JSON file. Return None on missing / parse error.

    Errors propagate up via the caller's classification path -- this
    helper does not raise so the schema-version probe can distinguish
    "file missing" from "file unparseable" via downstream finding codes.
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _probe_schema_version(
    partial_dir: str,
    stage_id: str,
) -> List[Dict[str, Any]]:
    """Check schema_version on each artefact this stage stamps.

    Emits fatal errors for: missing schema_version key, mismatched value,
    or unparseable file. Pre-flight on Stage 0 covers the spine.json
    case directly via stage0.preflight_spine, but we double-check here
    so the dispatcher has a single normalised envelope.
    """
    targets = _STAGE_SCHEMA_TARGETS.get(stage_id, ())
    out: List[Dict[str, Any]] = []
    stage_outputs = os.path.join(partial_dir, "_stage_outputs")
    for filename in targets:
        path = os.path.join(stage_outputs, filename)
        if not os.path.isfile(path):
            # Missing file is the per-stage validator's job to surface
            # (it already does, with much more context). Not a schema
            # issue per se -- skip.
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            out.append({
                "code": "schema_version_unreadable",
                "where": filename,
                "evidence": f"{type(exc).__name__}: {exc}",
            })
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            # Parse errors are surfaced by the per-stage validator with
            # better positioning info; we only emit a schema-level
            # fatal here so the unified envelope carries a clear code.
            out.append({
                "code": "schema_version_unreadable",
                "where": filename,
                "evidence": f"json parse error line {exc.lineno} col {exc.colno}",
            })
            continue
        if not isinstance(data, dict):
            out.append({
                "code": "schema_version_unreadable",
                "where": filename,
                "evidence": f"top-level not object (got {type(data).__name__})",
            })
            continue
        sv = data.get("schema_version")
        if sv != EXPECTED_SCHEMA_VERSION:
            out.append({
                "code": "schema_version_mismatch",
                "where": filename,
                "evidence": (
                    f"expected {EXPECTED_SCHEMA_VERSION!r}, got {sv!r}"
                ),
            })
    return out


# ---------------------------------------------------------------------------
# Stage dispatchers
# ---------------------------------------------------------------------------

def _validate_stage_0(partial_dir: str) -> ValidationResult:
    """Validate Stage 0 outputs (spine.json) via stage0.preflight_spine.

    stage0.preflight_spine handles the full structural contract --
    schema_version, top-level keys, slug shapes, enum values, dates,
    intra-roster slug uniqueness, time-span ordering, anchor-date shape.

    Cross-references the per-stage check intentionally defers and we
    add here:
      * anchor_moments[*].date in [time_span.start, time_span.end]
      * relationship endpoints resolve in people_roster
      * non-minimal-life walnuts have >=1 bundle
      * cross-roster slug uniqueness (people / walnut / bundle compound)
      * bundle.walnut_slug resolves to a real walnut
      * anchor.walnut_slugs / people_slugs resolve
    """
    stage0 = _load_stage(0)
    spine_path = stage0.spine_output_path(partial_dir)

    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    # Structural pre-flight (raises on failure).
    spine: Optional[Dict[str, Any]]
    try:
        spine = stage0.preflight_spine(spine_path)
    except stage0.SpinePreflightError as exc:
        # Pre-flight failures cover schema_version, slug shape, dates,
        # closed-key sets etc. Treat the whole bundle as fatal because a
        # malformed spine cannot drive the rest of the pipeline.
        for err_msg in exc.errors:
            errors.append({
                "code": "structural_pre_flight_failed",
                "where": "spine.json",
                "evidence": err_msg,
            })
        return ValidationResult.fatal("0", errors)
    except Exception as exc:  # noqa: BLE001
        return ValidationResult.fatal("0", [{
            "code": "internal_error",
            "where": "spine.json",
            "evidence": f"{type(exc).__name__}: {exc}",
        }])

    if not isinstance(spine, dict):  # defensive; preflight should reject
        return ValidationResult.fatal("0", [{
            "code": "spine_unparseable",
            "where": "spine.json",
            "evidence": f"top-level value is {type(spine).__name__}",
        }])

    # Cross-stage cross-refs.
    walnuts = spine.get("walnut_roster") or []
    people = spine.get("people_roster") or []
    bundles = spine.get("bundle_distribution") or []
    anchors = spine.get("anchor_moments") or []
    time_span = spine.get("time_span") or {}

    walnut_slugs = {
        w.get("slug") for w in walnuts
        if isinstance(w, dict) and isinstance(w.get("slug"), str)
    }
    people_slugs = {
        p.get("slug") for p in people
        if isinstance(p, dict) and isinstance(p.get("slug"), str)
    }
    bundle_compound_slugs = {
        f"{b.get('walnut_slug')}__{b.get('slug')}"
        for b in bundles
        if isinstance(b, dict)
        and isinstance(b.get("slug"), str)
        and isinstance(b.get("walnut_slug"), str)
    }

    # Anchor dates within time_span.
    ts_start = time_span.get("start") if isinstance(time_span, dict) else None
    ts_end = time_span.get("end") if isinstance(time_span, dict) else None
    for i, moment in enumerate(anchors):
        if not isinstance(moment, dict):
            continue
        date = moment.get("date")
        if (
            isinstance(date, str)
            and isinstance(ts_start, str)
            and isinstance(ts_end, str)
            and not (ts_start <= date <= ts_end)
        ):
            # Lex compare on YYYY-MM-DD is correct (Stage 0 pre-flight
            # already enforced strict zero-padded ISO 8601 shape).
            errors.append({
                "code": "anchor_date_outside_time_span",
                "where": f"anchor_moments[{i}]",
                "evidence": (
                    f"date {date!r} not within "
                    f"[{ts_start!r}, {ts_end!r}]"
                ),
            })

    # Relationship endpoints resolve.
    for pi, person in enumerate(people):
        if not isinstance(person, dict):
            continue
        rels = person.get("relationships")
        if not isinstance(rels, list):
            continue
        for ri, rel in enumerate(rels):
            if not isinstance(rel, dict):
                continue
            for endpoint in ("from", "to"):
                ep_slug = rel.get(endpoint)
                if not isinstance(ep_slug, str):
                    continue
                if ep_slug not in people_slugs:
                    errors.append({
                        "code": "relationship_endpoint_unresolved",
                        "where": (
                            f"people_roster[{pi}].relationships[{ri}].{endpoint}"
                        ),
                        "evidence": (
                            f"{ep_slug!r} not in people_roster slugs"
                        ),
                    })

    # Non-minimal-life walnuts must have >= 1 bundle.
    bundles_per_walnut: Dict[str, int] = {}
    for b in bundles:
        if not isinstance(b, dict):
            continue
        ws = b.get("walnut_slug")
        if isinstance(ws, str):
            bundles_per_walnut[ws] = bundles_per_walnut.get(ws, 0) + 1
    for wi, walnut in enumerate(walnuts):
        if not isinstance(walnut, dict):
            continue
        slug = walnut.get("slug")
        wtype = walnut.get("type")
        if not isinstance(slug, str):
            continue
        if wtype == "minimal-life":
            continue
        if bundles_per_walnut.get(slug, 0) < 1:
            errors.append({
                "code": "walnut_without_bundle",
                "where": f"walnut_roster[{wi}]",
                "evidence": (
                    f"walnut {slug!r} (type={wtype!r}) has zero bundles; "
                    f"only minimal-life walnuts may ship without one"
                ),
            })

    # Bundle.walnut_slug resolves to a real walnut.
    for bi, bundle in enumerate(bundles):
        if not isinstance(bundle, dict):
            continue
        ws = bundle.get("walnut_slug")
        if isinstance(ws, str) and ws not in walnut_slugs:
            errors.append({
                "code": "bundle_walnut_slug_unresolved",
                "where": f"bundle_distribution[{bi}].walnut_slug",
                "evidence": f"{ws!r} not in walnut_roster slugs",
            })

    # Anchor walnut_slugs / people_slugs resolve.
    for ai, moment in enumerate(anchors):
        if not isinstance(moment, dict):
            continue
        for slug in moment.get("walnut_slugs") or []:
            if isinstance(slug, str) and slug not in walnut_slugs:
                errors.append({
                    "code": "anchor_walnut_slug_unresolved",
                    "where": f"anchor_moments[{ai}].walnut_slugs",
                    "evidence": f"{slug!r} not in walnut_roster slugs",
                })
        for slug in moment.get("people_slugs") or []:
            if isinstance(slug, str) and slug not in people_slugs:
                errors.append({
                    "code": "anchor_people_slug_unresolved",
                    "where": f"anchor_moments[{ai}].people_slugs",
                    "evidence": f"{slug!r} not in people_roster slugs",
                })

    # Cross-roster slug uniqueness. Stage 0 pre-flight enforces
    # within-roster uniqueness; the cross-roster guarantee belongs here
    # because it spans rosters.
    cross_seen: Dict[str, str] = {}
    for label, slugs in (
        ("walnut_roster", walnut_slugs),
        ("people_roster", people_slugs),
        ("bundle_compound", bundle_compound_slugs),
    ):
        for slug in slugs:
            if not isinstance(slug, str):
                continue
            if slug in cross_seen and cross_seen[slug] != label:
                errors.append({
                    "code": "slug_collision_across_rosters",
                    "where": f"{label}",
                    "evidence": (
                        f"{slug!r} also appears in {cross_seen[slug]!r}"
                    ),
                })
            else:
                cross_seen[slug] = label

    schema_errors = _probe_schema_version(partial_dir, "0")
    errors.extend(schema_errors)

    if not errors:
        return ValidationResult.ok("0", warnings=warnings)
    status = _classify(errors)
    if status == _STATUS_FATAL:
        return ValidationResult.fatal("0", errors, warnings=warnings)
    return ValidationResult.retryable("0", errors, warnings=warnings)


def _build_stage2_expected_dispatches(
    partial_dir: str,
) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """Synthesize the Stage 2 dispatch descriptor set from the frozen spine.

    Stage 2's per-stage validator accepts a ``dispatches`` list so it
    can flag missing slugs as well as malformed ones. validate.py
    needs that behaviour: a fresh partial that ran Stage 0 + Stage 1
    but skipped Stage 2 entirely should NOT pass Stage 2 validation
    just because there's nothing on disk to walk.

    We build the same descriptor shape stage2.prepare_dispatches
    emits, but only the keys validate_entity_outputs needs:
    ``slug``, ``entity_type``, ``entity_data``. The other fields
    (``output_dir``, ``prompt`` etc.) are ignored on the validation
    path.

    Returns ``None`` if the spine is unreadable / unparseable -- the
    caller then falls back to inference mode (better signal than
    nothing at all). Returns ``(dispatches, gating_errors)`` on
    success: gating errors surface here for things validate_entity_
    outputs can't see (e.g. spine missing a required roster).
    """
    stage_outputs = os.path.join(partial_dir, "_stage_outputs")
    spine_path = os.path.join(stage_outputs, "spine.json")
    if not os.path.isfile(spine_path):
        return None
    try:
        with open(spine_path, "r", encoding="utf-8") as f:
            spine = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(spine, dict):
        return None

    dispatches: List[Dict[str, Any]] = []
    for entry in spine.get("walnut_roster") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str):
            continue
        dispatches.append({
            "slug": slug,
            "entity_type": "walnut",
            "entity_data": dict(entry),
        })
    for entry in spine.get("people_roster") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str):
            continue
        dispatches.append({
            "slug": slug,
            "entity_type": "person",
            "entity_data": dict(entry),
        })
    for entry in spine.get("bundle_distribution") or []:
        if not isinstance(entry, dict):
            continue
        bslug = entry.get("slug")
        wslug = entry.get("walnut_slug")
        if not isinstance(bslug, str) or not isinstance(wslug, str):
            continue
        compound = f"{wslug}__{bslug}"
        dispatches.append({
            "slug": compound,
            "entity_type": "bundle",
            "entity_data": dict(entry),
        })
    return dispatches, []


def _validate_stage_2(partial_dir: str) -> ValidationResult:
    """Validate Stage 2 outputs.

    Calls stage2.validate_entity_outputs (file presence, frontmatter
    contract, link resolution within Stage 2 outputs, bundle manifest
    parent_walnut resolution). Adds:
      * schema_version probe on stage2_done.json (when present)
    """
    stage2 = _load_stage(2)
    # Build the EXPECTED dispatch set from the frozen spine so the
    # validator flags missing entity directories (not just malformed
    # ones present on disk). In inference mode (no dispatches arg)
    # stage2.validate_entity_outputs only walks directories that
    # already exist -- a Stage 2 that wrote zero outputs would
    # otherwise return ok and let the pipeline advance with nothing.
    expected_findings = _build_stage2_expected_dispatches(partial_dir)
    if expected_findings is not None:
        dispatches, gating_errors = expected_findings
    else:
        dispatches = None
        gating_errors = []

    try:
        if dispatches is not None:
            findings = stage2.validate_entity_outputs(
                partial_dir, dispatches=dispatches,
            )
        else:
            findings = stage2.validate_entity_outputs(partial_dir)
    except Exception as exc:  # noqa: BLE001
        return ValidationResult.fatal("2", [{
            "code": "internal_error",
            "where": "validate_entity_outputs",
            "evidence": f"{type(exc).__name__}: {exc}",
        }])

    errors, warnings = _split_severity(findings)
    errors.extend(gating_errors)
    errors.extend(_probe_schema_version(partial_dir, "2"))

    if not errors:
        return ValidationResult.ok("2", warnings=warnings)
    status = _classify(errors)
    if status == _STATUS_FATAL:
        return ValidationResult.fatal("2", errors, warnings=warnings)
    return ValidationResult.retryable("2", errors, warnings=warnings)


def _validate_stage_3(partial_dir: str) -> ValidationResult:
    """Validate Stage 3 outputs.

    Calls stage3.validate_timeline (anchor coverage, decision-WHY,
    entity-ref resolution via COLOR_NAME_RE allowlist, squirrel-id
    stability, cross-walnut rule). Adds:
      * schema_version probe on stage3_done.json
    """
    stage3 = _load_stage(3)
    try:
        findings = stage3.validate_timeline(partial_dir)
    except Exception as exc:  # noqa: BLE001
        return ValidationResult.fatal("3", [{
            "code": "internal_error",
            "where": "validate_timeline",
            "evidence": f"{type(exc).__name__}: {exc}",
        }])

    errors, warnings = _split_severity(findings)
    errors.extend(_probe_schema_version(partial_dir, "3"))

    if not errors:
        return ValidationResult.ok("3", warnings=warnings)
    status = _classify(errors)
    if status == _STATUS_FATAL:
        return ValidationResult.fatal("3", errors, warnings=warnings)
    return ValidationResult.retryable("3", errors, warnings=warnings)


def _validate_stage_4(partial_dir: str) -> ValidationResult:
    """Validate Stage 4 outputs + Stage 4 -> Stage 3 cross-references.

    Stage 4's own validator (``stage4.validate_insights``) handles
    citation FORMAT (every bullet has a parenthetical squirrel cite,
    each cite parses to YYYY-MM-DD + 8-hex). It explicitly defers
    citation RESOLUTION to this module (per stages/stage4.py:1135).

    The new check here: every citation's ``squirrel:<8-hex>`` resolves
    to a real Stage 3 log entry. Resolution: every Stage 3 entry
    carries a 16-hex squirrel-id in its ``## <date> -- squirrel:<sid>``
    heading; the citation's 8-hex is the prefix. We collect every
    16-hex id from world + per-person + per-walnut logs and check each
    citation's 8-hex prefix against that set.

    We also enforce the **anchor-or-pattern** rule: every insight
    bullet must either reference an anchor moment (cite date matches an
    anchor.date) or be a recurring pattern (>=3 distinct cite dates
    across the bullet's section, signalling a cross-time theme). This
    is a soft rule -- the spec says "every insight ties to anchor
    moment OR recurring pattern (>=3 log refs across same theme)".
    The pattern arm is per-bullet (not per-section): the bullet itself
    must cite >=3 distinct resolved log-entries by ``(date, sid_8)``
    pair, otherwise a one-off bullet riding on its neighbours' richness
    would slip through.
    """
    stage3 = _load_stage(3)
    stage4 = _load_stage(4)
    canonical = os.path.normpath(os.path.abspath(partial_dir))

    try:
        findings = stage4.validate_insights(canonical)
    except Exception as exc:  # noqa: BLE001
        return ValidationResult.fatal("4", [{
            "code": "internal_error",
            "where": "validate_insights",
            "evidence": f"{type(exc).__name__}: {exc}",
        }])

    errors, warnings = _split_severity(findings)

    # ----------------------------------------------------------------
    # Citation RESOLUTION: every cited 8-hex prefix must resolve to a
    # real 16-hex squirrel-id from Stage 3.
    #
    # We compute paths via os.path.join (NOT stage3.people_logs_dir() /
    # stage3.walnut_logs_dir(), which call os.makedirs and would mutate
    # the filesystem during a read-only validation pass).
    # ----------------------------------------------------------------
    stage_outputs = os.path.join(canonical, "_stage_outputs")
    log_paths: List[str] = []
    world_log = os.path.join(stage_outputs, "log.md")
    if os.path.isfile(world_log):
        log_paths.append(world_log)
    people_logs_pth = os.path.join(stage_outputs, "people-logs")
    if os.path.isdir(people_logs_pth):
        for name in sorted(os.listdir(people_logs_pth)):
            if name.endswith(".md"):
                log_paths.append(os.path.join(people_logs_pth, name))
    walnut_logs_pth = os.path.join(stage_outputs, "walnut-logs")
    if os.path.isdir(walnut_logs_pth):
        for name in sorted(os.listdir(walnut_logs_pth)):
            if name.endswith(".md"):
                log_paths.append(os.path.join(walnut_logs_pth, name))

    # Collect both the sid_8 set (for "does this id exist?" lookups)
    # AND the (sid_8, date) set (for "does THIS cited date match the
    # entry's actual date?" lookups). Stage 3 entries can repeat the
    # same sid_8 across logs only when the (date, slugs) tuple is also
    # identical (compute_squirrel_id is deterministic), so the (sid, date)
    # pair set is the real source of truth.
    known_sid_prefixes: set = set()
    known_pairs: set = set()  # set[Tuple[sid_8, date]]
    sid_to_dates: Dict[str, set] = {}
    for log_path in log_paths:
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        for m in _LOG_ENTRY_HEADER_RE.finditer(text):
            sid_full = m.group("sid")
            date_full = m.group("date")
            date_prefix = date_full[:10] if len(date_full) >= 10 else date_full
            sid_prefix = sid_full[:8]
            known_sid_prefixes.add(sid_prefix)
            known_pairs.add((sid_prefix, date_prefix))
            sid_to_dates.setdefault(sid_prefix, set()).add(date_prefix)

    # Anchor dates (for the pattern-or-anchor rule).
    anchor_dates: set = set()
    try:
        anchors_env = stage4.load_anchors(canonical)
        for moment in anchors_env.get("confirmed") or []:
            if isinstance(moment, dict):
                d = moment.get("date")
                if isinstance(d, str):
                    anchor_dates.add(d)
    except (stage4.Stage4NotReady, stage4.Stage4Error):
        pass

    # Walk every insights file's bullets, parse citations, resolve.
    # We compute insights paths the same read-only way (no makedirs).
    insights_paths: List[Tuple[str, str]] = []  # (file_kind, path)
    world_insights = os.path.join(stage_outputs, "insights.md")
    if os.path.isfile(world_insights):
        insights_paths.append(("world", world_insights))
    wi_dir = os.path.join(stage_outputs, "walnut-insights")
    if os.path.isdir(wi_dir):
        for name in sorted(os.listdir(wi_dir)):
            if name.endswith(".md"):
                slug = name[:-3]
                insights_paths.append((
                    f"walnut:{slug}", os.path.join(wi_dir, name),
                ))

    for file_kind, path in insights_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        # Section + bullet walk.
        sections = _split_sections_by_heading(text)
        for section_title, section_body in sections:
            for bullet in _bullet_lines(section_body):
                cites = list(_extract_citations(bullet))
                # Per-cite resolution: the (date, sid_8) pair must
                # match a real Stage 3 log entry. We split the failure
                # into two codes:
                #   * citation_unresolved -- sid_8 not in any log
                #   * citation_date_mismatch -- sid_8 exists, but the
                #     cited date doesn't match the entry's real date
                resolved_pairs: set = set()
                for date_str, sid_8 in cites:
                    if sid_8 not in known_sid_prefixes:
                        errors.append({
                            "code": "citation_unresolved",
                            "where": f"{file_kind}#{section_title}",
                            "evidence": (
                                f"squirrel:{sid_8} cited on {date_str} "
                                f"does not resolve to any Stage 3 log entry"
                            ),
                        })
                        continue
                    if (sid_8, date_str) not in known_pairs:
                        # The id is real but its actual log date differs
                        # from the citation's date. Surface the real
                        # date(s) so the model can fix the citation in
                        # one shot.
                        actual = sorted(sid_to_dates.get(sid_8, set()))
                        errors.append({
                            "code": "citation_date_mismatch",
                            "where": f"{file_kind}#{section_title}",
                            "evidence": (
                                f"squirrel:{sid_8} cited on {date_str} "
                                f"resolves to a real entry, but its "
                                f"log date is {actual!r}"
                            ),
                        })
                        continue
                    resolved_pairs.add((sid_8, date_str))

                # Anchor-or-pattern (per-bullet).
                #
                # A bullet with no citations is already flagged by
                # Stage 4's own validator (missing_citation); skip the
                # OR-rule on it.
                if not cites:
                    continue
                cite_dates = {d for d, _ in cites}
                ties_to_anchor = bool(cite_dates & anchor_dates)
                # Pattern: this bullet itself cites >=3 distinct
                # resolved log entries (by (sid_8, date) pair). Same
                # date but different sids count as distinct entries
                # (different sessions on the same day). Multiple cites
                # of the same (sid, date) pair count once -- the same
                # log entry referenced twice is one ref.
                ties_to_pattern = len(resolved_pairs) >= 3
                if not ties_to_anchor and not ties_to_pattern:
                    errors.append({
                        "code": "insight_without_anchor_or_pattern",
                        "where": f"{file_kind}#{section_title}",
                        "evidence": (
                            "bullet does not cite an anchor-moment date "
                            "and does not cite >=3 distinct resolved "
                            "log entries (bullet preview: "
                            f"{bullet.strip()[:80]!r})"
                        ),
                    })

    errors.extend(_probe_schema_version(partial_dir, "4"))

    if not errors:
        return ValidationResult.ok("4", warnings=warnings)
    status = _classify(errors)
    if status == _STATUS_FATAL:
        return ValidationResult.fatal("4", errors, warnings=warnings)
    return ValidationResult.retryable("4", errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Citation + section helpers (mirror stage4 helpers; duplicated for
# stdlib-only and to keep validate.py self-contained on the citation
# RESOLUTION path).
# ---------------------------------------------------------------------------

def _split_sections_by_heading(text: str) -> List[Tuple[str, str]]:
    """Split a markdown body into (heading, body) sections by ``## ``."""
    out: List[Tuple[str, str]] = []
    matches = list(_SECTION_HEADING_RE.finditer(text))
    for i, m in enumerate(matches):
        title = m.group("title").strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((title, text[body_start:body_end]))
    return out


def _bullet_lines(section_body: str) -> List[str]:
    """Return logical bullet lines from a section body.

    Delegates to ``stage4._bullet_lines`` so cross-stage validation
    sees the EXACT same bullets Stage 4's own validator does --
    including continuation-line joining for multi-line bullets. A
    wrapped bullet like::

        - prose
          (2025-08-12, squirrel:abc12345)

    is one logical bullet whose citation should resolve under both
    Stage 4's format check and validate.py's resolution check.
    """
    return _load_stage(4)._bullet_lines(section_body)


def _extract_citations(line: str) -> List[Tuple[str, str]]:
    """Return [(date, sid_8), ...] for every well-formed citation."""
    out: List[Tuple[str, str]] = []
    for m in _CITATION_RE.finditer(line):
        body = m.group("body")
        for pair in body.split("; "):
            pm = _CITATION_PAIR_RE.match(pair.strip())
            if pm is not None:
                out.append((pm.group("date"), pm.group("sid")))
    return out


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

def validate_stage(stage_id: str, partial_dir) -> ValidationResult:
    """Run validation for the given stage's outputs.

    Args:
      stage_id: One of "0", "2", "3", "4". Stage 1 is UX-only.
      partial_dir: Path-like absolute path to ``<base>/wld_<ulid>.partial/``.

    Returns:
      A ``ValidationResult`` with status ``ok`` / ``retryable`` / ``fatal``.
      Callers (the per-stage dispatcher) decide what to do: ok -> proceed,
      retryable -> use ``format_retry_feedback`` for one retry, fatal ->
      surface the three-option block.

    Raises:
      ValueError: if ``stage_id`` is not one of the supported values.
      TypeError: if ``partial_dir`` is not path-like.
    """
    if hasattr(partial_dir, "__fspath__"):
        partial_dir = os.fspath(partial_dir)
    if not isinstance(partial_dir, str):
        raise TypeError(
            f"partial_dir must be path-like; got {type(partial_dir).__name__}"
        )
    if stage_id not in SUPPORTED_STAGES:
        raise ValueError(
            f"stage_id must be one of {SUPPORTED_STAGES}; got {stage_id!r}"
        )
    canonical = os.path.normpath(os.path.abspath(partial_dir))
    if stage_id == "0":
        return _validate_stage_0(canonical)
    if stage_id == "2":
        return _validate_stage_2(canonical)
    if stage_id == "3":
        return _validate_stage_3(canonical)
    if stage_id == "4":
        return _validate_stage_4(canonical)
    # Unreachable.
    raise AssertionError(f"unreachable: stage_id={stage_id!r}")


# ---------------------------------------------------------------------------
# Retry feedback formatter
# ---------------------------------------------------------------------------

def format_retry_feedback(
    result: ValidationResult,
    *,
    max_chars: int = 4000,
) -> str:
    """Render a truncated retry-feedback block for the dispatcher.

    Output shape::

        ## Retry feedback (stage <N>)

        Your previous attempt failed validation. Fix the errors below
        and re-write the offending files.

        ### Errors
        - [<code>] <where>: <evidence>
        ...

        ### Fix hints
        - <hint per code>

    The total length is bounded by ``max_chars`` -- the formatter trims
    trailing errors with a "... and N more" line so the prompt budget
    stays predictable on a model that emitted hundreds of findings.
    """
    if result.is_ok():
        return ""
    lines = [
        f"## Retry feedback (stage {result.stage})",
        "",
        (
            "Your previous attempt failed validation. Fix the errors "
            "below and re-write the offending files to the same paths "
            "via the standard atomic-write helpers."
        ),
        "",
        "### Errors",
    ]
    truncated_count = 0
    for err in result.errors:
        line = (
            f"- [{err.get('code', '?')}] {err.get('where', '?')}: "
            f"{err.get('evidence', '')}"
        )
        # Approximate length tracking. Stop adding errors once we're
        # within reach of the cap so the trailer + hints fit.
        projected = sum(len(s) for s in lines) + len(lines) + len(line)
        if projected > max_chars - 600:  # reserve room for trailer + hints
            truncated_count = len(result.errors) - (len(lines) - 5)
            break
        lines.append(line)
    if truncated_count > 0:
        lines.append(f"- ... and {truncated_count} more error(s)")

    # Fix hints by code. Coerce to str defensively -- a finding with a
    # non-string code (shouldn't happen but caller may pass us anything)
    # would otherwise break the sort.
    seen_codes = sorted({str(err.get("code", "?")) for err in result.errors})
    if seen_codes:
        lines.append("")
        lines.append("### Fix hints")
        for code in seen_codes:
            hint = _fix_hint_for(code)
            if hint:
                lines.append(f"- {code}: {hint}")

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 16] + "\n... [truncated]"
    return out


_FIX_HINTS: Dict[str, str] = {
    "anchor_date_outside_time_span": (
        "shift the anchor date to fall within time_span.start..end "
        "or widen the time_span"
    ),
    "relationship_endpoint_unresolved": (
        "either add the missing person to people_roster or fix the "
        "from/to slug to match an existing roster entry"
    ),
    "walnut_without_bundle": (
        "add at least one bundle to bundle_distribution with this "
        "walnut_slug, or change the walnut type to minimal-life"
    ),
    "bundle_walnut_slug_unresolved": (
        "set the bundle's walnut_slug to a slug present in walnut_roster"
    ),
    "anchor_walnut_slug_unresolved": (
        "remove the unknown walnut slug or add it to walnut_roster"
    ),
    "anchor_people_slug_unresolved": (
        "remove the unknown person slug or add them to people_roster"
    ),
    "slug_collision_across_rosters": (
        "rename the duplicate slug so each roster's slug is unique "
        "across people / walnut / bundle namespaces"
    ),
    "citation_unresolved": (
        "the cited squirrel:<8-hex> prefix must match a real Stage 3 "
        "log-entry id; either fix the citation or add the entry"
    ),
    "citation_date_mismatch": (
        "the squirrel id resolves to a real Stage 3 log entry but the "
        "cited date does not match the entry's actual log date; copy "
        "the entry's real date into the citation"
    ),
    "insight_without_anchor_or_pattern": (
        "anchor the insight to a confirmed anchor-moment date OR cite "
        "at least three distinct resolved log entries (by (sid, date) "
        "pair) on this bullet to establish a recurring pattern; "
        "neighbouring bullets do not contribute"
    ),
    "schema_version_mismatch": (
        f"set schema_version to {EXPECTED_SCHEMA_VERSION!r} on the "
        "stage output; do not invent a new version number"
    ),
    "schema_version_unreadable": (
        "the file is missing or unparseable; rewrite it from scratch "
        "with valid JSON and schema_version stamped"
    ),
}


def _fix_hint_for(code: str) -> Optional[str]:
    return _FIX_HINTS.get(code)


# ---------------------------------------------------------------------------
# Three-option surface block
# ---------------------------------------------------------------------------

def three_option_surface_block(result: ValidationResult) -> str:
    """Render a bordered block for the second-failure user surface.

    Per the spec's locked decision: if a stage validation fails twice
    (validate_stage -> retryable, dispatcher fires retry, validate_stage
    -> retryable again), the parent skill surfaces three options via
    AskUserQuestion at the squirrel level: accept partial / retry full
    / cancel. This helper renders the bordered block the squirrel emits
    inline before firing the question.
    """
    lines = [
        f"╭─ 🐿️ stage {result.stage} validation: second failure",
        "│",
        f"│   {len(result.errors)} error(s) remain after one retry.",
        "│",
        "│   First three:",
    ]
    for err in result.errors[:3]:
        code = err.get("code", "?")
        where = err.get("where", "?")
        evidence = err.get("evidence", "")
        trimmed = evidence if len(evidence) <= 80 else evidence[:77] + "..."
        lines.append(f"│   - [{code}] {where}: {trimmed}")
    if len(result.errors) > 3:
        lines.append(f"│   ... and {len(result.errors) - 3} more")
    lines.extend([
        "│",
        "│   ▸ Three options:",
        "│   1. Accept partial   (proceed with current outputs)",
        "│   2. Retry full       (re-dispatch the whole stage from scratch)",
        "│   3. Cancel           (abandon this demo world)",
        "╰─",
    ])
    return "\n".join(lines)


# Backwards-compat alias used by SKILL.md examples.
_three_option_surface_block = three_option_surface_block


__all__ = (
    "ValidationResult",
    "validate_stage",
    "format_retry_feedback",
    "three_option_surface_block",
    "EXPECTED_SCHEMA_VERSION",
    "SUPPORTED_STAGES",
)
