"""Bundle-schema signal source.

Scans walnut bundle frontmatter for v3.1+ canonical fields. The
canonical-fields set was introduced with the bundle schema rewrite at
v3.1 (epic spec § Approach -- bundle schema fingerprint):

* ``species``
* ``phase``
* ``goal``
* ``context_routes``

Presence of ANY of these in a ``context.manifest.yaml`` (or other bundle
header file inside a walnut) imputes ``≥ v3.1``. Their absence in
otherwise valid v3 bundles imputes ``v3.0`` (a v3.0 bundle still has
frontmatter, just not the canonical-rewrite fields).

YAML reads use stdlib regex -- mirrors
``generate-index.py:extract_frontmatter()`` -- per R10. NO PyYAML.

Snapshot input shape:
    Bundle YAMLs are captured ``head``-mode at 2 KiB; we only need the
    frontmatter dict, never the body. The orchestrator's combined
    snapshot allowlist receives our rule contributions via
    :func:`snapshot_rule_contributions`.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, List, Optional, Set

from . import (
    SCOPE_WALNUT,
    SOURCE_SCHEMA,
    SignalProbe,
)


__all__ = (
    "CANONICAL_FIELDS",
    "walnut_probes",
    "snapshot_rule_contributions",
)


#: Canonical bundle-schema fields introduced at v3.1.
CANONICAL_FIELDS: frozenset = frozenset((
    "species",
    "phase",
    "goal",
    "context_routes",
))


# Frontmatter delimiter pattern -- mirrors generate-index.py:32.
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
# Top-level key extraction (one key per line, no nested-key support).
_KV_RE = re.compile(r"^(\w[\w-]*)\s*:", re.MULTILINE)


def _fm_keys(blob: bytes, *, allow_pure_yaml: bool = False) -> Set[str]:
    """Return the set of top-level keys in *blob*.

    Accepts:
      * markdown-frontmatter form (``---\\n<keys>\\n---``) -- legacy
        ``companion.md``-style bundles. Always parsed.
      * pure YAML form (top-level ``key: value`` lines) -- canonical
        v3 ``context.manifest.yaml`` bundles. Parsed only when
        ``allow_pure_yaml=True``.

    Empty set if the blob is empty, undecodable, or carries no
    top-level keys.

    Bug-fix (T15 / fn-18.15): the original implementation required the
    ``---`` frontmatter fence to fire. Real v3 bundles ship as pure
    YAML (no fences), so the canonical-field probe never fired against
    fixtures or production worlds and detection landed at the v3.0
    kernel-floor for every walnut.

: pure-YAML parsing is gated by an explicit
    flag so legacy ``companion.md`` files (markdown body, sometimes
    with prose lines like ``goal: ship X``) cannot be misclassified
    as canonical v3.1 bundles. Callers pass ``allow_pure_yaml=True``
    only when the source filename is ``context.manifest.yaml``.
    """
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover -- decode is robust w/ errors=replace
        return set()
    # Markdown-frontmatter form first (legacy companion.md). When the
    # fence pattern matches, only the fenced body counts as keys.
    m = _FM_RE.match(text)
    if m:
        body = m.group(1)
        return set(_KV_RE.findall(body))
    if not allow_pure_yaml:
        # No fence, no pure-YAML opt-in -- treat as no frontmatter.
        return set()
    # Pure YAML form: extract top-level keys from the whole body. The
    # ``_KV_RE`` regex is anchored with ``^`` (MULTILINE) so it only
    # matches keys at column 0 -- nested indented keys are excluded.
    return set(_KV_RE.findall(text))


def _bundle_paths_under(snapshot: Any, walnut_path: str) -> List[str]:
    """Return snapshot paths that look like bundle-frontmatter sources.

    Bundles in v3 live at the walnut root; their frontmatter sits in
    ``context.manifest.yaml`` at the bundle's root. Pre-v3 (capsules)
    used ``companion.md``. We accept either name; the schema check
    only fires when canonical fields are present, so legacy companion
    files without the canonical set produce a ``v3.0`` floor (consistent
    with their actual layout).
    """
    walnut_path = os.path.abspath(walnut_path)
    matches: List[str] = []
    walnut_with_sep = walnut_path.rstrip(os.sep) + os.sep
    for path in snapshot.paths():
        if not path.startswith(walnut_with_sep):
            continue
        bn = os.path.basename(path)
        if bn in ("context.manifest.yaml", "companion.md"):
            matches.append(path)
    return matches


def walnut_probes(snapshot: Any, walnut_path: str) -> List[SignalProbe]:
    """Bundle-schema probes for one *walnut_path*."""
    out: List[SignalProbe] = []
    walnut_path = os.path.abspath(walnut_path)
    bn = os.path.basename(walnut_path) or walnut_path
    bundle_paths = _bundle_paths_under(snapshot, walnut_path)

    if not bundle_paths:
        # Single absent probe so the all-signals payload records that
        # the schema source was consulted at this scope.
        out.append(SignalProbe(
            probe_id="schema_no_bundles@{}".format(bn),
            source=SOURCE_SCHEMA,
            scope=SCOPE_WALNUT,
            fired=False,
            inferred_version=None,
            walnut_path=walnut_path,
            detail="no bundle-frontmatter sources in snapshot under walnut",
        ))
        return out

    canonical_hits = 0
    full_canonical_hits = 0  # bundles with EVERY canonical field
    bundles_with_fm = 0
    for path in bundle_paths:
        try:
            blob = snapshot.read(path)
        except (KeyError, ValueError):
            # exists_only / missing-data; skip silently.
            continue
        # Pure-YAML parsing only for context.manifest.yaml; legacy
        # companion.md stays frontmatter-only.
        allow_pure_yaml = (
            os.path.basename(path) == "context.manifest.yaml"
        )
        keys = _fm_keys(blob, allow_pure_yaml=allow_pure_yaml)
        if not keys:
            continue
        bundles_with_fm += 1
        if keys & CANONICAL_FIELDS:
            canonical_hits += 1
        if CANONICAL_FIELDS.issubset(keys):
            full_canonical_hits += 1

    if full_canonical_hits > 0:
        # Codex completion-review fix: bundles carrying EVERY
        # canonical field (species + phase + goal + context_routes)
        # are the v3.2 bundle shape per ``_v3_2_walnut`` in the
        # fixture generators. The v3.1 bundle shape carries a
        # PARTIAL canonical set (phase + goal + status). Distinguishing
        # the two at walnut scope lets the no-op gate's strict-equality
        # predicate fire on a clean v3.2 walnut.
        out.append(SignalProbe(
            probe_id="schema_canonical_v32@{}".format(bn),
            source=SOURCE_SCHEMA,
            scope=SCOPE_WALNUT,
            fired=True,
            inferred_version="3.2.0",
            walnut_path=walnut_path,
            detail=(
                "{}/{} bundles carry FULL canonical fields ({})"
                .format(
                    full_canonical_hits, len(bundle_paths),
                    sorted(CANONICAL_FIELDS),
                )
            ),
        ))
    elif canonical_hits > 0:
        out.append(SignalProbe(
            probe_id="schema_canonical_v31@{}".format(bn),
            source=SOURCE_SCHEMA,
            scope=SCOPE_WALNUT,
            fired=True,
            inferred_version="3.1",
            walnut_path=walnut_path,
            detail=(
                "{}/{} bundles carry canonical fields ({})".format(
                    canonical_hits, len(bundle_paths),
                    sorted(CANONICAL_FIELDS),
                )
            ),
        ))
    elif bundles_with_fm > 0:
        # Bundles with frontmatter but no canonical fields = v3.0 floor.
        out.append(SignalProbe(
            probe_id="schema_pre_canonical@{}".format(bn),
            source=SOURCE_SCHEMA,
            scope=SCOPE_WALNUT,
            fired=True,
            inferred_version="3.0",
            walnut_path=walnut_path,
            detail=(
                "{} bundles have frontmatter but no canonical v3.1 "
                "fields".format(bundles_with_fm)
            ),
        ))
    else:
        # No frontmatter on any bundle => no schema signal.
        out.append(SignalProbe(
            probe_id="schema_no_frontmatter@{}".format(bn),
            source=SOURCE_SCHEMA,
            scope=SCOPE_WALNUT,
            fired=False,
            inferred_version=None,
            walnut_path=walnut_path,
            detail=(
                "{} bundle file(s) found but none carried frontmatter"
                .format(len(bundle_paths))
            ),
        ))
    return out


def snapshot_rule_contributions() -> List[Any]:
    """``SnapshotRule`` contributions for the schema source."""
    from ..file_snapshot import SnapshotRule  # noqa: PLC0415

    return [
        # Bundle frontmatter -- head-mode @ 2 KiB.
        SnapshotRule(
            glob="<world>/**/context.manifest.yaml",
            max_bytes=2048,
        ),
        SnapshotRule(
            glob="<world>/**/companion.md",
            max_bytes=2048,
        ),
    ]
