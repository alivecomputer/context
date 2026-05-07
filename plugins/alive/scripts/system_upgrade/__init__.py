"""``system_upgrade`` -- redesign of the ``/alive:system-upgrade`` skill.

Owned by fn-18 (v3.2-redesign). Provides the ``alive system-upgrade``
subcommand surface plus phase-coordinator skeleton, pre-flight guard
chain, ``UpgradeLock`` wrapper, ``FileSnapshot`` primitive, and the
phase-5 no-op short-circuit gate.

T1 ships the architecture; T3-T11 fill in the per-phase implementations.

Public constants
----------------
TARGET_WORLD_VERSION
    The world-format version that the redesign migrates worlds TO.
    Compared against detection results in the no-op short-circuit gate
    (phase 5). Bumped in lockstep with each plugin minor version that
    introduces world-format changes; never derived from ``plugin.json``.

Public helpers
--------------
``_normalize_version`` -- normalize a version string into a tuple of
ints suitable for equality comparison. Tolerates leading ``v`` and
short forms (``"3.2"`` vs ``"3.2.0"``).
"""

from __future__ import annotations

from typing import Tuple


__all__ = ("TARGET_WORLD_VERSION", "_normalize_version")


#: World-format version the redesign migrates TO. Hardcoded; never
#: read from ``plugin.json`` (that is the *tool* version, a different
#: concern -- see epic spec § Tool version vs world version).
TARGET_WORLD_VERSION: str = "3.2.0"


def _normalize_version(v: str) -> Tuple[int, ...]:
    """Normalize a dotted version string into an ``(int, ...)`` tuple.

    Strips a leading ``v`` (case-insensitive) and pads short forms with
    zeros so ``"3.2"``, ``"v3.2"``, and ``"3.2.0"`` all compare equal.

    Non-integer segments are rejected by raising ``ValueError`` so the
    caller never silently mis-compares (e.g. dev builds tagged
    ``3.2.0-rc1`` MUST surface as a hard error rather than parsing as
    ``(3, 2, 0)`` and missing the ``-rc1`` distinction).
    """
    if v is None:
        raise ValueError("version is None")
    raw = str(v).strip()
    if not raw:
        raise ValueError("version is empty")
    if raw[:1].lower() == "v":
        raw = raw[1:]
    parts = raw.split(".")
    out = []
    for part in parts:
        if not part or not part.isdigit():
            raise ValueError(
                "version segment {!r} is not a non-negative integer in "
                "{!r}".format(part, v)
            )
        out.append(int(part))
    # Pad to length 3 so 3.2 == 3.2.0.
    while len(out) < 3:
        out.append(0)
    return tuple(out)
