"""v3.0 -> v3.1 migration runner (T10 of fn-18).

Public entry: :func:`run_v3_0_to_v3_1`. Consumes phase-3's
``DetectionReport``, phase-7's ``WalkthroughDecisions``, and the
post-backup world tree to execute v3.0 -> v3.1 operations in place.

What the v3.0 -> v3.1 transition actually did (per drift-inventory):

* ``ALIVE_PLUGIN_ROOT`` env var introduced (commit ``f565c81``,
  2026-04-16). User content referencing ``plugins/alive/scripts/...``
  needs rewriting to ``${ALIVE_PLUGIN_ROOT}/scripts/...``.
* Bundle canonical fields locked: ``species``, ``phase``, ``goal``,
  ``context_routes``. **Schema rewrite is deferred to v4** per the
  CHANGELOG locked decision -- v3.1 ships dual-read fallbacks so the
  migration is detection-only (we record counts in the upgrade record
  for visibility, but never rewrite).
* ``.alive/scripts/`` copy-to-world pattern fully retired -- the
  cleanup of stale ``.alive/scripts/`` directories is owned by T5's
  cleanup phase, NOT this runner.
* P2P + alive-mcp surfaces are independent release lines; not in
  scope for the world-state migrator.

So this task ships ZERO bespoke rewrite logic. The ALIVE_PLUGIN_ROOT
find/replace runs through T8's ``walkthrough/apply.py`` -- the
catalog entry at ``retired_patterns.CATALOG`` (source_version="3.1",
walkthrough_eligible=True, rewrite_kind="regex_substitute",
pattern_signature=r"\\bplugins/alive/scripts/(\\S+)",
replacement_template=r"${ALIVE_PLUGIN_ROOT}/scripts/\\1") drives BOTH
the candidate detection (phase 7) AND the rewrite generation (phase
9). Every byte change traces back to a catalog entry.

Bundle canonical fields detection
---------------------------------
Read-only. We scan the per-walnut bundle frontmatter (snapshot-style
regex of the YAML header -- mirrors ``signals/bundle_schema.py``) and
record:

* ``bundles_with_canonical``     -- count carrying any of the four
                                    canonical fields.
* ``bundles_without_canonical``  -- count with frontmatter but no
                                    canonical field.
* ``bundles_total``              -- total bundles inspected.

These land on the ``MigrationReport`` for the orchestrator's phase-12
final-record write. **No rewrite.** The dual-read fallbacks shipping
in v3.1 mean a v3.0-shape bundle remains valid; v4 will introduce the
hard break.

Idempotency
-----------
Running the runner twice in succession against the same world
produces identical post-state because:

* The walkthrough apply step is itself idempotent on a per-pattern
  basis (the regex matches ``plugins/alive/scripts/...`` literals; the
  replacement contains ``${ALIVE_PLUGIN_ROOT}/scripts/...`` which does
  not satisfy the original pattern, so re-runs find no further matches
  to rewrite). The phase-7 decision step would not even surface the
  rewritten lines as candidates on a second pass.
* The bundle-fields detection is read-only and produces the same count
  on identical input.

Stdlib-only (R10): no PyYAML / ruamel; runstate I/O via
:mod:`system_upgrade._record_codec`.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from _common import iso_now

from .. import _normalize_version
from . import _record, _retroactive
from ._record import MigrationReport, OpResult


__all__ = (
    "run_v3_0_to_v3_1",
)


_FROM_VERSION = "3.0"
_TO_VERSION = "3.1"


# Canonical bundle-schema fields introduced at v3.1. Mirrors
# ``signals/bundle_schema.CANONICAL_FIELDS`` but inlined to avoid
# pulling the signals package into the migration module's import
# graph (the signals package is detection-time; T10 runs at apply
# time and the duplication is one tuple).
_CANONICAL_BUNDLE_FIELDS: Tuple[str, ...] = (
    "species",
    "phase",
    "goal",
    "context_routes",
)


# Frontmatter delimiter / key extractor. Mirror of
# ``signals/bundle_schema._FM_RE`` / ``_KV_RE``.
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_KV_RE = re.compile(r"^(\w[\w-]*)\s*:", re.MULTILINE)


# ---------------------------------------------------------------------------
# Bundle-fields detection (read-only)
# ---------------------------------------------------------------------------


def _bundle_manifests_under(walnut_root: str) -> List[str]:
    """Return absolute paths of bundle manifest candidates inside *walnut_root*.

    A "bundle manifest" is any file named:

    * ``context.manifest.yaml``  -- canonical v3 bundle frontmatter
    * ``companion.md``           -- legacy bundle frontmatter (some
                                    v3.0 bundles still use this name)

    The walk skips dotted dirs (``.alive``, ``.git``), the walnut's
    own ``_kernel`` / ``_core`` (skip-listed at the top level), and
    non-bundle plumbing directories.

    Walnut-boundary pruning: when the walk
    encounters a *nested* walnut (a sub-directory whose
    ``_kernel/key.md`` exists), it does NOT descend further. Walnuts
    don't nest from a bundle-discovery perspective; the nested
    walnut's bundles are owned by that walnut's own count, not by
    the parent's. This matters for the world-as-walnut layout where
    ``walnut_root`` IS the world root and nested walnuts under
    ``04_Ventures/`` need their bundles attributed to themselves
    only.
    """
    out: List[str] = []
    skip_top = {"_kernel", "_core"}
    if not os.path.isdir(walnut_root):
        return out
    walnut_root_abs = os.path.abspath(walnut_root)
    for root, dirs, files in os.walk(walnut_root):
        # Filter children: never descend into hidden / kernel dirs.
        kept: List[str] = []
        for d in dirs:
            if d.startswith("."):
                continue
            if d in (
                "node_modules", "__pycache__", "venv", ".venv",
                "build", "dist",
            ):
                continue
            if root == walnut_root and d in skip_top:
                continue
            child = os.path.join(root, d)
            # Walnut-boundary pruning: a child whose `_kernel/key.md`
            # exists is itself a walnut. Don't traverse into it; its
            # bundles are owned by the nested walnut's own scan.
            if (
                os.path.abspath(child) != walnut_root_abs
                and os.path.isfile(
                    os.path.join(child, "_kernel", "key.md")
                )
            ):
                continue
            kept.append(d)
        dirs[:] = kept
        for fname in files:
            if fname == "context.manifest.yaml":
                out.append(os.path.join(root, fname))
            elif fname == "companion.md":
                out.append(os.path.join(root, fname))
    return out


def _frontmatter_keys(blob: bytes) -> Optional[List[str]]:
    """Return frontmatter keys for *blob*, or ``None`` if absent.

    ``None`` distinguishes "no frontmatter at all" (skip the file --
    not a bundle manifest in the canonical sense) from "frontmatter
    present but empty" (count as a bundle without canonical fields).
    """
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover -- decode is robust
        return None
    m = _FM_RE.match(text)
    if not m:
        return None
    body = m.group(1)
    return list(_KV_RE.findall(body))


def _detect_bundle_fields(walnut_root: str) -> Dict[str, Any]:
    """Scan *walnut_root* for bundle-frontmatter canonical-field presence.

    Returns a counts dict suitable for landing on the migration report:

        {
            "walnut_root": "/abs/path",
            "bundles_total": int,
            "bundles_with_canonical": int,
            "bundles_without_canonical": int,
            "fields_seen": ["species", "phase", ...],   # union across bundles
        }

    Read-only -- the bundle YAML files are never modified.
    """
    counts = {
        "walnut_root": os.path.abspath(walnut_root),
        "bundles_total": 0,
        "bundles_with_canonical": 0,
        "bundles_without_canonical": 0,
        "fields_seen": [],
    }
    seen: List[str] = []
    canonical_set = set(_CANONICAL_BUNDLE_FIELDS)
    for path in _bundle_manifests_under(walnut_root):
        try:
            with open(path, "rb") as f:
                blob = f.read(2048)  # head-mode 2 KiB; mirrors snapshot rule
        except OSError:
            continue
        keys = _frontmatter_keys(blob)
        if keys is None:
            # No frontmatter -- not a v3 bundle manifest. Skip.
            continue
        counts["bundles_total"] += 1
        canonical_hits = canonical_set.intersection(keys)
        if canonical_hits:
            counts["bundles_with_canonical"] += 1
            for k in canonical_hits:
                if k not in seen:
                    seen.append(k)
        else:
            counts["bundles_without_canonical"] += 1
    counts["fields_seen"] = sorted(seen)
    return counts


# ---------------------------------------------------------------------------
# Walkthrough-apply driver lives in ``migrations._record`` as
# :func:`_apply_walkthrough_decisions` -- shared across every runner so
# the v3.1-retired catalog (ALIVE_PLUGIN_ROOT find/replace) is driven
# by the same composition as v2 -> v3.0 and v3.1 -> v3.2.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Walnut resolution (mirrors v2_to_v3_0._resolve_walnuts but bound to
# v3.0 -> v3.1 scope: only walnuts whose detection result is < 3.1).
# ---------------------------------------------------------------------------


def _resolve_walnuts(
    world_root: str, detection: Any,
) -> List[str]:
    """Return the absolute walnut paths needing v3.0 -> v3.1 migration.

    Detection-driven: filter ``per_walnut_versions`` to walnuts whose
    resolved version is below v3.1. Walnuts whose detection result
    is already at v3.1+ are silently skipped (scoped fingerprinting,
    R16: a world on v3.1 with one v3.0-laggy walnut runs only the
    laggy walnut's migration).

    Fallback (no detection supplied): scan the live disk for any
    walnut whose ``_kernel/key.md`` exists -- a v3 walnut is a
    candidate for the v3.0 -> v3.1 walkthrough. The bundle-canonical-
    field detection runs unconditionally on every candidate; the
    walkthrough apply only fires when the operator's phase-7
    decisions name files inside the candidate.
    """
    target = (3, 1, 0)
    if detection is not None:
        per_walnut = getattr(detection, "per_walnut_versions", None) or {}
        out: List[str] = []
        for walnut_path, version in per_walnut.items():
            try:
                if _normalize_version(version) < target:
                    out.append(os.path.abspath(walnut_path))
            except (ValueError, TypeError):
                # Unparseable version -- include it. The detection
                # work is read-only and the walkthrough apply only
                # acts on phase-7 decisions, so over-inclusion is safe.
                out.append(os.path.abspath(walnut_path))
        return sorted(set(out))

    # Fallback: live-disk sweep.
    #
    # World-root special case: a world that
    # is ITSELF walnut-shaped (``<world>/_kernel/key.md`` present) is a
    # legitimate v1/v2 layout vestige (see ``version_detect``'s
    # ``_world_root_is_walnut`` predicate). When such a world ALSO
    # contains nested walnuts (canonical or otherwise), the sweep must
    # NOT prune the descent at the world root -- otherwise we'd never
    # discover the children. So we evaluate the world root separately
    # and only honour the walnut-boundary pruning for paths strictly
    # below it. Mirrors ``v2_to_v3_0._resolve_walnuts``.
    found: List[str] = []
    if os.path.isfile(os.path.join(world_root, "_kernel", "key.md")):
        found.append(world_root)
    for root, dirs, _files in os.walk(world_root):
        # Don't descend into hidden / ignored dirs.
        dirs[:] = [
            d for d in dirs
            if not (d.startswith(".") and d != "_kernel")
            and d not in ("node_modules", "__pycache__", "venv", ".venv")
        ]
        if root == world_root:
            # Already evaluated above -- do NOT prune descent here, or
            # nested walnuts would never be reached on a world-as-walnut
            # layout.
            continue
        if os.path.isfile(os.path.join(root, "_kernel", "key.md")):
            found.append(root)
            # Walnut boundary: don't descend further into a discovered
            # walnut (walnuts don't nest).
            dirs[:] = []
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def run_v3_0_to_v3_1(
    world_root: str,
    snapshot: Any = None,
    plan: Any = None,
    walkthrough_decisions: Any = None,
    *,
    detection: Any = None,
    started_iso: Optional[str] = None,
    tool_version_at_run: str = "",
    session_id: str = "manual",
    dry_run: bool = False,
    now_provider=None,
    resume_marker: Any = None,
    halt_on_failure: bool = True,
) -> MigrationReport:
    """Execute v3.0 -> v3.1 migration ops + walkthrough apply.

    Parameters mirror :func:`run_v2_to_v3_0`; see that docstring for
    the parameter contract. The semantic differences:

    * The runner ships ZERO bespoke filesystem rewrite logic. Every
      byte change traces back to a catalog entry surfaced in phase 7.
    * Per-walnut bundle-canonical-field detection lands on the
      report as ``OpResult(op_type="detect_bundle_canonical_fields",
      status="applied", detail=<counts>)``. **Read-only -- no
      rewrite.** Bundle schema rewrite is deferred to v4 per the
      CHANGELOG locked decision.
    * The runstate file uses the same ``-runstate.yaml`` suffix
      family as the v2 -> v3.0 runner; T7's strict regex excludes
      the suffix from final-record loads.

    Returns the ``MigrationReport`` the orchestrator (phase 12)
    merges into the canonical final upgrade record.

    Raises
    ------
    Nothing. Op-level failures land on ``report.errors`` /
    ``report.operations``; walkthrough-apply exceptions land on
    ``report.errors`` and (when ``halt_on_failure=True``) stop the
    runner without re-raising. Mirrors the v2 -> v3.0 contract so
    the orchestrator can treat every per-version runner identically.
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
    # Per-walnut: bundle canonical-field detection (read-only).
    # ------------------------------------------------------------------
    walnuts_to_migrate: List[str] = _resolve_walnuts(world_root, detection)
    report.walnuts_migrated = list(walnuts_to_migrate)

    for walnut_root in walnuts_to_migrate:
        counts = _detect_bundle_fields(walnut_root)
        # Encode counts into the OpResult.detail so the runstate /
        # final record carries the structured payload as a string-
        # serialisable summary. The full counts dict also lands on
        # ``report.errors``-adjacent forensic paths is NOT what we
        # want -- we want it on a structured field. Keep the detail
        # human-readable and the dict accessible via the runstate
        # codec (which serialises OpResult.as_dict() verbatim).
        detail = (
            "bundles_total={total}, bundles_with_canonical={with_c}, "
            "bundles_without_canonical={without_c}, "
            "fields_seen={fields}"
        ).format(
            total=counts["bundles_total"],
            with_c=counts["bundles_with_canonical"],
            without_c=counts["bundles_without_canonical"],
            fields=",".join(counts["fields_seen"]) or "<none>",
        )
        op = OpResult(
            op_type="detect_bundle_canonical_fields",
            from_path=walnut_root,
            to_path="",
            status="applied",
            timestamp=now_provider(),
            detail=detail,
            walnut_root=walnut_root,
        )
        if not _record_op(op):
            return _finalise(report, now_provider)

    # ------------------------------------------------------------------
    # Walkthrough apply (Codex M9 / phase 9): rewrite v3.1-retired
    # patterns in user extensions (ALIVE_PLUGIN_ROOT find/replace).
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
    # Retroactive synthesis for messy worlds (clean-finish only).
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

    # Finalise marker.
    marker_tracker.finalise_completed()

    return _finalise(report, now_provider)


def _finalise(report: MigrationReport, now_provider) -> MigrationReport:
    """Stamp ``finished_iso`` and return."""
    report.finished_iso = now_provider()
    return report
