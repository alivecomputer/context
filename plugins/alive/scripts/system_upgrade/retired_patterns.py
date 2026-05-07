"""Source of truth for world-state cleanup -- the explicit retired-pattern
catalog enumerating every retirement by source version.

This module is **R5 audit-allowlisted**: it is hardcoded by design.
World-state cleanup decisions (T5), v2->v3 migration inputs (T9), and
walkthrough rewrites (T8) all consume entries from ``CATALOG`` instead
of inferring cleanup targets from live plugin manifests. Live-read
verification (``verify.py``) covers a different concern --
plugin-surface drift in user extensions -- and cannot close
#62-class bugs because retired paths (e.g. ``.alive/scripts/``) are by
definition absent from current ``hooks.json`` / ``plugin.json``.

Schema (per epic spec § Approach):

* ``source_commit``         -- short SHA where the pattern was retired
* ``source_version``        -- target version of the release that retired it
* ``target_path_glob``      -- world-relative glob (or literal) the catalog
                                  matches against
* ``pattern_type``          -- ``"directory" | "file" | "frontmatter_field"
                                  | "string_match"``
* ``pattern_signature``     -- regex / glob / literal -- matched against
                                  snapshot bytes (or path existence for
                                  ``directory``/``file`` types)
* ``redesign_step_id``      -- which redesign task / phase consumes this
                                  entry (``T1``, ``T4``, ``T5``, ...)
* ``surface_message``       -- short human-readable diff context for
                                  walkthrough rendering and cleanup
                                  briefings
* ``walkthrough_eligible``  -- whether T8/T9 walkthrough should flag
                                  user-content matches
* ``surface_overlap_risk``  -- ``"plugin_owned" | "world_state"
                                  | "potentially_surface"`` -- T5
                                  conservative-refusal allowlist driver
                                  under ``--surfaces=none``
* ``cleanup_action``        -- which phase consumes the entry:
                                  ``"cleanup"``    -> phase 8 deletes
                                  ``"migrate_input"`` -> phase 9 reads,
                                                        transforms, then
                                                        removes
                                  ``"walkthrough_rewrite"`` -> phase 7
                                                        surfaces; phase 9
                                                        applies via T8.apply
                                  ``"verify_only"`` -> never triggers a
                                                        write
* ``rewrite_kind``          -- ``None | "regex_substitute" | "static_replace"
                                  | "delete_only"``; only meaningful when
                                  ``walkthrough_eligible`` is True OR
                                  ``cleanup_action == "walkthrough_rewrite"``
* ``replacement_template``  -- backref-aware replacement string for
                                  ``regex_substitute``; literal new content
                                  for ``static_replace``; ``None`` for
                                  ``delete_only``
* ``rewrite_fn_id``         -- registry key for non-template rewrites
                                  (escape hatch); resolved at apply time
* ``expected_filenames``    -- ``set[str] | None``. When ``pattern_type
                                  == "directory"`` and the directory MAY
                                  contain user-authored siblings, this
                                  set lists the historic plugin filenames
                                  (so T5 enumerates non-plugin contents
                                  by name). ``None`` means either "all
                                  contents are plugin-owned" or
                                  ``pattern_type`` is non-directory.

Rewrite-payload invariant (R13 / M9):

Any entry with ``walkthrough_eligible: True`` MUST have ``rewrite_kind``
non-None AND exactly one of ``(replacement_template`` set,
``rewrite_fn_id`` set, OR ``rewrite_kind == "delete_only"``). T8.apply
uses this to deterministically generate ``rewrite_bytes`` for each
accepted walkthrough decision; T8 does NOT invent rewrite logic --
every rewrite originates here.

Action-ownership invariant:

* ``cleanup_action == "cleanup"`` are the ONLY entries T5 deletes.
* Active migration inputs (``_core/``, ``_capsules/``, ``now.md``,
  ``tasks.md``, ``observations.md``, ``_kernel/_generated/``,
  ``03_Inputs/``, ``companion.md``) MUST carry
  ``cleanup_action == "migrate_input"``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ._phase_helpers import compute_byte_offsets


__all__ = (
    "RetiredPattern",
    "CATALOG",
    "CatalogMatch",
    "match_walkthrough_eligible",
    "match_directory_for_cleanup",
    "validate_catalog_entry",
    "REWRITE_FN_REGISTRY",
    "ALL_PATTERN_TYPES",
    "ALL_CLEANUP_ACTIONS",
    "ALL_SURFACE_OVERLAP_RISKS",
    "ALL_REWRITE_KINDS",
)


ALL_PATTERN_TYPES: Tuple[str, ...] = (
    "directory",
    "file",
    "frontmatter_field",
    "string_match",
)
ALL_CLEANUP_ACTIONS: Tuple[str, ...] = (
    "cleanup",
    "migrate_input",
    "walkthrough_rewrite",
    "verify_only",
)
ALL_SURFACE_OVERLAP_RISKS: Tuple[str, ...] = (
    "plugin_owned",
    "world_state",
    "potentially_surface",
)
ALL_REWRITE_KINDS: Tuple[Optional[str], ...] = (
    None,
    "regex_substitute",
    "static_replace",
    "delete_only",
)


@dataclass(frozen=True)
class RetiredPattern:
    """One catalog entry. See module docstring for field semantics.

    Validation is intentionally schema-only here; combinatorial
    invariants are enforced by the module-level
    ``validate_catalog_entry`` helper, which is invoked once per entry
    at module-import (see the ``for entry in CATALOG`` loop at the
    bottom of this file) and is also test-callable so the catalog's
    M9 invariant has direct unit coverage.
    """

    source_commit: str
    source_version: str
    target_path_glob: str
    pattern_type: str
    pattern_signature: str
    redesign_step_id: str
    surface_message: str
    walkthrough_eligible: bool
    surface_overlap_risk: str
    cleanup_action: str
    rewrite_kind: Optional[str]
    replacement_template: Optional[str]
    rewrite_fn_id: Optional[str]
    expected_filenames: Optional[frozenset]


def validate_catalog_entry(entry: "RetiredPattern") -> None:
    """Validate a single ``RetiredPattern`` against the catalog invariants.

    Raises ``ValueError`` on the first violation; callers chain the
    target-path glob into the message so import-time failures are
    self-locating.

    Validation is the same combinatorial contract that previously
    lived in ``RetiredPattern.__post_init__``:

    * ``pattern_type`` / ``cleanup_action`` / ``surface_overlap_risk``
      / ``rewrite_kind`` each belong to their respective whitelist.
    * ``expected_filenames`` is only meaningful for ``"directory"``
      pattern types.
    * Walkthrough eligibility implies a populated rewrite payload
      (M9 invariant): exactly one of ``replacement_template`` /
      ``rewrite_fn_id`` for non-``delete_only`` rewrites; zero for
      ``delete_only``.

    Called from the ``for entry in CATALOG`` loop at module-import so
    typos in catalog data fail loud at startup, AND from the dedicated
    test in ``tests/test_retired_patterns.py`` so the invariant has
    direct unit coverage.
    """
    if entry.pattern_type not in ALL_PATTERN_TYPES:
        raise ValueError(
            "pattern_type {!r} not in {}".format(
                entry.pattern_type, ALL_PATTERN_TYPES
            )
        )
    if entry.cleanup_action not in ALL_CLEANUP_ACTIONS:
        raise ValueError(
            "cleanup_action {!r} not in {}".format(
                entry.cleanup_action, ALL_CLEANUP_ACTIONS
            )
        )
    if entry.surface_overlap_risk not in ALL_SURFACE_OVERLAP_RISKS:
        raise ValueError(
            "surface_overlap_risk {!r} not in {}".format(
                entry.surface_overlap_risk, ALL_SURFACE_OVERLAP_RISKS
            )
        )
    if entry.rewrite_kind not in ALL_REWRITE_KINDS:
        raise ValueError(
            "rewrite_kind {!r} not in {}".format(
                entry.rewrite_kind, ALL_REWRITE_KINDS
            )
        )
    # expected_filenames is meaningful only for directory patterns.
    if (
        entry.pattern_type != "directory"
        and entry.expected_filenames is not None
    ):
        raise ValueError(
            "expected_filenames must be None for non-directory pattern "
            "(pattern_type={}, target={})".format(
                entry.pattern_type, entry.target_path_glob
            )
        )
    # Walkthrough eligibility implies a populated rewrite payload
    # (M9 invariant). The catalog test enumerates this too, but the
    # module-import loop catches typos at import time.
    if entry.walkthrough_eligible:
        if entry.rewrite_kind is None:
            raise ValueError(
                "walkthrough_eligible entries must declare a "
                "rewrite_kind (target={})".format(entry.target_path_glob)
            )
        payload_count = sum(
            1
            for v in (
                entry.replacement_template,
                entry.rewrite_fn_id,
            )
            if v is not None
        )
        if entry.rewrite_kind == "delete_only":
            if payload_count != 0:
                raise ValueError(
                    "delete_only entries must not carry a "
                    "replacement_template or rewrite_fn_id "
                    "(target={})".format(entry.target_path_glob)
                )
        else:
            if payload_count != 1:
                raise ValueError(
                    "non-delete_only walkthrough entries must declare "
                    "exactly one of replacement_template / "
                    "rewrite_fn_id (target={}, kind={})".format(
                        entry.target_path_glob, entry.rewrite_kind
                    )
                )


# ---------------------------------------------------------------------------
# Rewrite registry -- escape hatch for non-template rewrites
# ---------------------------------------------------------------------------

#: Maps ``rewrite_fn_id`` to a callable ``(matched_bytes: bytes,
#: full_content: bytes) -> bytes`` returning the new full content. Empty
#: at T4: no current entry needs callable rewrites; the registry exists
#: so future entries can opt out of regex/static templates without
#: relaxing the M9 invariant.
REWRITE_FN_REGISTRY: Dict[str, Callable[[bytes, bytes], bytes]] = {}


# ---------------------------------------------------------------------------
# Catalog data
# ---------------------------------------------------------------------------

def _fs(*names: str) -> frozenset:
    return frozenset(names)


# Catalog populated from:
# * 04_Ventures/alive/upgrade-discipline/audit-public-history.md
#   (per-version retirement events with commit SHAs)
# * 04_Ventures/alive/upgrade-discipline/drift-inventory.md
#   (gap matrix: what shipped vs what skill handles)
# * 04_Ventures/alive/upgrade-discipline/audit-current-skill.md
#   (verification check #1 lineage)
#
# Action-ownership rule:
#  - Active migration inputs (_core/, _capsules/, companion.md, now.md,
#    tasks.md, observations.md, _kernel/_generated/, 03_Inputs/) MUST
#    carry cleanup_action == "migrate_input" so phase 8 leaves them
#    intact for phase 9.
#  - Only inert / deprecated paths (.alive/scripts/, .alive/atoms/, etc.)
#    carry cleanup_action == "cleanup".
CATALOG: List[RetiredPattern] = [
    # -----------------------------------------------------------------
    # v3.0 retirements -- v2 layout consumed by the v2->v3.0 migration
    # phase (T9). These MUST carry cleanup_action == "migrate_input"
    # so phase 8 (cleanup) leaves them intact for phase 9 to consume.
    # -----------------------------------------------------------------
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="_core/",
        pattern_type="directory",
        pattern_signature=r"_core/?$",
        redesign_step_id="T9",
        surface_message=(
            "v1/v2 kernel directory '_core/'. Phase 9 reads its content "
            "into the new flat '_kernel/' before removing it."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="migrate_input",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,  # T9 consumes wholesale; not enumerated
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="_capsules/",
        pattern_type="directory",
        pattern_signature=r"_capsules/?$",
        redesign_step_id="T9",
        surface_message=(
            "v1 'capsule' bundles directory. Phase 9 promotes each "
            "capsule into a flat top-level bundle under the walnut root "
            "before removing the container."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="migrate_input",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="**/companion.md",
        pattern_type="file",
        pattern_signature=r"companion\.md$",
        redesign_step_id="T9",
        surface_message=(
            "Per-bundle hand-written companion.md. Phase 9 converts to "
            "context.manifest.yaml, then removes."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="migrate_input",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="**/_kernel/now.md",
        pattern_type="file",
        pattern_signature=r"now\.md$",
        redesign_step_id="T9",
        surface_message=(
            "Hand-written now.md. v3 generates now.json post-save; "
            "phase 9 deletes the now.md after the projection script "
            "writes the canonical now.json."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="migrate_input",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="**/_kernel/tasks.md",
        pattern_type="file",
        pattern_signature=r"tasks\.md$",
        redesign_step_id="T9",
        surface_message=(
            "Markdown tasks. Phase 9 converts checkbox lines to "
            "tasks.json + completed.json, then removes (a .bak copy is "
            "kept by T5's backup tarball)."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="migrate_input",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="**/_kernel/observations.md",
        pattern_type="file",
        pattern_signature=r"observations\.md$",
        redesign_step_id="T9",
        surface_message=(
            "Standalone observations.md. v3 routes observations into "
            "the manifest context block at save time. Phase 9 migrates "
            "any prior content, then removes the file."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="migrate_input",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="**/_kernel/_generated/",
        pattern_type="directory",
        pattern_signature=r"_kernel/_generated/?$",
        redesign_step_id="T9",
        surface_message=(
            "Nested generated subdirectory inside per-walnut _kernel/. "
            "Phase 9 promotes its now.json (and any sibling generated "
            "files) into the flat _kernel/ before removing the dir."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="migrate_input",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="03_Inputs/",
        pattern_type="directory",
        pattern_signature=r"^03_Inputs/?$",
        redesign_step_id="T9",
        surface_message=(
            "Pre-v3 inbox folder name. Phase 9 renames to 03_Inbox/ "
            "(content preserved, parent path rewritten)."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="migrate_input",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    # -----------------------------------------------------------------
    # v3.1 retirements
    # -----------------------------------------------------------------
    # The #62 canary -- copy-to-world scripts pattern.
    RetiredPattern(
        source_commit="f565c81",
        source_version="3.1",
        target_path_glob=".alive/scripts/",
        pattern_type="directory",
        pattern_signature=r"^\.alive/scripts/?$",
        redesign_step_id="T5",
        surface_message=(
            "World-local '.alive/scripts/' directory. The copy-to-world "
            "script pattern was retired in v3.1 (commit f565c81): "
            "scripts now run from $ALIVE_PLUGIN_ROOT/scripts/. Stale "
            "copies are inert but mislead future debugging. Phase 8 "
            "removes the directory; T5 enumerates any non-plugin "
            "filenames inside as user content in the cleanup briefing."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        # Historic plugin filenames present at retirement (sourced from
        # the v3.0/v3.1 plugin scripts/ inventory). Anything inside
        # .alive/scripts/ that is NOT in this set is named explicitly in
        # T5's cleanup briefing as user-authored content.
        expected_filenames=_fs(
            "alive-p2p.py",
            "alive-context-watch.sh",
            "tasks.py",
            "project.py",
            "generate-graph.py",
            "generate-index.py",
            "_common.py",
            "_alive_common",
            "validate.py",
        ),
    ),
    RetiredPattern(
        source_commit="f565c81",
        source_version="3.1",
        target_path_glob=".alive/atoms/",
        pattern_type="directory",
        pattern_signature=r"^\.alive/atoms/?$",
        redesign_step_id="T5",
        surface_message=(
            "Pre-v3 '.alive/atoms/' directory (atom-cache vestige). "
            "Phase 8 removes."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,  # entire dir is plugin-owned
    ),
    RetiredPattern(
        source_commit="f565c81",
        source_version="3.1",
        target_path_glob=".alive/computed/",
        pattern_type="directory",
        pattern_signature=r"^\.alive/computed/?$",
        redesign_step_id="T5",
        surface_message=(
            "Pre-v3 '.alive/computed/' directory (projection-cache "
            "vestige; v3 routes projections to '_kernel/now.json'). "
            "Phase 8 removes."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="f565c81",
        source_version="3.1",
        target_path_glob=".alive/locks/",
        pattern_type="directory",
        pattern_signature=r"^\.alive/locks/?$",
        redesign_step_id="T5",
        surface_message=(
            "Legacy '.alive/locks/' coordination directory. v3.1+ uses "
            "in-place flock files at canonical paths. Phase 8 removes."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="f565c81",
        source_version="3.1",
        target_path_glob=".alive/overrides.md",
        pattern_type="file",
        pattern_signature=r"^\.alive/overrides\.md$",
        redesign_step_id="T5",
        surface_message=(
            "Pre-v3 '.alive/overrides.md' (rule-customization buffer). "
            "v3 routes rule overrides into '.alive/overrides.md' under "
            "the new schema; the legacy file is preserved by the backup "
            "tarball before removal."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="potentially_surface",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="f565c81",
        source_version="3.1",
        target_path_glob=".alive/upgrade-plan.html",
        pattern_type="file",
        pattern_signature=r"^\.alive/upgrade-plan\.html$",
        redesign_step_id="T5",
        surface_message=(
            "Stale '.alive/upgrade-plan.html' from a previous "
            "upgrade-rendering experiment. Phase 8 removes."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    # World-level '.alive/_generated/' upgrade-loop vestige (codex
    # completion-review fix R6). Distinct from the per-walnut
    # '**/_kernel/_generated/' entry above: the world-level
    # '.alive/_generated/' carried legacy upgrade scripts (upgrade.py,
    # upgrade-v3.py, upgrade-delta.py) and pre-upgrade tarball
    # placeholders that pre-v3.2 upgrade loops emitted. Phase 8
    # removes the entire directory; the pre-upgrade tarball backup
    # preserves contents for forensics.
    RetiredPattern(
        source_commit="f565c81",
        source_version="3.1",
        target_path_glob=".alive/_generated/",
        pattern_type="directory",
        pattern_signature=r"^\.alive/_generated/?$",
        redesign_step_id="T5",
        surface_message=(
            "World-level '.alive/_generated/' directory (legacy "
            "upgrade-loop vestige). Pre-v3.2 upgrade scripts emitted "
            "scripts and tarball placeholders here. Phase 8 removes "
            "the entire directory; the pre-upgrade backup tarball "
            "preserves contents for forensics."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    # Pre-v3.1 backup of the relay manifest (codex completion-review
    # fix R6). The '.alive/relay.yaml.bak' file is left behind by
    # legacy relay-config edits; the canonical relay manifest lives at
    # '.alive/relay.yaml'. Phase 8 removes the .bak sibling.
    RetiredPattern(
        source_commit="f565c81",
        source_version="3.1",
        target_path_glob=".alive/relay.yaml.bak",
        pattern_type="file",
        pattern_signature=r"^\.alive/relay\.yaml\.bak$",
        redesign_step_id="T5",
        surface_message=(
            "Pre-v3.1 '.alive/relay.yaml.bak' backup. The canonical "
            "manifest at '.alive/relay.yaml' supersedes; the .bak "
            "sibling is preserved by the pre-upgrade tarball. Phase 8 "
            "removes."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    # ALIVE_PLUGIN_ROOT find/replace -- the only walkthrough-eligible
    # entry in the catalog at T4. Targets user content (custom skills/
    # rules/hooks) referencing the pre-v3.1 hardcoded plugin path.
    RetiredPattern(
        source_commit="f565c81",
        source_version="3.1",
        target_path_glob="<world>/.alive/skills/**/*.md|<world>/.alive/rules/**/*.md|<world>/.alive/hooks/**/*.sh",
        pattern_type="string_match",
        pattern_signature=r"\bplugins/alive/scripts/(\S+)",
        redesign_step_id="T8",
        surface_message=(
            "Hardcoded 'plugins/alive/scripts/...' reference in user "
            "content. v3.1 introduced ALIVE_PLUGIN_ROOT (commit "
            "f565c81); rewrite to '${ALIVE_PLUGIN_ROOT}/scripts/...' so "
            "the path resolves correctly under any plugin-cache layout."
        ),
        walkthrough_eligible=True,
        surface_overlap_risk="potentially_surface",
        cleanup_action="walkthrough_rewrite",
        rewrite_kind="regex_substitute",
        replacement_template=r"${ALIVE_PLUGIN_ROOT}/scripts/\1",
        rewrite_fn_id=None,
        expected_filenames=None,  # string_match -- not directory
    ),
    # World-root strays that belong inside walnut _kernel/. These are
    # v1/v2 layout holdovers (per epic spec § Backward cleanup). Phase 8
    # removes them after the pre-upgrade tarball captures contents for
    # forensics. Per-walnut content was already promoted into
    # ``_kernel/`` by v3.0 -> v3.1 migration; the world-root copies are
    # stale duplicates.
    #
    # Codex completion-review fix: previously these carried
    # ``cleanup_action="verify_only"`` on the assumption that T9 would
    # move them per-walnut. But T9's per-walnut migration only touches
    # paths beneath each walnut; world-root strays sat outside its
    # purview, leaving them on disk indefinitely. The canary contract
    # demands their absence post-upgrade -- switching to "cleanup" is
    # the locked posture.
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="key.md",
        pattern_type="file",
        pattern_signature=r"^key\.md$",
        redesign_step_id="T5",
        surface_message=(
            "World-root key.md is a v1/v2 layout vestige. v3 places "
            "kernel files under <walnut>/_kernel/. Phase 8 removes; "
            "the pre-upgrade tarball preserves contents for forensics."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="log.md",
        pattern_type="file",
        pattern_signature=r"^log\.md$",
        redesign_step_id="T5",
        surface_message=(
            "World-root log.md belongs inside a walnut's _kernel/. "
            "Phase 8 removes; the pre-upgrade tarball preserves "
            "contents for forensics."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="insights.md",
        pattern_type="file",
        pattern_signature=r"^insights\.md$",
        redesign_step_id="T5",
        surface_message=(
            "World-root insights.md belongs inside a walnut's "
            "_kernel/. Phase 8 removes; the pre-upgrade tarball "
            "preserves contents for forensics."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    # -----------------------------------------------------------------
    # v3.2 retirements -- demo skill internal cleanup contract
    # (per fn-17 marker contract).
    # -----------------------------------------------------------------
    RetiredPattern(
        source_commit="6a9f629",
        source_version="3.2",
        target_path_glob="_stage_outputs/entities/",
        pattern_type="directory",
        pattern_signature=r"^_stage_outputs/entities/?$",
        redesign_step_id="T5",
        surface_message=(
            "Demo-skill stage-outputs entities directory. The v3.2 "
            "/alive:demo install step prunes this post-install (fn-17 "
            "marker contract). Stragglers from earlier demo runs are "
            "removed by phase 8."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    # -----------------------------------------------------------------
    # Catalog-gap entries (closes #84). These five patterns existed in
    # real-world worlds but were missing from the catalog, so phase 8
    # left them in place across upgrades. Sources:
    # * P2P revert -- staging-only PR #69 reverted the P2P scripts
    #   portion of #64, but worlds that ran the pre-revert plugin had
    #   .alive/relay/ written and the revert removed code, not state.
    # * v1 layout vestiges (_brain/, _state/, _kernel/_chapters/) --
    #   referenced in legacy detection prose, never in the Python
    #   catalog; the v2->v3.0 migration's bundle-shape work covered
    #   _core/ and _capsules/ but did not retire the kernel-precursor
    #   directories.
    # * .v2backup -- a distinctive manual-migration leftover suffix
    #   used during early v1->v2 hand-rolled upgrades.
    RetiredPattern(
        source_commit="7cae463",
        source_version="3.1",
        target_path_glob=".alive/relay/",
        pattern_type="directory",
        pattern_signature=r"^\.alive/relay/?$",
        redesign_step_id="T5",
        surface_message=(
            "P2P relay artifacts directory from the pre-revert plugin "
            "(staging-only PR #64; reverted by #69). Contains relay/"
            "state JSON manifests and the keypair under keys/. The "
            "revert removed the code path; the directory remains on "
            "worlds that ran the pre-revert plugin. Phase 8 removes; "
            "the pre-upgrade tarball preserves contents for forensics."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="_brain/",
        pattern_type="directory",
        pattern_signature=r"^_brain/?$",
        redesign_step_id="T5",
        surface_message=(
            "Pre-_core/ v1 kernel-precursor directory. Predates the v1 "
            "_core/ layout that the v2->v3.0 migration consumes. "
            "Content was promoted into per-walnut _kernel/ in earlier "
            "manual migrations; the directory is a layout vestige. "
            "Phase 8 removes."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="_state/",
        pattern_type="directory",
        pattern_signature=r"^_state/?$",
        redesign_step_id="T5",
        surface_message=(
            "Pre-_core/ v1 state directory. Companion to _brain/ in "
            "the earliest layout; runtime state moved to _kernel/ "
            "and later to projection artifacts. Phase 8 removes."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="**/_kernel/_chapters/",
        pattern_type="directory",
        pattern_signature=r"_kernel/_chapters/?$",
        redesign_step_id="T5",
        surface_message=(
            "Legacy per-walnut log-chapters directory. Predates the "
            "_kernel/history/ convention; chapter content was already "
            "duplicated into log.md by the time _kernel/history/ "
            "shipped. Phase 8 removes; the pre-upgrade tarball "
            "preserves any unique content for forensics."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
    RetiredPattern(
        source_commit="21ac613",
        source_version="3.0",
        target_path_glob="**/_kernel/*.v2backup",
        pattern_type="file",
        pattern_signature=r"_kernel/[^/]+\.v2backup$",
        redesign_step_id="T5",
        surface_message=(
            "Per-walnut .v2backup file from a hand-rolled v1->v2 "
            "migration. The .v2backup suffix was a one-time rename "
            "convention; the canonical content lives at the original "
            "filename (without the suffix) post-migration. Phase 8 "
            "removes; the pre-upgrade tarball preserves contents."
        ),
        walkthrough_eligible=False,
        surface_overlap_risk="world_state",
        cleanup_action="cleanup",
        rewrite_kind=None,
        replacement_template=None,
        rewrite_fn_id=None,
        expected_filenames=None,
    ),
]


# Module-import-time validation: catches catalog-data typos at startup
# rather than letting an invalid entry propagate into a phase that
# only trips on the specific ``rewrite_kind`` / ``cleanup_action`` it
# uses. Replaces the previous ``RetiredPattern.__post_init__`` body
# (extracted to ``validate_catalog_entry`` so tests can call it
# directly).
for _entry in CATALOG:
    validate_catalog_entry(_entry)
del _entry


# ---------------------------------------------------------------------------
# Pre-scan API consumed by T3's version_detect (phase 3) and T8's
# walkthrough decide (phase 7).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogMatch:
    """One ``(path, pattern_id)`` walkthrough candidate from the pre-scan.

    ``pattern_id`` is the catalog index; consumers can resolve the full
    ``RetiredPattern`` via ``CATALOG[match.pattern_id]``.

    ``span`` records the byte offsets of the match within the captured
    snapshot bytes; T8.apply uses this to render the diff and to anchor
    regex replacements without re-running the pattern compile.
    """

    path: str
    pattern_id: int
    span_start: int
    span_end: int
    matched_bytes: bytes


def _iter_walkthrough_eligible_indices() -> List[int]:
    """Return catalog indices whose entries are walkthrough-eligible."""
    return [i for i, p in enumerate(CATALOG) if p.walkthrough_eligible]


def _glob_to_fnmatch(g: str) -> str:
    """Normalize ``**``/``**/`` to ``*`` for ``fnmatch`` (which has no
    double-star semantics; a single ``*`` matches across path separators
    in ``fnmatch.fnmatch``, so collapsing ``**/<x>`` and ``**`` both to
    ``*`` produces the recursive-glob behaviour callers expect)."""
    # ``**/`` should match zero or more path segments (including none),
    # so collapse it to ``*`` rather than ``*/`` which would force an
    # intermediate directory.
    while "**/" in g:
        g = g.replace("**/", "*")
    while "**" in g:
        g = g.replace("**", "*")
    return g


def _path_in_target_scope(
    path: str, target_path_glob: str, world_root: Optional[str] = None
) -> bool:
    """Check whether *path* falls within the catalog entry's declared
    target scope.

    The catalog uses a small grammar for ``target_path_glob`` on
    walkthrough-eligible entries: a ``|``-separated list of glob
    patterns, each starting with the ``<world>`` template.

    Behaviour:

    * When *world_root* is supplied, the matcher expands the template
      to the absolute path under that root and accepts ONLY paths that
      satisfy the expanded glob. A path under a different root that
      happens to share the same tail is rejected (``/other/.alive/...``
      no longer false-positives when *world_root* is ``/world``).
    * When *world_root* is ``None``, the matcher falls back to a
      tail-suffix test (the templated tail, with or without a leading
      ``*/`` so absolute paths satisfy the glob). This is the only
      path-classification mode that does not require world-root context.

    Non-walkthrough entries are NOT routed through this helper -- their
    ``target_path_glob`` is a directory or filename literal consumed by
    different phases (T5 cleanup, T9 migration).
    """
    alternatives = target_path_glob.split("|")
    for alt in alternatives:
        alt = alt.strip()
        if not alt:
            continue
        if alt.startswith("<world>/"):
            tail = _glob_to_fnmatch(alt[len("<world>/"):])
            if world_root is not None:
                # Strict: absolute glob, no suffix fallback. Out-of-root
                # paths sharing the same tail are rejected.
                full = world_root.rstrip("/") + "/" + tail
                if fnmatch.fnmatch(path, full):
                    return True
                continue
            # No world_root supplied -- tail-suffix fallback. Test
            # both the bare tail and a leading-slash variant so
            # absolute paths match templated tails like
            # ``.alive/skills/*.md``.
            if fnmatch.fnmatch(path, tail) or fnmatch.fnmatch(
                path, "*/" + tail
            ):
                return True
        else:
            # Non-templated literal/glob -- match directly.
            if fnmatch.fnmatch(path, _glob_to_fnmatch(alt)):
                return True
    return False


def match_walkthrough_eligible(
    snapshot: Any,
    retired_patterns: Optional[List[RetiredPattern]] = None,
    *,
    world_root: Optional[str] = None,
) -> List[CatalogMatch]:
    """Return every ``CatalogMatch`` the snapshot satisfies (pure / read-only).

    Signature follows the published contract
    (``match_walkthrough_eligible(snapshot, retired_patterns)``).
    *retired_patterns* defaults to the module-level :data:`CATALOG`
    when omitted; T3 / T8 callers may pass an explicit catalog snapshot
    if they need to pin a particular shape.

    Walks every captured path in ``snapshot.paths()``. For each
    walkthrough-eligible catalog entry, the path must fall within the
    entry's declared ``target_path_glob`` scope (the catalog explicitly
    targets user-extension trees only). Paths in scope are read,
    decoded, and tested against the entry's regex -- each hit becomes
    one ``CatalogMatch``. Plugin-owned snapshot files containing the
    same legacy string never produce false positives because they are
    out of scope.

    *world_root* is optional but recommended; when supplied, the scope
    test compares absolute paths against the expanded glob. Without it,
    the test falls back to a tail-suffix match (which is correct for
    paths whose suffix matches the catalog's templated tail).

    Pure: no caching, no globals mutated, no disk reads. Two calls on
    the same snapshot (and same *world_root*) return identical lists.

    ``CatalogMatch.pattern_id`` indexes into the catalog passed in (or
    :data:`CATALOG` when omitted), so consumers that pass a custom
    catalog must resolve via the same list.
    """
    catalog = retired_patterns if retired_patterns is not None else CATALOG
    indices = [i for i, p in enumerate(catalog) if p.walkthrough_eligible]
    if not indices:
        return []
    # Compile once per call; cheap because the catalog is small.
    compiled = [
        (i, re.compile(catalog[i].pattern_signature)) for i in indices
    ]
    out: List[CatalogMatch] = []
    for path in snapshot.paths():
        try:
            data = snapshot.read(path)
        except (KeyError, ValueError):
            # exists_only entries / missing entries: skip silently.
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # Non-text files cannot match string-match patterns.
            continue
        # Char-offset to byte-offset translation lives in
        # ``_phase_helpers.compute_byte_offsets`` -- the same helper
        # backs ``verify.scan_user_extensions`` so non-ASCII spans
        # stay consistent across both consumers.
        for pat_id, regex in compiled:
            entry = catalog[pat_id]
            if not _path_in_target_scope(
                path, entry.target_path_glob, world_root=world_root
            ):
                continue
            for m in regex.finditer(text):
                start_chars, end_chars = m.span()
                start_bytes, end_bytes = compute_byte_offsets(
                    text, start_chars, end_chars,
                )
                out.append(
                    CatalogMatch(
                        path=path,
                        pattern_id=pat_id,
                        span_start=start_bytes,
                        span_end=end_bytes,
                        matched_bytes=text[start_chars:end_chars].encode(
                            "utf-8"
                        ),
                    )
                )
    return out


def match_directory_for_cleanup(world_root: str) -> List[Tuple[int, str]]:
    """Live-disk pre-scan for ``cleanup_action == "cleanup"`` directory entries.

    Returns a list of ``(pattern_id, absolute_path)`` for every catalog
    directory whose target exists at *world_root*. Pure read-only;
    consumed by T5 (phase 8 cleanup) to know what to delete.

    Kept here -- not in T5 -- because the catalog is the source of
    truth for cleanup targets and centralising the matcher makes T5's
    audit grep narrower.
    """
    import os

    results: List[Tuple[int, str]] = []
    for idx, pat in enumerate(CATALOG):
        if pat.cleanup_action != "cleanup":
            continue
        if pat.pattern_type != "directory":
            continue
        # target_path_glob is world-relative for our cleanup entries.
        target = os.path.join(world_root, pat.target_path_glob.rstrip("/"))
        if os.path.isdir(target):
            results.append((idx, target))
    return results
