"""Legacy-aware target resolver for ``alive system-upgrade``.

The strict resolver in ``_common.find_world_root_with_strategy`` (and the
``_world_root_io.is_valid_world_root`` predicate it leans on) refuses
legacy worlds: they have no ``.alive/`` marker and may have no canonical
numbered domain dirs. The redesign of ``system-upgrade`` is the **one**
command that legitimately needs to operate on legacy worlds, so this
resolver intentionally relaxes the strict predicate -- but only for
high-confidence markers, and never via heuristics that would walk into
nested directories searching for evidence (those would mis-target a
normal repo root containing fixture walnuts).

Marker rules (first hit wins, evaluated at the candidate-directory ROOT):

1. ``<candidate>/.alive/`` exists                              (v3 worlds)
2. >= 2 of ``WORLD_ROOT_DOMAIN_DIRS`` + legacy ``03_Inputs/``  (legacy v3 / v2)
3. ``<candidate>/.walnut/`` exists                             (v1 state dir)
4. ``<candidate>/_core/`` AND ``<candidate>/companion.md``     (v1 world-as-walnut)
5. ``<candidate>/_core/`` AND ``<candidate>/now.md``           (v1 variant)
6. ``<candidate>/companion.md`` AND ``now.md`` AND ``tasks.md`` (v1 no-_core triple)

Explicitly **un-numbered** legacy domains (``archive/``, ``life/``,
``ventures/``, ...) are NOT auto-detected; the user must pass
``--world-root <path>`` because un-numbered names overlap with normal
directory names in unrelated repos.
"""

from __future__ import annotations

import os
from typing import List, Optional

from _world_root_io import WORLD_ROOT_DOMAIN_DIRS as _CANONICAL_DOMAINS


__all__ = (
    "CANDIDATE_DOMAINS",
    "MISSING_WORLD_HINT",
    "MISSING_WORLD_HINT_UNNUMBERED",
    "ResolveError",
    "resolve_target_world",
)


# Pull the canonical numbered-domain set from ``_world_root_io`` so a
# future addition to that constant flows here automatically. Add the
# legacy ``03_Inputs/`` alias (renamed to ``03_Inbox/`` in v3); legacy
# worlds are exactly what this resolver targets.
CANDIDATE_DOMAINS = tuple(_CANONICAL_DOMAINS) + ("03_Inputs",)


MISSING_WORLD_HINT = (
    "no world detected at {cwd} or any parent; pass `<world-path>` or "
    "`--world-root <path>` to target a legacy world explicitly. "
    "(system-upgrade refuses to guess the target for destructive "
    "operations.)"
)

# Hint to surface when the resolver suspects an un-numbered legacy
# layout (lowercase ``archive/``, ``life/``, ``ventures/``, etc.) at
# cwd. Auto-detection refuses these because the names overlap with
# unrelated repos -- explicit ``--world-root`` is required.
MISSING_WORLD_HINT_UNNUMBERED = (
    "no world detected at {cwd}; if this is an un-numbered-legacy-domain "
    "world (`archive/`, `life/`, `ventures/`, etc.), pass `--world-root "
    "<path>` explicitly -- auto-detection refuses to guess for "
    "destructive operations on legacy folder shapes."
)

# Lowercase legacy domain names we look for as a *hint trigger* only.
# Their presence at cwd flips the error message to MISSING_WORLD_HINT_UNNUMBERED.
_UNNUMBERED_LEGACY_DOMAINS = (
    "archive", "life", "ventures", "experiments", "inbox",
)


class ResolveError(Exception):
    """Raised when ``resolve_target_world`` cannot find a world.

    Carries a human-readable ``message`` and a ``hint_kind`` so callers
    can pick the right exit-code path (the redesign maps both kinds to
    exit code ``3`` -- ``not found``).
    """

    def __init__(self, message: str, hint_kind: str = "missing_world") -> None:
        super().__init__(message)
        self.message = message
        self.hint_kind = hint_kind


def _has_dir(path: str) -> bool:
    """True iff ``path`` exists and is a directory (not a symlink to one)."""
    return os.path.isdir(path) and not os.path.islink(path)


def _has_file(path: str) -> bool:
    """True iff ``path`` exists and is a regular file (not a symlink)."""
    return os.path.isfile(path) and not os.path.islink(path)


def _matches_marker_at(candidate: str) -> bool:
    """True iff ``candidate`` matches one of the six high-confidence markers."""
    # 1. .alive/ marker
    if _has_dir(os.path.join(candidate, ".alive")):
        return True

    # 2. >= 2 canonical numbered domain dirs (incl. legacy 03_Inputs)
    domain_count = 0
    for domain in CANDIDATE_DOMAINS:
        if _has_dir(os.path.join(candidate, domain)):
            domain_count += 1
            if domain_count >= 2:
                return True

    # 3. .walnut/ state dir
    if _has_dir(os.path.join(candidate, ".walnut")):
        return True

    # 4-5. _core/ paired with companion.md or now.md
    has_core = _has_dir(os.path.join(candidate, "_core"))
    has_companion = _has_file(os.path.join(candidate, "companion.md"))
    has_now = _has_file(os.path.join(candidate, "now.md"))
    if has_core and (has_companion or has_now):
        return True

    # 6. triple cluster without _core/
    has_tasks = _has_file(os.path.join(candidate, "tasks.md"))
    if has_companion and has_now and has_tasks:
        return True

    return False


def _walk_up(start: str) -> List[str]:
    """Yield each ancestor of ``start`` from ``start`` up to ``/`` inclusive."""
    out = []
    cur = os.path.abspath(start)
    while True:
        out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return out


def _looks_unnumbered(cwd: str) -> bool:
    """Heuristic: at least one un-numbered legacy domain present at cwd."""
    for name in _UNNUMBERED_LEGACY_DOMAINS:
        if _has_dir(os.path.join(cwd, name)):
            return True
    return False


def resolve_target_world(cwd: Optional[str] = None) -> str:
    """Walk up from ``cwd`` looking for a world-root marker.

    Parameters
    ----------
    cwd:
        Starting directory. ``None`` -> ``os.getcwd()``.

    Returns
    -------
    str
        Absolute path of the first ancestor that matches one of the
        six high-confidence markers.

    Raises
    ------
    ResolveError
        No marker found by the time the walk hits ``/``. The exception's
        ``hint_kind`` is ``missing_world_unnumbered`` when ``cwd`` itself
        looks like an un-numbered-legacy-domain world (``archive/`` /
        ``life/`` / ...), otherwise ``missing_world``.
    """
    if cwd is None:
        cwd = os.getcwd()
    cwd_abs = os.path.abspath(cwd)

    for candidate in _walk_up(cwd_abs):
        if _matches_marker_at(candidate):
            return candidate

    # No marker found anywhere in the walk-up chain. Pick the right hint
    # based on whether cwd ITSELF looks like an un-numbered legacy world.
    if _looks_unnumbered(cwd_abs):
        raise ResolveError(
            MISSING_WORLD_HINT_UNNUMBERED.format(cwd=cwd_abs),
            hint_kind="missing_world_unnumbered",
        )
    raise ResolveError(
        MISSING_WORLD_HINT.format(cwd=cwd_abs),
        hint_kind="missing_world",
    )
