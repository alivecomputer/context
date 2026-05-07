"""Path-existence signal source.

Presence / absence of canonical paths under the world or a candidate
walnut imputes a version. The probes encode the layout history of the
ALIVE world format:

v1 markers:
    ``<world>/.walnut/``         -- v1 world-state directory
    ``<world>/_core/``           -- v1 layout (kernel at world root)
    ``<world>/_capsules/``       -- v1 capsule bundles
    ``<world>/companion.md``     -- v1 file
    ``<world>/now.md``           -- v1 (later folded into now.json)
    ``<world>/tasks.md``         -- v1/v2 file
    ``<world>/observations.md``  -- v1 file
    ``<world>/03_Inputs/``       -- v1 directory (renamed to 03_Inbox/)

v2 markers (per-walnut, pre-v3 layout):
    ``<walnut>/_core/``                -- pre-v3 walnut layout
    ``<walnut>/_capsules/``            -- pre-v3 walnut layout
    ``<walnut>/_core/_squirrels/``     -- pre-v3 walnut layout

v3.0 markers:
    ``<walnut>/_kernel/key.md``        -- flat kernel layout (≥v3.0)
    ``<walnut>/_kernel/atoms/``        -- pre-v3.1 atom-cache vestige
    ``<walnut>/_kernel/_generated/``   -- pre-v3.1 nested-generated layout

v3.1 markers (presence == ≥v3.1, absence with v3.0+ kernel == v3.0):
    ``<world>/.alive/scripts/``        -- pre-v3.1 (retired in 3.1; absence
                                          implies ≥v3.1 when other v3
                                          markers fire)
    ``<world>/.alive/atoms/``          -- pre-v3.1 vestige
    ``<world>/.alive/computed/``       -- pre-v3.1 vestige
    ``<world>/.alive/locks/``          -- pre-v3.1 vestige
    ``<world>/.alive/overrides.md``    -- pre-v3.1 file (path)
    ``<world>/.alive/upgrade-plan.html`` -- pre-v3.1 stray

v3.2 markers:
    ``<world>/_stage_outputs/.demo-state.yaml`` -- ≥v3.2 demo-installed

The probes never assume the snapshot captured *every* relevant path --
only paths declared in the orchestrator's combined snapshot allowlist
(see :func:`snapshot_rule_contributions`) are present. Probes that
reference paths the snapshot does not carry record ``fired=False`` with
``detail="not-in-snapshot"`` so the all-signals payload remains
faithful even if the allowlist is misconfigured.

Detection-lock-file exclusion (epic § Detection lock-file exclusion,
): pure ``.alive/`` existence is NOT a v3 signal --
preflight creates ``.alive/`` to host the upgrade lock before this
phase runs. Path probes target specific *retired-pattern* paths or
v3-marker paths under ``.alive/`` (``.alive/scripts/``, etc.), never
``.alive/`` itself.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from . import (
    SCOPE_WALNUT,
    SCOPE_WORLD,
    SOURCE_PATH,
    SignalProbe,
)


__all__ = (
    "world_probes",
    "walnut_probes",
    "snapshot_rule_contributions",
)


# ---------------------------------------------------------------------------
# Probe definitions (table-driven for reproducibility + test introspection)
# ---------------------------------------------------------------------------

# (probe_id, world-relative path, version when fires, detail)
_V1_WORLD_MARKERS = (
    ("v1_walnut_dir",         ".walnut",          "1.0", "v1 world-state directory"),
    ("v1_world_core",         "_core",            "1.0", "v1 root-level _core/ (kernel at world root)"),
    ("v1_world_capsules",     "_capsules",        "1.0", "v1 root-level _capsules/"),
    ("v1_world_companion",    "companion.md",     "1.0", "v1 root-level companion.md"),
    ("v1_world_now_md",       "now.md",           "1.0", "v1 root-level now.md"),
    ("v1_world_tasks_md",     "tasks.md",         "1.0", "v1/v2 root-level tasks.md"),
    ("v1_world_observations", "observations.md",  "1.0", "v1 root-level observations.md"),
    ("v1_world_03_inputs",    "03_Inputs",        "1.0", "v1 root-level 03_Inputs/ (renamed in v3)"),
)

# v3.1 retirements -- presence imputes a pre-v3.1 world. The full
# canonical list is the catalog (T4); we mirror those entries here as
# version signals so resolution can downgrade a candidate v3.1 world
# back to v3.0 when these strays are present.
_PRE_V31_WORLD_MARKERS = (
    # DERIVED: pre-v3.1 retired-path marker catalog. Each entry is a
    # (signal_id, relpath, version_floor, description) tuple. The
    # paths here are the SIGNAL DATA -- the catalog itself is the
    # version-detection contract. R5 audit exception: catalog
    # entries, not verifier callsites.
    ("pre_v31_alive_scripts",       ".alive/scripts",        "3.0", "pre-v3.1 alive scripts dir (retired f565c81)"),  # DERIVED: catalog
    ("pre_v31_alive_atoms",         ".alive/atoms",          "3.0", "pre-v3.1 alive atoms dir vestige"),  # DERIVED: catalog
    ("pre_v31_alive_computed",      ".alive/computed",       "3.0", "pre-v3.1 alive computed dir vestige"),  # DERIVED: catalog
    ("pre_v31_alive_locks",         ".alive/locks",          "3.0", "pre-v3.1 alive locks dir vestige"),  # DERIVED: catalog
    # ``.alive/overrides.md`` is NOT a pre-v3.1-only marker: v3 also
    # uses ``.alive/overrides.md`` as the canonical user-customisation
    # buffer (per the user-facing CLAUDE.md). Treating its presence as
    # a ≤3.0 ceiling caused the idempotency property to fail under
    # ``--surfaces=none`` (the catalog skips ``potentially_surface``
    # cleanup so the file legitimately survives, which dragged
    # detection back to v3.0 on the second run). Removed (codex
    # completion-review fix).
    ("pre_v31_alive_upgrade_plan",  ".alive/upgrade-plan.html", "3.0", "pre-v3.1 stray upgrade-plan html file"),  # DERIVED: catalog
    # Per epic spec section Approach (v3.x fingerprint paths) -- the
    # pre-v3.1 relay yaml at world-root alive dir is a path-existence
    # signal for the pre-v3.1 era. v3.1+ relay state lives elsewhere.
    ("pre_v31_alive_relay_yaml",    ".alive/relay.yaml",     "3.0", "pre-v3.1 alive relay yaml file"),  # DERIVED: catalog
)

# v3.2 demo-installed marker (positive -- presence raises floor).
_V32_WORLD_MARKERS = (
    ("v32_demo_state_yaml", "_stage_outputs/.demo-state.yaml", "3.2", "v3.2 demo-installed state file"),
)

# v1 walnut markers (root-of-world v1 layouts where the world IS a
# walnut). When these fire on a walnut path, the walnut resolves at
# 1.0 floor -- correct for "v1 world-as-walnut" cases. They overlap
# with the world-scope v1 markers; that's intentional (R16: scope-
# scoped fingerprinting), and lowest-wins means 1.0 still beats the
# v2_walnut_core 2.0 reading on the same path.
_V1_WALNUT_MARKERS = (
    ("v1_walnut_companion",   "companion.md",    "1.0", "v1 walnut: companion.md present at walnut root"),
    ("v1_walnut_now_md",      "now.md",          "1.0", "v1 walnut: now.md present at walnut root"),
    ("v1_walnut_observations", "observations.md", "1.0", "v1 walnut: observations.md present at walnut root"),
)

# v2 walnut markers (per-walnut, pre-v3 layout). Note that bare
# ``_core/`` is NOT a version-imputing v2 marker (a bare ``_core/``
# at a walnut root with NO ``_core/key.md`` and NO ``_core/_squirrels/``
# is a v1 layout). v2 is distinguished by the presence of
# ``_core/key.md`` OR ``_core/_squirrels/``; bare ``_core/`` defaults
# to v1 via the derived ``v1_walnut_core_bare`` probe in
# :func:`walnut_probes`.
_V2_WALNUT_MARKERS = (
    ("v2_walnut_core_key",   "_core/key.md",     "2.0", "v2 walnut layout: _core/key.md"),
    ("v2_walnut_capsules",   "_capsules",        "2.0", "v2 walnut layout: _capsules/"),
    ("v2_walnut_squirrels",  "_core/_squirrels", "2.0", "v2 walnut layout: _core/_squirrels/"),
)

# v3 walnut kernel marker. NOTE: this probe records presence (so the
# resolver can apply the v3.0-floor default for a walnut with no
# version-imputing probes) but does NOT itself impute a version --
# otherwise its 3.0 reading would always win the lowest-wins resolution
# against schema (3.1) / content (3.1) signals on the same walnut.
# The default-floor logic in version_detect.py reads ``fired`` here and
# applies the floor only when no other walnut probe imputed a version.
_V3_WALNUT_MARKERS: tuple = (
    ("v3_walnut_kernel_key", "_kernel/key.md", None, "v3 flat _kernel/key.md (presence-only)"),
)

# Pre-v3.1 walnut markers (presence imputes ≤ v3.0 floor).
_PRE_V31_WALNUT_MARKERS = (
    ("pre_v31_walnut_atoms",     "_kernel/atoms",      "3.0", "pre-v3.1 _kernel/atoms/ vestige"),
    ("pre_v31_walnut_generated", "_kernel/_generated", "3.0", "pre-v3.1 _kernel/_generated/ nested layout"),
)


def _check_path(snapshot: Any, abs_path: str) -> bool:
    """Return True iff *abs_path* exists in the snapshot.

    The snapshot's ``exists`` API treats both ``exists_only`` rules and
    content-mode rules as carrying existence; either is fine here since
    path-existence probes only need a yes/no.
    """
    return snapshot.exists(abs_path)


def world_probes(snapshot: Any, world_root: str) -> List[SignalProbe]:
    """Probe path-existence world signals from *snapshot*."""
    out: List[SignalProbe] = []
    world_root = os.path.abspath(world_root)
    for probe_id, rel, version, detail in _V1_WORLD_MARKERS:
        full = os.path.join(world_root, rel)
        fired = _check_path(snapshot, full)
        out.append(SignalProbe(
            probe_id=probe_id,
            source=SOURCE_PATH,
            scope=SCOPE_WORLD,
            fired=fired,
            inferred_version=version if fired else None,
            walnut_path=None,
            detail=("hit:" + rel) if fired else ("absent:" + rel),
        ))
    for probe_id, rel, version, detail in _PRE_V31_WORLD_MARKERS:
        full = os.path.join(world_root, rel)
        fired = _check_path(snapshot, full)
        out.append(SignalProbe(
            probe_id=probe_id,
            source=SOURCE_PATH,
            scope=SCOPE_WORLD,
            fired=fired,
            inferred_version=version if fired else None,
            walnut_path=None,
            detail=("hit:" + rel) if fired else ("absent:" + rel),
        ))
    for probe_id, rel, version, detail in _V32_WORLD_MARKERS:
        full = os.path.join(world_root, rel)
        fired = _check_path(snapshot, full)
        out.append(SignalProbe(
            probe_id=probe_id,
            source=SOURCE_PATH,
            scope=SCOPE_WORLD,
            fired=fired,
            inferred_version=version if fired else None,
            walnut_path=None,
            detail=("hit:" + rel) if fired else ("absent:" + rel),
        ))
    return out


def walnut_probes(snapshot: Any, walnut_path: str) -> List[SignalProbe]:
    """Probe path-existence walnut signals for one *walnut_path*."""
    out: List[SignalProbe] = []
    walnut_path = os.path.abspath(walnut_path)
    bn = os.path.basename(walnut_path) or walnut_path

    # Derived "bare _core/" probe -- fires when ``_core/`` exists at
    # the walnut root but neither ``_core/key.md`` nor
    # ``_core/_squirrels/`` is present (i.e. the walnut is a v1
    # layout, not v2). Without this derived probe, v1 worlds where
    # the world is a walnut and the only marker is a root-level
    # ``_core/`` resolve as v2.0 -- the spec calls these out as v1.
    core_dir = os.path.join(walnut_path, "_core")
    core_key = os.path.join(walnut_path, "_core", "key.md")
    core_squirrels = os.path.join(walnut_path, "_core", "_squirrels")
    bare_core_v1_fired = (
        snapshot.exists(core_dir)
        and not snapshot.exists(core_key)
        and not snapshot.exists(core_squirrels)
    )
    out.append(SignalProbe(
        probe_id="v1_walnut_core_bare@{}".format(bn),
        source=SOURCE_PATH,
        scope=SCOPE_WALNUT,
        fired=bare_core_v1_fired,
        inferred_version="1.0" if bare_core_v1_fired else None,
        walnut_path=walnut_path,
        detail=(
            "hit:bare _core/ (no _core/key.md, no _core/_squirrels/)"
            if bare_core_v1_fired else
            "absent:_core/ OR has v2 markers"
        ),
    ))

    for probe_id, rel, version, detail in _V1_WALNUT_MARKERS:
        full = os.path.join(walnut_path, rel)
        fired = _check_path(snapshot, full)
        out.append(SignalProbe(
            probe_id="{}@{}".format(probe_id, bn),
            source=SOURCE_PATH,
            scope=SCOPE_WALNUT,
            fired=fired,
            inferred_version=version if fired else None,
            walnut_path=walnut_path,
            detail=("hit:" + rel) if fired else ("absent:" + rel),
        ))
    for probe_id, rel, version, detail in _V2_WALNUT_MARKERS:
        full = os.path.join(walnut_path, rel)
        fired = _check_path(snapshot, full)
        out.append(SignalProbe(
            probe_id="{}@{}".format(probe_id, bn),
            source=SOURCE_PATH,
            scope=SCOPE_WALNUT,
            fired=fired,
            inferred_version=version if fired else None,
            walnut_path=walnut_path,
            detail=("hit:" + rel) if fired else ("absent:" + rel),
        ))
    for probe_id, rel, version, detail in _V3_WALNUT_MARKERS:
        full = os.path.join(walnut_path, rel)
        fired = _check_path(snapshot, full)
        out.append(SignalProbe(
            probe_id="{}@{}".format(probe_id, bn),
            source=SOURCE_PATH,
            scope=SCOPE_WALNUT,
            fired=fired,
            inferred_version=version if fired else None,
            walnut_path=walnut_path,
            detail=("hit:" + rel) if fired else ("absent:" + rel),
        ))
    for probe_id, rel, version, detail in _PRE_V31_WALNUT_MARKERS:
        full = os.path.join(walnut_path, rel)
        fired = _check_path(snapshot, full)
        out.append(SignalProbe(
            probe_id="{}@{}".format(probe_id, bn),
            source=SOURCE_PATH,
            scope=SCOPE_WALNUT,
            fired=fired,
            inferred_version=version if fired else None,
            walnut_path=walnut_path,
            detail=("hit:" + rel) if fired else ("absent:" + rel),
        ))
    return out


def snapshot_rule_contributions() -> List[Any]:
    """Return :class:`SnapshotRule` records this source needs in the snapshot.

    Imported lazily inside the function so the orchestrator can pass
    its own SnapshotRule type without a top-level cycle. The returned
    list must be added to the combined-allowlist passed to
    ``FileSnapshot.populate``; T1 owns the merge.
    """
    from ..file_snapshot import SnapshotRule  # noqa: PLC0415

    rules: List[Any] = []
    # World-scope path probes -- exists_only is sufficient (no bytes).
    for _id, rel, _v, _d in _V1_WORLD_MARKERS + _PRE_V31_WORLD_MARKERS + _V32_WORLD_MARKERS:
        rules.append(SnapshotRule(
            glob="<world>/" + rel,
            exists_only=True,
        ))
    # Forensic-capture rules -- markers that are NO LONGER version-
    # imputing but still matter for debugging / migration awareness.
    # ``.alive/overrides.md`` was removed from ``_PRE_V31_WORLD_MARKERS``
    # because v3 also uses it as the canonical user-customisation buffer
    # (codex completion-review fix); we still capture it so forensic
    # inspectors and the allowlist-contribution test see the file.
    for rel in (".alive/overrides.md",):  # DERIVED: forensic-only allowlist entry; not a verifier callsite, never read for version inference
        rules.append(SnapshotRule(
            glob="<world>/" + rel,
            exists_only=True,
        ))
        # Trailing slash variant for directories handled by glob match
        # against the directory itself; ``glob.glob`` matches a directory
        # as a path equal to the dir, so the rule above suffices.

    # Walnut-scope path probes -- glob across every walnut under the
    # world. The orchestrator has the discovered walnut list at rule-
    # build time, but to keep this function pure and free of walnut
    # discovery, we use the recursive ``**`` template -- the snapshot
    # accepts arbitrary glob depth and the path-probe lookup is
    # lexical, so any walnut directory whose marker exists IS captured.
    for _id, rel, _v, _d in (
        _V1_WALNUT_MARKERS
        + _V2_WALNUT_MARKERS
        + _V3_WALNUT_MARKERS
        + _PRE_V31_WALNUT_MARKERS
    ):
        rules.append(SnapshotRule(
            glob="<world>/**/" + rel,
            exists_only=True,
        ))
    # Walnut-derivation markers (consumed by the snapshot-derived
    # walnut discovery in :func:`version_detect._derive_walnuts_from_snapshot`).
    # These are file paths whose presence imputes the parent dir as a
    # walnut; they are NOT version-imputing on their own (they overlap
    # with walnut markers above). Without these, the snapshot-derived
    # walk can miss v2 walnuts whose only marker is ``_core/key.md``.
    for rel in ("_kernel/key.md", "_core/key.md"):
        rules.append(SnapshotRule(
            glob="<world>/**/" + rel,
            exists_only=True,
        ))
    return rules
