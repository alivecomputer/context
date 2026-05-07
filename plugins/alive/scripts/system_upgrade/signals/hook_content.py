"""Hook/script content signal source.

Scans user-extension files (``.alive/skills/``, ``.alive/rules/``,
``.alive/hooks/``) for two contradictory references:

* ``ALIVE_PLUGIN_ROOT``           -- introduced at v3.1 (commit
                                     f565c81). Presence imputes ≥v3.1.
* ``plugins/alive/scripts/...``    -- the pre-v3.1 hardcoded plugin
                                     path. Presence imputes ≤v3.0.

When both fire (a partially-migrated user extension), the lowest-wins
resolver pulls the world to ``v3.0`` (the floor implied by the
hardcoded reference). The migration walkthrough (T8/T9) eats the
remaining hardcoded references.

Reads come from the snapshot only; binary files / non-UTF8 content are
skipped silently. The compiled regex is shared with the catalog matcher
in :mod:`retired_patterns` (we re-derive it here so the two sources
remain audit-independent -- if the catalog rotated to a new pattern,
T3 must surface the divergence rather than silently follow).
"""

from __future__ import annotations

import os
import re
from typing import Any, List, Optional

from . import (
    SCOPE_WORLD,
    SOURCE_CONTENT,
    SignalProbe,
)


__all__ = (
    "ALIVE_PLUGIN_ROOT_RE",
    "HARDCODED_PLUGIN_PATH_RE",
    "USER_EXTENSION_GLOBS",
    "world_probes",
    "snapshot_rule_contributions",
)


ALIVE_PLUGIN_ROOT_RE = re.compile(r"\bALIVE_PLUGIN_ROOT\b")
# Mirrors the catalog's f565c81 entry; kept locally so audits can
# verify the two sources line up without an inter-module dependency.
HARDCODED_PLUGIN_PATH_RE = re.compile(r"\bplugins/alive/scripts/\S+")


# User-extension trees scanned by the content source. The snapshot
# allowlist contributions match these directly.
USER_EXTENSION_GLOBS = (
    "<world>/.alive/skills/**/*.md",
    "<world>/.alive/skills/**/*.sh",
    "<world>/.alive/rules/**/*.md",
    "<world>/.alive/hooks/**/*.sh",
    "<world>/.alive/hooks/**/*.md",
)


def _is_user_extension_path(world_root: str, path: str) -> bool:
    """Return True iff *path* lives under a scanned user-extension tree."""
    world_root = os.path.abspath(world_root)
    path = os.path.abspath(path)
    # DERIVED: user-extension scan-prefix catalog -- the signal source
    # MUST list the canonical scan dirs to do its job (per R5 audit
    # exception: catalog entries, not verifier callsites)
    prefixes = (
        os.path.join(world_root, ".alive", "skills") + os.sep,  # DERIVED: scan-prefix catalog entry
        os.path.join(world_root, ".alive", "rules") + os.sep,  # DERIVED: scan-prefix catalog entry
        os.path.join(world_root, ".alive", "hooks") + os.sep,  # DERIVED: scan-prefix catalog entry
    )
    return path.startswith(prefixes)


def _decode(blob: bytes) -> Optional[str]:
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def world_probes(snapshot: Any, world_root: str) -> List[SignalProbe]:
    """Content probes for the world. Returns at most two fired probes
    (``has_ALIVE_PLUGIN_ROOT``, ``has_hardcoded_plugin_path``) plus
    absent variants when no user extensions are present.
    """
    world_root = os.path.abspath(world_root)
    plugin_root_hits: List[str] = []
    hardcoded_hits: List[str] = []
    scanned = 0
    for path in snapshot.paths():
        if not _is_user_extension_path(world_root, path):
            continue
        try:
            blob = snapshot.read(path)
        except (KeyError, ValueError):
            continue
        scanned += 1
        text = _decode(blob)
        if text is None:
            continue
        if ALIVE_PLUGIN_ROOT_RE.search(text):
            plugin_root_hits.append(path)
        if HARDCODED_PLUGIN_PATH_RE.search(text):
            hardcoded_hits.append(path)

    out: List[SignalProbe] = []
    # Forensic completeness (R16 + spec § Approach all_signals_raw):
    # both content probes always emit, even when no user extensions
    # exist. Consumers reading all_signals_raw need negative evidence
    # for every probed feature so they can distinguish "feature
    # absent" from "feature not consulted".
    if scanned == 0:
        absent_detail = "no user-extension files in snapshot"
    else:
        if plugin_root_hits:
            absent_detail = ""
        else:
            absent_detail = "no extension references ALIVE_PLUGIN_ROOT (scanned {})".format(scanned)
    out.append(SignalProbe(
        probe_id="content_alive_plugin_root",
        source=SOURCE_CONTENT,
        scope=SCOPE_WORLD,
        fired=bool(plugin_root_hits),
        inferred_version="3.1" if plugin_root_hits else None,
        walnut_path=None,
        detail=(
            "{} extension(s) reference ALIVE_PLUGIN_ROOT".format(
                len(plugin_root_hits)
            )
            if plugin_root_hits else absent_detail
        ),
    ))
    if scanned == 0:
        absent_detail = "no user-extension files in snapshot"
    else:
        absent_detail = (
            "no extension carries pre-v3.1 hardcoded path (scanned {})"
            .format(scanned)
        )
    out.append(SignalProbe(
        probe_id="content_hardcoded_plugin_path",
        source=SOURCE_CONTENT,
        scope=SCOPE_WORLD,
        fired=bool(hardcoded_hits),
        inferred_version="3.0" if hardcoded_hits else None,
        walnut_path=None,
        detail=(
            "{} extension(s) reference plugins/alive/scripts/...".format(
                len(hardcoded_hits)
            )
            if hardcoded_hits else absent_detail
        ),
    ))
    return out


def snapshot_rule_contributions() -> List[Any]:
    """``SnapshotRule`` contributions for the content source."""
    from ..file_snapshot import SnapshotRule  # noqa: PLC0415

    rules: List[Any] = []
    for g in USER_EXTENSION_GLOBS:
        # Full-mode (within DEFAULT_MAX_BYTES) so the regex sees all
        # body content. User extensions are typically << 256 KiB.
        rules.append(SnapshotRule(glob=g))
    return rules
