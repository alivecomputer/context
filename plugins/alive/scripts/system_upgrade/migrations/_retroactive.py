"""Retroactive last-upgrade synthesis for messy worlds (T9).

Some worlds have NO ``.alive/upgrades/`` history but a content-
fingerprint resolution implies prior upgrades happened (e.g. a world
that was on v2 in 2026-01 and is now sitting on v3.0 layout but
lacks the canonical record under ``.alive/upgrades/``). For these
"messy worlds" the migration runner synthesises a backfill record at
``<world>/.alive/upgrades/<iso-ts>-retroactive.yaml`` so the next
run sees a coherent prior state.

The retroactive record carries ``synthesized_from: fingerprint`` (vs
``synthesized_from: live_run`` for normal records). It is consumed
ONLY by this module's own de-duplication check on subsequent runs --
T7's ``load_prior_final_record`` excludes the ``-retroactive.yaml``
suffix by strict regex.

Existing personal-world records at
``.alive/upgrade-log.yaml`` and ``.alive/_generated/upgrade-log-v3.yaml``
are PRESERVED untouched -- the retroactive sibling is purely additive.

Stdlib-only (R10).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import _record


__all__ = (
    "is_messy_world",
    "synthesize_retroactive_record",
)


def is_messy_world(world_root: str) -> bool:
    """Return True iff *world_root* has no upgrade-history record.

    The predicate is filesystem-only -- callers pre-resolve the world
    root and pass an absolute path. We treat the directory as "no
    history" when:

    * ``.alive/upgrades/`` does not exist, OR
    * the directory exists but contains zero HISTORY files. Operational
      state files (``-runstate.yaml``, ``-resume.yaml``) are NOT
      history -- they describe an in-flight or just-finished run, not
      a prior upgrade event. History files are:

      - canonical final records (``<filename-safe-iso>.yaml`` with no
        suffix), AND
      - retroactive records (``<filename-safe-iso>-retroactive.yaml``).

    The legacy personal-world files at ``.alive/upgrade-log.yaml`` and
    ``.alive/_generated/upgrade-log-v3.yaml`` are NOT consulted here:
    they are preserved as historical context but the rolling
    ``.alive/upgrades/`` directory is the authoritative location and
    a world that has only the legacy files is still "messy" from the
    new-format perspective (and gets a retroactive record on top).
    """
    upgrades_dir = os.path.join(world_root, ".alive", "upgrades")
    if not os.path.isdir(upgrades_dir):
        return True
    try:
        entries = os.listdir(upgrades_dir)
    except OSError:
        # Unreadable -- treat as messy (the surrounding code will
        # surface the OSError separately if it tries to write here).
        return True
    for name in entries:
        if not name.endswith(".yaml"):
            continue
        # Operational-state suffixes are NOT history.
        if name.endswith("-runstate.yaml"):
            continue
        if name.endswith("-resume.yaml"):
            continue
        # Anything else is either a canonical final record or a
        # retroactive backfill -- both count as history.
        return False
    return True


def synthesize_retroactive_record(
    world_root: str,
    started_iso: str,
    *,
    inferred_source_version: str,
    target_version: str,
    tool_version_at_run: str,
    operations: Optional[List[Dict[str, Any]]] = None,
    detection_signals: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Write the retroactive record for a messy world.

    Returns the absolute path written, or ``None`` if the world is
    NOT messy (a canonical or retroactive record already exists --
    the de-dup short-circuit). Per the spec acceptance criteria, the
    retroactive record carries ``synthesized_from: fingerprint`` and
    accurate inferred-source-version.

    Idempotency: if a retroactive record already exists for this run
    timestamp the helper short-circuits without re-writing (the
    record is forensic; rewriting it on every retry would corrupt
    the audit trail).
    """
    if not is_messy_world(world_root):
        # Existing record family present -- de-dup short-circuit.
        return None

    target = _record.retroactive_path_for(world_root, started_iso)
    if os.path.isfile(target):
        # Same-timestamp re-entry; preserve the existing record.
        return target

    return _record.write_retroactive(
        world_root,
        started_iso,
        inferred_source_version=inferred_source_version,
        target_version=target_version,
        tool_version_at_run=tool_version_at_run,
        operations=operations,
        detection_signals=detection_signals,
    )
