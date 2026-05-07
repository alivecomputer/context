"""Content-fingerprint version detection (T3 of fn-18).

Reads a frozen :class:`FileSnapshot` plus the retired-pattern catalog
to produce a :class:`DetectionReport` carrying:

* ``world_version``                 -- lowest-version inferred from
                                       world-scope signals.
* ``per_walnut_versions``           -- per-walnut version inferences.
* ``all_signals_raw``                -- every probe (fired or absent)
                                       for forensic debugging.
* ``tool_version_at_run``           -- plugin manifest version captured
                                       separately from world signals.
* ``walkthrough_eligible_matches``   -- catalog matches from T4 piped
                                       in for phase-5 no-op gating.
* ``legacy_walnuts_discovered``      -- walnuts found by the legacy-aware
                                       finder but NOT by the canonical
                                       ``_common.find_all_walnuts``.

The module is read-only -- every byte arrives via the snapshot, no
``Path.read_text`` / ``Path.read_bytes`` after the snapshot is built.
The ``test_version_detect.py`` module has a regex test that asserts
this property at the source level.

Refusal semantics:

* Zero world-scope signals fire AND no walnuts discovered AND
  ``--assume-empty-world`` not supplied -> :class:`DetectionRefusal`
  surfaced with hint code ``no_signals``.
* ``--assume-empty-world`` supplied but the world is non-empty
  (kernel present, walnuts discovered, or any world signal fires)
  -> :class:`DetectionRefusal` with code ``assume_empty_world_invalid``.

The CLI subcommand catches :class:`DetectionRefusal` and translates
to the structured exit envelope (exit code 1 with ``error_code``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .orchestrator import DetectionReport
from .retired_patterns import (
    CATALOG,
    CatalogMatch,
    match_walkthrough_eligible,
)
from .signals import (
    SCOPE_WALNUT,
    SCOPE_WORLD,
    SOURCE_RANK,
    SignalProbe,
)
from .signals import bundle_schema as _schema
from .signals import hook_content as _content
from .signals import path_existence as _path
from .tool_version import read_tool_version


__all__ = (
    "DetectionRefusal",
    "detect_world_version",
    "discover_walnuts_legacy_aware",
    "snapshot_rule_contributions",
)


# Versions used as floors when no probe fires but a v3 kernel is
# present on a walnut. The v3.0 floor is the safe default for "we
# know it's v3 but no canonical-rewrite signal was seen".
_V3_KERNEL_FLOOR = "3.0"
_BASELINE_VERSION = "0.0"  # for --assume-empty-world ack only


# ---------------------------------------------------------------------------
# Refusal exception
# ---------------------------------------------------------------------------

class DetectionRefusal(Exception):
    """Raised by :func:`detect_world_version` to surface a structured refusal.

    Attributes
    ----------
    code : str
        Stable error_code suitable for the CLI envelope. One of:
        ``"no_signals"``, ``"assume_empty_world_invalid"``.
    message : str
        Human-readable diagnostic naming the path classes that didn't
        fire (for ``no_signals``) or the conditions that disqualified
        the empty-world bypass (for ``assume_empty_world_invalid``).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Legacy-aware walnut discovery
# ---------------------------------------------------------------------------

# Directory names we never descend into during legacy-aware walks.
# Aligned with ``_common._WALNUT_SCAN_SKIP_DIRS`` plus extras for
# legacy world layouts. Allows ``_kernel/`` (legitimate dot-prefixed
# name).
_LEGACY_SKIP_DIRS: frozenset = frozenset({
    ".git",
    ".alive",  # DERIVED: skip-dir entry; this is the directory NAME, not a path-string verifier callsite (see R5 audit)
    ".stversions",
    ".flow",
    ".next",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "raw",
    "target",
    "venv",
})

#: Prefix marking a directory as a point-in-time walnut snapshot/backup
#: rather than a live walnut tree. Created by ad-hoc cleanup commits
#: ("walnut-duplicates-2026-04-27/"); the directory holds copies of
#: walnuts the operator already moved aside. Discovery must NOT recurse
#: into these or the duplicate walnuts re-register and the cleanup /
#: migration phases would mutate the very copies the operator preserved.
_ARCHIVED_SNAPSHOT_PREFIXES: Tuple[str, ...] = ("walnut-duplicates-",)


def _is_archived_snapshot_dir(name: str) -> bool:
    """Return True iff *name* is a point-in-time walnut backup directory.

    Used by both the legacy-aware traversal here and the canonical-only
    finder in ``_common.find_all_walnuts``. Centralised so both
    discovery paths agree on what "not a walnut directory" means.
    """
    return any(name.startswith(p) for p in _ARCHIVED_SNAPSHOT_PREFIXES)


# Allow-list for dot-prefixed directories that ARE traversable.
_LEGACY_ALLOW_DOT_DIRS: frozenset = frozenset({"_kernel"})

# Templates exclusion: plugin-vendored template
# directories MUST NOT register as walnuts. Match any path containing
# one of these segments (segment-aware -- avoid false matches on a
# walnut literally named ``templates``).
_TEMPLATE_EXCLUSIONS: Tuple[Tuple[str, ...], ...] = (
    ("templates", "walnut"),
    ("templates", "companion"),
    ("templates", "_kernel"),
)


def _is_under_template_dir(rel_segments: Tuple[str, ...]) -> bool:
    """Return True iff a path's segments include any template tuple."""
    for tmpl in _TEMPLATE_EXCLUSIONS:
        # Look for the template tuple as a contiguous run anywhere in
        # the path. This catches both ``templates/walnut/now.md`` and
        # ``plugins/foo/templates/walnut/now.md``.
        n = len(tmpl)
        for i in range(len(rel_segments) - n + 1):
            if rel_segments[i:i + n] == tmpl:
                return True
    return False


def _walnut_markers(dir_path: str) -> Tuple[bool, str]:
    """Return ``(is_walnut, marker_note)`` for one directory.

    Detection markers (any of which qualifies the directory as a walnut):

    * ``_kernel/key.md``        -- v3 walnut
    * ``_core/key.md``          -- v2 walnut
    * ``companion.md``          -- v1 walnut marker
    * ``_core/_squirrels/``     -- v2 walnut alt-marker
    * ``_kernel/_generated/``   -- in-flight upgrade walnut (v2.5)

    Suppressed: directories under ``<walnut>/_core/_capsules/`` -- in
    the v1 layout ``_core/_capsules/<name>/`` was the bundle storage
    location inside a walnut, so a directory whose path contains a
    ``_core/_capsules`` segment pair is a v1 capsule (bundle), not a
    walnut. Without the suppression, a partial v1 archive missing its
    outer walnut markers (e.g. an outer dir lacking ``companion.md``)
    causes the inner capsule to register as a phantom v1 walnut.
    """
    # v1-capsule suppression. Use os.sep-aware segment match rather
    # than a string ``in`` check so a directory literally named
    # ``_capsules`` at a different depth is unaffected.
    norm = os.path.normpath(dir_path)
    segments = norm.split(os.sep)
    for i in range(len(segments) - 1):
        if segments[i] == "_core" and segments[i + 1] == "_capsules":
            # Only suppress paths STRICTLY UNDER _core/_capsules/, not
            # the _capsules dir itself (_core/_capsules/foo qualifies;
            # _core/_capsules does not -- though the latter has no
            # walnut markers either, the explicit gate avoids relying
            # on that coincidence).
            if i + 2 < len(segments):
                return False, ""
    if os.path.isfile(os.path.join(dir_path, "_kernel", "key.md")):
        return True, "v3:_kernel/key.md"
    if os.path.isfile(os.path.join(dir_path, "_core", "key.md")):
        return True, "v2:_core/key.md"
    if os.path.isfile(os.path.join(dir_path, "companion.md")):
        return True, "v1:companion.md"
    if os.path.isdir(os.path.join(dir_path, "_core", "_squirrels")):
        return True, "v2:_core/_squirrels/"
    if os.path.isdir(os.path.join(dir_path, "_kernel", "_generated")):
        return True, "v2.5:_kernel/_generated/"
    return False, ""


def _world_root_is_walnut(world_root: str) -> Tuple[bool, str]:
    """Return ``(is_walnut, note)`` for a world-root v1 layout.

    A world is itself a walnut when it carries any of:
      * ``_core/``
      * ``companion.md``
      * ``now.md``
    """
    if os.path.isdir(os.path.join(world_root, "_core")):
        return True, "world-as-walnut:_core/"
    if os.path.isfile(os.path.join(world_root, "companion.md")):
        return True, "world-as-walnut:companion.md"
    if os.path.isfile(os.path.join(world_root, "now.md")):
        return True, "world-as-walnut:now.md"
    return False, ""


def discover_walnuts_legacy_aware(world_root: str) -> List[str]:
    """Recursive walnut walk that finds v1, v2, and v3 walnut shapes.

    Walks *world_root* without an arbitrary depth cap. Skip-list +
    walnut-boundary detection (a found walnut is a leaf for traversal
    purposes; walnuts don't nest) replace depth-limiting.
    Plugin-vendored template directories are excluded so a world with
    vendored templates does NOT register them as walnuts.

    Returns absolute paths sorted lexically. Includes ``world_root``
    itself if the world looks v1 (has ``_core/`` OR ``companion.md``
    OR ``now.md`` at root).

    The function reads the live filesystem (NOT the snapshot) because
    walnut discovery happens BEFORE snapshot population -- the
    orchestrator needs the walnut list to scope per-walnut snapshot
    rules. Detection-side reads after snapshot-build go through
    ``snapshot.read`` per the R7 invariant.
    """
    world_root = os.path.abspath(world_root)
    discovered: List[str] = []

    # World-as-walnut detection.
    is_w, _note = _world_root_is_walnut(world_root)
    if is_w:
        discovered.append(world_root)

    # Recursive walk. We do NOT pass topdown=False because we mutate
    # ``dirs[:]`` to prune at runtime (skip-list, dot-dir filter,
    # walnut-boundary, template exclusion).
    for root, dirs, _files in os.walk(world_root):
        rel = os.path.relpath(root, world_root)
        if rel == ".":
            rel_segments: Tuple[str, ...] = ()
        else:
            rel_segments = tuple(rel.split(os.sep))

        # Template exclusion -- never recurse into vendored templates.
        if _is_under_template_dir(rel_segments):
            dirs[:] = []
            continue

        # Skip the world root itself for walnut-detection (already
        # handled by world-as-walnut above).
        if rel != ".":
            is_walnut, _marker = _walnut_markers(root)
            if is_walnut:
                discovered.append(root)
                # Walnut boundary: do NOT descend into the walnut.
                dirs[:] = []
                continue

        # Filter children:
        #   - hidden dirs (start with ".") allowed only if in allow-set
        #   - explicit skip-list always pruned
        #   - archived-snapshot prefix (walnut-duplicates-*) pruned so
        #     snapshot copies of walnuts the operator already moved
        #     aside don't re-register and force the cleanup / migration
        #     phases to mutate the preserved copies.
        kept: List[str] = []
        for d in dirs:
            if d in _LEGACY_SKIP_DIRS:
                continue
            if d.startswith(".") and d not in _LEGACY_ALLOW_DOT_DIRS:
                continue
            if _is_archived_snapshot_dir(d):
                continue
            kept.append(d)
        dirs[:] = kept

    # Dedup by realpath so that a symlink-aliased walnut doesn't
    # produce duplicates. Preserve sort order via realpath -> path map.
    seen: Set[str] = set()
    out: List[str] = []
    for p in sorted(discovered):
        try:
            rp = os.path.realpath(p)
        except OSError:
            rp = p
        if rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    return out


def union_walnuts(world_root: str) -> Tuple[List[str], List[str]]:
    """Return ``(union_walnut_list, legacy_only_walnut_list)``.

    The union is the deduplicated set of walnuts found by
    ``_common.find_all_walnuts`` plus :func:`discover_walnuts_legacy_aware`.
    The legacy-only list is the set difference (legacy finder found,
    canonical finder did not).
    """
    # Lazy import to avoid putting ``_common`` on the orchestrator
    # import path; ``_common`` requires ``scripts/`` on sys.path which
    # is a callsite invariant, not a package one.
    from _common import find_all_walnuts  # noqa: PLC0415

    canonical = [os.path.abspath(p) for p in find_all_walnuts(world_root)]
    legacy = [os.path.abspath(p) for p in discover_walnuts_legacy_aware(world_root)]

    canonical_realpaths: Set[str] = set()
    canon_by_real: Dict[str, str] = {}
    for p in canonical:
        try:
            rp = os.path.realpath(p)
        except OSError:
            rp = p
        canonical_realpaths.add(rp)
        canon_by_real.setdefault(rp, p)

    union: List[str] = list(canonical)
    legacy_only: List[str] = []
    seen_real: Set[str] = set(canonical_realpaths)
    for p in legacy:
        try:
            rp = os.path.realpath(p)
        except OSError:
            rp = p
        if rp in canonical_realpaths:
            continue
        if rp in seen_real:
            continue
        seen_real.add(rp)
        union.append(p)
        legacy_only.append(p)
    union.sort()
    legacy_only.sort()
    return union, legacy_only


# ---------------------------------------------------------------------------
# Snapshot rule contributions (orchestrator merges these into the combined
# allowlist before populate)
# ---------------------------------------------------------------------------

def snapshot_rule_contributions() -> List[Any]:
    """Aggregate all signal-source ``SnapshotRule`` contributions.

    The orchestrator (T1) calls this and merges the result into the
    combined snapshot allowlist before ``FileSnapshot.populate``.
    """
    from .file_snapshot import SnapshotRule  # noqa: PLC0415

    rules: List[Any] = []
    rules.extend(_path.snapshot_rule_contributions())
    rules.extend(_schema.snapshot_rule_contributions())
    rules.extend(_content.snapshot_rule_contributions())
    # Prior-upgrade canonical records (T20 / R20 floor lift). Detection
    # reads the most recent canonical record's ``tool_version_at_run``
    # to lift the world floor after demo_cleanup removes the on-disk
    # v3.2 fingerprint. The codec needs FULL content (no head cap) so
    # the YAML decoder always sees a complete document.
    rules.append(SnapshotRule(
        glob="<world>/.alive/upgrades/*.yaml",
    ))
    return rules


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _ver_tuple(v: str) -> Tuple[int, ...]:
    """Convert ``"3.2"`` / ``"3.2.0"`` -> ``(3, 2, 0)``.

    Mirrors ``_normalize_version`` semantics but does NOT raise on
    short-forms; pads with zeros. Non-numeric segments raise ValueError
    (caller normalizes inputs upstream so this is defensive).
    """
    raw = v.strip()
    if raw.lower().startswith("v"):
        raw = raw[1:]
    parts = raw.split(".")
    out: List[int] = []
    for p in parts:
        if not p or not p.isdigit():
            raise ValueError("non-numeric version segment: {!r}".format(v))
        out.append(int(p))
    while len(out) < 3:
        out.append(0)
    return tuple(out)


# ---------------------------------------------------------------------------
# Prior-record floor lift helper
# ---------------------------------------------------------------------------
#
# A successful tool-completed upgrade durably writes a canonical
# record at ``<world>/.alive/upgrades/<ts>.yaml``. Detection consults
# the most recent such record's ``world_version`` as a floor lift so
# that after demo_cleanup removes ``_stage_outputs/.demo-state.yaml``,
# the world still resolves at target. Without this, the second run of
# an upgrade against an already-upgraded world would see a v3.1 floor
# (no v3.2 fingerprint signal left on disk) and refuse to no-op.

import re as _re  # noqa: E402

_CANONICAL_RECORD_RE = _re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:-\d+)?\.yaml$"
)
_PRIOR_RECORD_SUPPORTED_SCHEMA: frozenset = frozenset({"1"})


def _read_prior_record_world_version(
    snapshot: Any, world_root: str,
) -> Optional[str]:
    """Return the most recent canonical upgrade record's post-upgrade
    target version (the ``tool_version_at_run`` of a successful run),
    or ``None``.

    Mirrors ``surfaces.load_prior_final_record``'s discovery rules:
    strict canonical filename pattern (no ``-resume`` / ``-runstate``
    / ``-retroactive`` siblings), filename-timestamp sort, schema-
    version gating against the supported set.

    Snapshot-only contract (R7): all reads route through
    ``snapshot.paths()`` / ``snapshot.read()``. The orchestrator's
    snapshot rule for ``<world>/.alive/upgrades/*.yaml`` (added in
    :func:`snapshot_rule_contributions`) ensures the canonical records
    are captured so detection never touches live disk.

    Why ``tool_version_at_run`` and not ``world_version``: the
    canonical record's ``world_version`` field captures the
    PRE-upgrade detection result (per the canary test contract --
    the world_version surfaces the inferred floor of source-version
    signals at run start). A successful canonical record (the only
    kind this canonical filename pattern matches) means the orchestrator
    completed phase 12 = the world is at ``tool_version_at_run`` after
    cleanup + migration. That is the floor we want to lift detection
    to on subsequent runs against the same world.
    """
    upgrades_dir = os.path.join(world_root, ".alive", "upgrades")  # DERIVED: prefix-filter against snapshot.paths(); the bytes themselves arrive via snapshot.read, not Path.read_*
    upgrades_with_sep = upgrades_dir.rstrip(os.sep) + os.sep
    candidates: List[str] = []
    for path in snapshot.paths():
        if not path.startswith(upgrades_with_sep):
            continue
        # Only direct children of upgrades/, not nested.
        rel = path[len(upgrades_with_sep):]
        if os.sep in rel:
            continue
        if _CANONICAL_RECORD_RE.match(rel):
            candidates.append(path)
    if not candidates:
        return None
    candidates.sort()
    latest = candidates[-1]
    try:
        blob = snapshot.read(latest)
    except (KeyError, ValueError):
        return None
    # Codec writes JSON-text-as-YAML (see _record_codec.write_atomic),
    # so json.loads is the parse path. Stdlib-only per R10.
    import json as _json  # noqa: PLC0415
    try:
        record = _json.loads(blob.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    sv = record.get("schema_version")
    if sv not in _PRIOR_RECORD_SUPPORTED_SCHEMA:
        return None
    tv = record.get("tool_version_at_run")
    if isinstance(tv, str) and tv:
        return tv
    return None


# ---------------------------------------------------------------------------
# Probe-polarity classifier (≥X floor vs ≤X ceiling vs ==X identity)
# ---------------------------------------------------------------------------
#
# Many probe ids encode a *floor* semantic ("presence imputes ≥X") rather
# than equality. ``ALIVE_PLUGIN_ROOT`` was introduced at v3.1 -- its
# presence imputes ≥3.1, NOT exactly 3.1. Likewise the v3.2
# demo-installed marker imputes ≥3.2. Without polarity awareness, a
# clean v3.2 world that uses ``ALIVE_PLUGIN_ROOT`` extensions resolves
# to v3.1 by lowest-wins, defeating the noop short-circuit gate.
#
# The fix: classify each probe id as floor / ceiling / identity. The
# resolver:
#   1. Picks the lowest-ceiling version from non-floor signals
#      (lowest-wins on ceiling-class signals).
#   2. Lifts the result to the highest floor when floor signals fire
#      (a v3.2 floor cannot resolve below 3.2).
#   3. When ONLY floor signals fire, returns the highest floor.
#
# This preserves lowest-wins semantics for retirement / pre-X markers
# (which truly cap the version) while preventing positive ≥X markers
# from pulling the version DOWN from a higher floor.
#
# Probe-id taxonomy (kept in sync with ``signals/`` source modules):
#   floor probes (presence imputes ≥X):
#     * ``v32_demo_state_yaml``                    -- ≥3.2
#     * ``content_alive_plugin_root``              -- ≥3.1
#     * ``schema_canonical_v31@<bn>``              -- ≥3.1
#   ceiling/identity probes (presence imputes ≤X or ==X):
#     * ``v1_*`` / ``v2_*``                        -- 1.0 / 2.0 identity
#     * ``v1_walnut_core_bare``                    -- 1.0 identity
#     * ``pre_v31_*``                              -- ≤3.0 ceiling
#     * ``schema_pre_canonical@<bn>``              -- ≤3.0 ceiling
#     * ``content_hardcoded_plugin_path``          -- ≤3.0 ceiling
#
# ``v3_walnut_kernel_key`` is presence-only (``inferred_version=None``)
# and never reaches the resolver here -- it's handled separately via
# the v3-kernel-floor default-floor logic.

#: Stable probe-id prefixes / exact ids that carry FLOOR (≥X) semantics.
_FLOOR_PROBE_IDS: frozenset = frozenset({
    "v32_demo_state_yaml",
    "content_alive_plugin_root",
})

#: Probe-id prefixes (before ``@<bn>``) that carry FLOOR (≥X) semantics
#: at walnut scope. The ``@<bn>`` suffix keys per-walnut probes to a
#: walnut basename; we strip it before the prefix check.
_FLOOR_PROBE_PREFIXES: Tuple[str, ...] = (
    "schema_canonical_v31",
    "schema_canonical_v32",  # codex completion-review fix
)


def _is_floor_probe(probe_id: str) -> bool:
    """Return True iff *probe_id* is a FLOOR (≥X) signal.

    Floor probes do not participate in the ceiling lowest-wins pass --
    instead the resolver lifts the result to the highest floor.
    """
    if probe_id in _FLOOR_PROBE_IDS:
        return True
    base = probe_id.split("@", 1)[0]
    return base in _FLOOR_PROBE_PREFIXES


def _resolve_lowest_wins(
    fired: List[SignalProbe],
) -> Optional[str]:
    """Resolve a list of fired probes to a single version string.

    Polarity-aware: floor probes
    raise the floor; ceiling/identity probes participate in the
    lowest-wins ceiling pass. Final = ``max(min(ceilings), max(floors))``.

    Source-rank order on ceiling ties: ``path > schema > content``
    (lower ``SOURCE_RANK`` wins on ties).

    Returns ``None`` when no probe with a usable inferred version fires.
    """
    if not fired:
        return None
    ceiling_candidates: List[Tuple[Tuple[int, ...], int, str]] = []
    floor_candidates: List[Tuple[Tuple[int, ...], str]] = []
    for p in fired:
        if p.inferred_version is None:
            continue
        try:
            vt = _ver_tuple(p.inferred_version)
        except ValueError:
            continue
        if _is_floor_probe(p.probe_id):
            floor_candidates.append((vt, p.inferred_version))
        else:
            ceiling_candidates.append(
                (vt, SOURCE_RANK[p.source], p.inferred_version)
            )

    # No usable candidates at all -> no resolution.
    if not ceiling_candidates and not floor_candidates:
        return None

    # Lowest-wins on ceilings (lowest version first; lower source-rank
    # breaks ties).
    if ceiling_candidates:
        ceiling_candidates.sort(key=lambda x: (x[0], x[1]))
        ceiling_vt, _rank, ceiling_str = ceiling_candidates[0]
    else:
        ceiling_vt, ceiling_str = None, None  # type: ignore[assignment]

    # Highest floor among floor probes.
    if floor_candidates:
        floor_candidates.sort(key=lambda x: x[0], reverse=True)
        floor_vt, floor_str = floor_candidates[0]
    else:
        floor_vt, floor_str = None, None  # type: ignore[assignment]

    # Combine ceiling and floor:
    #
    # * Only floors fired -> use the highest floor (no upper bound
    #   evidence, so the highest "≥X" claim governs).
    # * Only ceilings fired -> use the lowest ceiling (lowest-wins
    #   classic).
    # * Both fired AND ceiling >= floor -> ceiling governs (the lowest
    #   ceiling is a valid upper bound and is consistent with the
    #   floor; we still report the ceiling because it carries the
    #   migration-pending semantic for retired-pattern markers).
    # * Both fired AND ceiling < floor -> conflict (e.g. walnut has
    #   _kernel/atoms vestige imputing ≤3.0 AND canonical v3.1 schema
    #   imputing ≥3.1). The ceiling still governs: a ceiling marker
    #   means there is concrete vestigial state on disk that migration
    #   must clear, so we surface the lower version to drive the
    #   walkthrough/migration paths. The floor ≥3.1 is forensic evidence
    #   of partial progress, not cause to skip migration.
    if ceiling_vt is None:
        return floor_str
    return ceiling_str


# ---------------------------------------------------------------------------
# Snapshot-derived walnut discovery (snapshot-only fallback path)
# ---------------------------------------------------------------------------

def _derive_walnuts_from_snapshot(
    snapshot: Any, world_root: str,
) -> Tuple[List[str], List[str]]:
    """Return ``(union_walnut_list, legacy_only_walnut_list)`` from snapshot.

    Walks the snapshot's captured paths -- every path the orchestrator's
    combined allowlist covered -- and infers walnut directories from
    walnut-marker tails. Plugin-vendored template trees are excluded so
    a world with vendored templates does NOT register them as walnuts.

    Marker priority (high -> low). Higher-priority markers carry the
    "walnut here" signal more authoritatively; when a candidate from a
    weaker marker is a strict descendant of a stronger-marker walnut,
    the descendant is dropped (walnut-boundary semantics: walnuts
    don't nest, and bundle-internal files with marker filenames must
    not be invented as walnut roots).

    Markers consulted (priority order; bare ``_kernel/`` and ``_core/``
    directories are NOT walnut markers -- both can occur as non-walnut
    fixtures):
      1. ``_kernel/key.md``         -- v3 walnut key file (strongest)
      2. ``_core/key.md``           -- v2 walnut key file
      3. ``_core/_squirrels/``      -- v2 walnut alt-marker
      4. ``_kernel/_generated/``    -- v2.5 in-flight upgrade walnut
      5. ``companion.md``           -- v1 walnut marker (weakest;
                                       bundle companion.md files are
                                       pruned by the boundary pass)

    World-as-walnut detection (root-of-world v1/v2 layouts) -- the
    world root IS a walnut when its snapshot carries any of
    ``_core/`` (incl. ``_core/key.md`` / ``_core/_squirrels/``),
    ``companion.md``, or ``now.md`` directly at the root. This
    matches the live legacy-aware finder's contract
    (:func:`discover_walnuts_legacy_aware`) so the snapshot-derived
    path produces an identical walnut list to the orchestrator-
    supplied path when the snapshot covers the same paths. Note that
    BARE NESTED ``_core/`` (i.e. ``<world>/misc/_core/``) is still
    NOT treated as a walnut anywhere else -- the world-root special
    case is the only place we accept bare ``_core/`` as a marker.

    legacy_only is the set of derived walnuts that would NOT be found
    by ``_common.find_all_walnuts``. The canonical finder requires
    ``_kernel/key.md`` AND a canonical-domain parent (``01_Archive``,
    ``02_Life``, ``04_Ventures``, ``05_Experiments``). Anything else
    -- v2 walnuts even under canonical domains, v3 walnuts outside
    canonical domains, world-as-walnut layouts -- is legacy.
    """
    world_root = os.path.abspath(world_root)

    def _segments_after_world(p: str) -> Tuple[str, ...]:
        try:
            rel = os.path.relpath(p, world_root)
        except ValueError:
            return ()
        if rel == ".":
            return ()
        return tuple(rel.split(os.sep))

    # Bucket candidates by priority; lower-priority candidates that
    # nest under a higher-priority walnut are pruned in the boundary
    # pass below. Bare ``_kernel/`` and ``_core/`` directories are
    # intentionally NOT markers -- a directory named ``_kernel`` or
    # ``_core`` anywhere in the world must not register as a walnut
    # without a real walnut-content file (key.md / _squirrels /
    # _generated) inside it. This matches both the canonical finder
    # and the legacy-aware finder's invariants.
    P_KERNEL_KEY = 0  # _kernel/key.md       (v3 walnut)
    P_CORE_KEY = 1    # _core/key.md         (v2 walnut)
    P_SQUIRRELS = 2   # _core/_squirrels/    (v2 walnut alt)
    P_GENERATED = 3   # _kernel/_generated/  (v2.5 in-flight)
    P_COMPANION = 4   # companion.md         (v1; weakest, prunable)

    # candidates: dict[walnut_path, priority]
    candidates: Dict[str, int] = {}
    # kernel_key_paths: subset of candidates that fired _kernel/key.md
    # (used downstream for legacy-only classification).
    kernel_key_paths: Set[str] = set()

    def _add(path: str, prio: int) -> None:
        prev = candidates.get(path)
        if prev is None or prio < prev:
            candidates[path] = prio

    # World-as-walnut detection. The world root IS a walnut when its
    # snapshot carries any of:
    #   * companion.md / now.md       -- v1 root markers (v1 walnut)
    #   * _core/key.md                -- v2 walnut at root
    #   * _core/_squirrels            -- v2 walnut alt-marker at root
    #   * _core/                      -- legacy v1/v2 finder still
    #                                    accepts a bare _core/ at the
    #                                    world root (see
    #                                    :func:`_world_root_is_walnut`)
    # Bare nested ``_core/`` elsewhere (e.g. ``<world>/misc/_core/``)
    # is NOT a walnut marker -- only the root is special-cased.
    if snapshot.exists(os.path.join(world_root, "companion.md")):
        _add(world_root, P_COMPANION)
    if snapshot.exists(os.path.join(world_root, "now.md")):
        _add(world_root, P_COMPANION)
    if snapshot.exists(os.path.join(world_root, "_core", "key.md")):
        _add(world_root, P_CORE_KEY)
    if snapshot.exists(os.path.join(world_root, "_core", "_squirrels")):
        _add(world_root, P_SQUIRRELS)
    if snapshot.exists(os.path.join(world_root, "_core")):
        # Last-resort root marker (matches live finder's
        # ``_world_root_is_walnut`` predicate). Lower priority than
        # the explicit content markers above so a root v2 walnut with
        # _core/key.md still classifies via P_CORE_KEY.
        _add(world_root, P_COMPANION)

    for path in snapshot.paths():
        segs = _segments_after_world(path)
        if not segs:
            continue
        if _is_under_template_dir(segs):
            continue

        # File-tail markers (highest priority)
        if len(segs) >= 2 and segs[-2:] == ("_kernel", "key.md"):
            walnut_segs = segs[:-2]
            if walnut_segs:
                walnut_path = os.path.join(world_root, *walnut_segs)
                _add(walnut_path, P_KERNEL_KEY)
                kernel_key_paths.add(walnut_path)
        if len(segs) >= 2 and segs[-2:] == ("_core", "key.md"):
            walnut_segs = segs[:-2]
            if walnut_segs:
                _add(os.path.join(world_root, *walnut_segs), P_CORE_KEY)
        if len(segs) >= 2 and segs[-2:] == ("_core", "_squirrels"):
            walnut_segs = segs[:-2]
            if walnut_segs:
                _add(os.path.join(world_root, *walnut_segs), P_SQUIRRELS)
        if len(segs) >= 2 and segs[-2:] == ("_kernel", "_generated"):
            walnut_segs = segs[:-2]
            if walnut_segs:
                _add(os.path.join(world_root, *walnut_segs), P_GENERATED)
        if len(segs) >= 1 and segs[-1] == "companion.md":
            walnut_segs = segs[:-1]
            if walnut_segs:
                _add(os.path.join(world_root, *walnut_segs), P_COMPANION)

    # Walnut-boundary pruning: drop candidates that nest under a
    # higher-priority candidate. The result mirrors the live-disk
    # ``discover_walnuts_legacy_aware`` invariant that walnuts don't
    # nest: a walnut found is a leaf for discovery purposes. We
    # protect higher-priority entries explicitly so a v3 walnut at
    # ``alpha/`` doesn't get clobbered by a v1 ``alpha/cap1/companion.md``
    # that the snapshot also captured.
    sorted_paths = sorted(candidates.keys(), key=lambda p: (p.count(os.sep), p))
    survivors: List[str] = []
    for path in sorted_paths:
        prio = candidates[path]
        # Drop if any survivor strictly contains this path AND that
        # survivor is at strictly-higher priority (lower number).
        nested_under_stronger = False
        for parent in survivors:
            if path == parent:
                continue
            if path.startswith(parent.rstrip(os.sep) + os.sep):
                if candidates[parent] < prio:
                    nested_under_stronger = True
                    break
                # Same-or-weaker priority parent: still drop the
                # nested entry (walnut-boundary). This matches the
                # live-disk finder which never recurses into a
                # discovered walnut.
                if candidates[parent] <= prio:
                    nested_under_stronger = True
                    break
        if not nested_under_stronger:
            survivors.append(path)

    # Dedup by realpath (consistent with discover_walnuts_legacy_aware).
    seen: Set[str] = set()
    union: List[str] = []
    for p in sorted(survivors):
        try:
            rp = os.path.realpath(p)
        except OSError:
            rp = p
        if rp in seen:
            continue
        seen.add(rp)
        union.append(p)

    # Legacy-only mirrors the predicate used by ``_common.find_all_walnuts``:
    # canonical iff walnut has ``_kernel/key.md`` AND lives under a
    # canonical numbered domain. Anything else is legacy -- v2 walnuts
    # (under canonical OR non-canonical parents), v3 walnuts outside
    # canonical domains, and the world-as-walnut case.
    canonical_domains = ("01_Archive", "02_Life", "04_Ventures", "05_Experiments")
    legacy_only: List[str] = []
    for w in union:
        rel = os.path.relpath(w, world_root)
        if rel == "." or rel == "":
            legacy_only.append(w)
            continue
        first_seg = rel.split(os.sep)[0] if rel else ""
        is_canonical = (
            w in kernel_key_paths
            and first_seg in canonical_domains
        )
        if not is_canonical:
            legacy_only.append(w)
    return union, legacy_only


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_world_version(
    snapshot: Any,
    world_root: str,
    catalog: Optional[List[Any]] = None,
    *,
    walnuts: Optional[List[str]] = None,
    legacy_walnuts: Optional[List[str]] = None,
    plugin_root: Optional[str] = None,
    assume_empty_world: bool = False,
) -> DetectionReport:
    """Build the detection report for *world_root* from *snapshot*.

    Parameters
    ----------
    snapshot : FileSnapshot
        Frozen view of the world (built by phase 2). Detection reads
        bytes via ``snapshot.read`` and existence via ``snapshot.exists``.
        No live disk reads are performed by this function.
    world_root : str
        Resolved absolute path of the target world.
    catalog : list[RetiredPattern] | None
        Retired-pattern catalog, defaults to :data:`CATALOG`. Passed
        through to :func:`match_walkthrough_eligible`.
    plugin_root : str | None
        Plugin install root for ``read_tool_version``. When ``None``,
        ``tool_version_at_run`` lands as ``"unknown"``.
    assume_empty_world : bool
        Phase-3 bypass for fresh empty worlds. Honoured ONLY when
        ``_kernel/`` absent AND walnut discovery empty AND zero
        world-scope signals fired; otherwise raises
        :class:`DetectionRefusal`.
    walnuts : list[str] | None
        Optional pre-discovered walnut union. The orchestrator (T1)
        runs ``union_walnuts()`` BEFORE snapshot population so per-
        walnut snapshot rules can be scoped against the discovered
        list, then threads the result through here. When omitted,
        detection derives the walnut list from the snapshot's
        captured paths (every walnut-marker file -- ``_kernel/key.md``,
        ``_core/key.md``, ``companion.md``, ``_core/_squirrels/``,
        ``_kernel/_generated/`` -- under a directory makes that dir a
        walnut), preserving the snapshot-only contract without forcing
        callers to know about a side-channel. Either path produces an
        identical walnut list when the snapshot's allowlist covers the
        same paths (which it does by construction -- see
        :func:`snapshot_rule_contributions`).
    legacy_walnuts : list[str] | None
        Subset of *walnuts* found by the legacy-aware finder but NOT
        by ``_common.find_all_walnuts``. Recorded on the report for T9
        migration awareness. Defaults to an empty list when *walnuts*
        is also derived from the snapshot.

    Returns
    -------
    DetectionReport
        ``world_version`` is the lowest-wins resolution of world-scope
        probes; ``per_walnut_versions`` is the per-walnut resolution
        keyed by absolute walnut path (so distinct walnuts with
        identical basenames -- e.g. ``04_Ventures/alpha`` vs
        ``05_Experiments/alpha`` -- never collide); ``all_signals_raw``
        carries every probe (fired and absent) plus the union walnut
        list and catalog-match summary for forensic visibility.

    Raises
    ------
    DetectionRefusal
        On zero-signal worlds without a valid bypass, or on
        ``assume_empty_world`` supplied against a non-empty world.
    """
    cat = catalog if catalog is not None else CATALOG
    world_root = os.path.abspath(world_root)

    # World-scope path probes (v1, pre-v3.1, v3.2 markers).
    world_path_probes = _path.world_probes(snapshot, world_root)
    # World-scope content probes (ALIVE_PLUGIN_ROOT vs hardcoded).
    world_content_probes = _content.world_probes(snapshot, world_root)

    # Walnut union: caller may supply pre-discovered lists, or detection
    # derives them from the snapshot's captured paths. Either way,
    # detection itself touches NO live filesystem -- any disk mutation
    # between snapshot populate and detect cannot change the answer
    # because both code paths read from the snapshot exclusively.
    if walnuts is None:
        union, derived_legacy = _derive_walnuts_from_snapshot(
            snapshot, world_root,
        )
        legacy_only = (
            [os.path.abspath(p) for p in legacy_walnuts]
            if legacy_walnuts is not None else derived_legacy
        )
    else:
        union = [os.path.abspath(p) for p in walnuts]
        legacy_only = (
            [os.path.abspath(p) for p in legacy_walnuts]
            if legacy_walnuts is not None else []
        )

    # Per-walnut versions keyed by absolute walnut path so distinct
    # walnuts with identical basenames never collide. The orchestrator
    # + T9 / T10 migrators read the map and group by full path; T9
    # generates per-walnut migration plans against the absolute path,
    # not the basename.
    per_walnut_versions: Dict[str, str] = {}
    # Track walnuts whose version came purely from the kernel-floor
    # default (no version-imputing probe fired). These walnuts have no
    # pending walnut-affecting migration -- the catalog's retired
    # patterns and bundle-schema signals all reported absent. The
    # additive-lift block below uses this map to lift them to the
    # world version when world is at-or-above target, so the no-op
    # short-circuit gate can close on worlds with bundleless walnuts.
    per_walnut_floor_only: Dict[str, bool] = {}
    all_walnut_probes: List[SignalProbe] = []
    walnut_v3_kernel_present = False
    for walnut_path in union:
        wpath_probes = _path.walnut_probes(snapshot, walnut_path)
        all_walnut_probes.extend(wpath_probes)
        wschema_probes = _schema.walnut_probes(snapshot, walnut_path)
        all_walnut_probes.extend(wschema_probes)

        # Per-walnut resolution: combine path + schema probes for THIS
        # walnut. Track v3-kernel presence for the empty-world gating.
        per_walnut_fired = [
            p for p in wpath_probes + wschema_probes
            if p.fired and p.inferred_version is not None
        ]
        # Default-floor logic: a walnut whose `_kernel/key.md` exists
        # but no probe fires lands at the v3.0 floor (the kernel is
        # the v3 baseline).
        kernel_present = any(
            p.probe_id.startswith("v3_walnut_kernel_key@") and p.fired
            for p in wpath_probes
        )
        if kernel_present:
            walnut_v3_kernel_present = True
        resolved = _resolve_lowest_wins(per_walnut_fired)
        if resolved is None:
            if kernel_present:
                resolved = _V3_KERNEL_FLOOR
                per_walnut_floor_only[walnut_path] = True
            else:
                # Walnut with no version-imputing signals at all -- omit
                # rather than fabricate. The forensic payload still
                # carries the absent probes.
                continue
        else:
            per_walnut_floor_only[walnut_path] = False
        per_walnut_versions[walnut_path] = resolved

    # Detect world-scope kernel presence (a world-root v3 kernel implies
    # the world IS a walnut at v3.0+ floor).
    world_kernel_present = snapshot.exists(
        os.path.join(world_root, "_kernel", "key.md")
    )

    # World-scope resolution.
    world_fired = [
        p for p in world_path_probes + world_content_probes
        if p.fired and p.inferred_version is not None
    ]
    world_resolved = _resolve_lowest_wins(world_fired)
    if world_resolved is None:
        if world_kernel_present:
            world_resolved = _V3_KERNEL_FLOOR
        else:
            world_resolved = ""

    # ------------------------------------------------------------------
    # Quorum / empty-world gating.
    # ------------------------------------------------------------------
    # World scope is "empty of signals" when:
    #   * no world-scope probe fired (path or content), AND
    #   * no walnut was discovered, AND
    #   * no _kernel/ at world root.
    empty_world = (
        not any(p.fired for p in world_path_probes + world_content_probes)
        and len(union) == 0
        and not world_kernel_present
    )

    if assume_empty_world:
        if not empty_world:
            # The bypass is invalid -- world has actual content.
            raise DetectionRefusal(
                code="assume_empty_world_invalid",
                message=(
                    "--assume-empty-world supplied but the world is not "
                    "empty: kernel_present={}, walnuts_discovered={}, "
                    "any_world_signal_fired={}.".format(
                        world_kernel_present,
                        len(union),
                        any(
                            p.fired for p in
                            world_path_probes + world_content_probes
                        ),
                    )
                ),
            )
        # Empty-world ack: world resolves to baseline; per-walnut map empty.
        world_resolved = _BASELINE_VERSION
    elif world_resolved == "" and len(per_walnut_versions) == 0:
        # No signals AND no per-walnut inference AND no bypass -> refuse.
        path_classes = sorted({
            p.detail.split(":", 1)[1] if ":" in p.detail else p.detail
            for p in world_path_probes + world_content_probes
        })
        raise DetectionRefusal(
            code="no_signals",
            message=(
                "no world-fingerprint signals fired (path classes "
                "considered: {}). "
                "tool_version=plugin.json says version=X but no world "
                "signals fire -- possible fresh empty world; pass "
                "--assume-empty-world to proceed treating as v0/baseline."
                .format(path_classes)
            ),
        )

    # When world_resolved is "" but per-walnut inference ran, lift the
    # world version to the lowest per-walnut version (a world with
    # walnuts but no world-root signals is a v3-style world; the
    # walnut floor governs).
    if world_resolved == "" and per_walnut_versions:
        try:
            world_resolved = min(
                per_walnut_versions.values(), key=_ver_tuple,
            )
        except ValueError:
            world_resolved = _V3_KERNEL_FLOOR

    # Prior-record floor lift (codex completion-review fix for R20).
    #
    # A successful tool-completed upgrade emits a canonical record at
    # ``<world>/.alive/upgrades/<ts>.yaml`` carrying the post-upgrade
    # ``world_version``. After demo_cleanup removes ``_stage_outputs/``
    # the on-disk v3.2 floor signal disappears, but the upgrade record
    # is durable evidence the world reached target. Lifting world to
    # the record's ``world_version`` (when ``world_resolved`` is below
    # it) keeps idempotency: a second run sees an at-target world and
    # short-circuits.
    #
    # Only the canonical pattern (no -resume / -runstate / -retroactive
    # suffix) qualifies. Schema-version gating mirrors
    # surfaces.load_prior_final_record: an unknown future schema is
    # ignored (forgiving posture).
    try:
        prior_floor = _read_prior_record_world_version(snapshot, world_root)
    except Exception:  # noqa: BLE001 -- never let detection crash on a bad record
        prior_floor = None
    if prior_floor is not None and world_resolved:
        try:
            world_tuple = _ver_tuple(world_resolved)
            prior_tuple = _ver_tuple(prior_floor)
        except ValueError:
            world_tuple = prior_tuple = None
        if (
            world_tuple is not None
            and prior_tuple is not None
            and prior_tuple > world_tuple
        ):
            world_resolved = prior_floor

    # Per-walnut additive lift (codex completion-review fix for R20).
    #
    # The v3.1 -> v3.2 transition is purely additive at world-scope
    # (demo state install, plugin manifest bump) -- no user-world
    # walnut migration runs at that step. There is therefore no
    # walnut-scope v3.2 fingerprint signal, so a walnut inside a
    # clean v3.2 world legitimately resolves to "3.1" via
    # ``schema_canonical_v31`` (the highest walnut floor).
    #
    # The R20 strict-equality predicate (orchestrator.should_short_circuit)
    # demands every walnut version == TARGET_WORLD_VERSION. Without this
    # lift a clean v3.2 world's walnuts would never satisfy the gate,
    # forcing the full pipeline to run on every already-current world.
    #
    # Two lift branches:
    #
    # (1) v3.1+ canonical-bundle lift -- a walnut whose schema probe
    #     fired at the highest walnut-affecting floor (>= 3.1) has no
    #     pending walnut-affecting migration. Lift to world version.
    #
    # (2) Floor-only lift -- a walnut whose `_kernel/key.md` is present
    #     but emitted ZERO version-imputing probes (no path retired
    #     pattern fired, no bundle-schema fingerprint fired) resolved
    #     purely from the v3.0 kernel-floor default. There is nothing
    #     for the catalog or walkthrough surfaces to do; the v3.0
    #     reading is a label, not pending work. Lift to world version
    #     so the no-op gate can close. This is the bundleless walnut
    #     case (e.g. a person walnut with `_kernel/key.md` and no
    #     bundles at the walnut root).
    #
    # In both branches the lift is bounded by world version (never
    # raises walnut above world). A walnut that detects below target
    # from a REAL signal -- e.g. `pre_v31_walnut_generated` firing 3.0
    # because `_kernel/_generated/` exists, or `schema_pre_canonical@<bn>`
    # firing 3.0 from a non-canonical bundle frontmatter -- is left
    # untouched: those represent genuine pending migrations that the
    # cleanup / migration / walkthrough phases own.
    from . import TARGET_WORLD_VERSION  # noqa: PLC0415 -- avoid circular at module load
    try:
        target_tuple = _ver_tuple(TARGET_WORLD_VERSION)
    except ValueError:
        target_tuple = None
    if target_tuple is not None and world_resolved:
        try:
            world_tuple = _ver_tuple(world_resolved)
        except ValueError:
            world_tuple = None
        if world_tuple is not None and world_tuple >= target_tuple:
            v31_floor = _ver_tuple("3.1")
            for wpath, wver in list(per_walnut_versions.items()):
                try:
                    wtup = _ver_tuple(wver)
                except ValueError:
                    continue
                if wtup >= world_tuple:
                    continue
                # Branch (1): v3.1+ canonical-bundle walnut.
                if wtup >= v31_floor:
                    per_walnut_versions[wpath] = world_resolved
                    continue
                # Branch (2): floor-only walnut (kernel present, zero
                # version-imputing probes fired). Resolution came from
                # the kernel-floor default, not a real signal.
                if per_walnut_floor_only.get(wpath, False):
                    per_walnut_versions[wpath] = world_resolved

    # ------------------------------------------------------------------
    # Catalog walkthrough matches (T4).
    # ------------------------------------------------------------------
    matches: List[CatalogMatch] = match_walkthrough_eligible(
        snapshot, cat, world_root=world_root,
    )

    # ------------------------------------------------------------------
    # Tool version at run (separate from world signals).
    # ------------------------------------------------------------------
    if plugin_root is not None:
        tool_version_at_run = read_tool_version(plugin_root)
    else:
        tool_version_at_run = "unknown"

    # ------------------------------------------------------------------
    # Assemble the report.
    # ------------------------------------------------------------------
    all_signals_raw: Dict[str, Any] = {
        "world_path_probes": [p.as_dict() for p in world_path_probes],
        "world_content_probes": [p.as_dict() for p in world_content_probes],
        "walnut_probes": [p.as_dict() for p in all_walnut_probes],
        "walnuts_union": list(union),
        "walnuts_legacy_only": list(legacy_only),
        "world_kernel_present": world_kernel_present,
        "empty_world": empty_world,
        "assume_empty_world": bool(assume_empty_world),
    }

    return DetectionReport(
        world_version=world_resolved,
        per_walnut_versions=per_walnut_versions,
        walkthrough_eligible_matches=list(matches),
        tool_version_at_run=tool_version_at_run,
        all_signals_raw=all_signals_raw,
        legacy_walnuts_discovered=list(legacy_only),
    )
