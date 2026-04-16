"""World discovery for alive-mcp — Roots-first, env-fallback, bounded walk.

The World is the ALIVE Context System folder on the human's machine. Every
tool and resource the server exposes is scoped to exactly one World per
server process (v0.1 decision; multi-world is v0.2). This module turns
the MCP Roots handshake and env-var fallbacks into a single resolved
World root.

World predicate (matches the existing ecosystem)
------------------------------------------------
A directory is a World root if EITHER:

1. It contains both ``01_Archive/`` and ``02_Life/`` (legacy hook
   predicate — see ``plugins/alive/hooks/scripts/alive-common.sh`` at
   L126 and L152 in the monorepo), OR
2. It contains ``.alive/`` (Hermes predicate — see
   ``hermes/memory-provider/__init__.py`` ``_find_world_root``).

Both predicates exist in the codebase today and CAN disagree — a fresh
install that has only run ``alive init`` may have ``.alive/`` but no
``02_Life/``; a legacy world-on-disk may have the domain folders but no
``.alive/``. alive-mcp accepts either so it never disagrees with the
rest of the ecosystem about what a World is.

Roots as the authorization boundary
-----------------------------------
Per the MCP spec, Roots declare what the HOST authorizes the server to
access. Walking above a Root would read outside that authorization —
contradicts our ``openWorldHint: False`` annotation and breaks the
consent model. Discovery therefore walks upward WITHIN each Root only:

- If a Root points AT a World root, the walk matches on step 0.
- If a Root points INSIDE a World (e.g. directly at a walnut), the walk
  climbs upward and finds the enclosing World — provided that World
  root is itself at or below the Root.
- If the Root is ABOVE a potential World (e.g. Root = ``$HOME`` while
  the World is at ``$HOME/world``), the upward walk never descends and
  we emit ``ERR_NO_WORLD`` with a pointed diagnostic: "your Root
  appears to be above a potential World — set Root to the World root
  directly."

Walking DOWN into a Root was explicitly rejected in plan-review round
3 (multi-world ambiguity, invasive scanning). The v0.1 contract is:
Root must point AT or INSIDE the World. README documents the correct
pattern per client.

Env fallback (mandatory, not optional)
--------------------------------------
Claude Desktop spawns the server without Roots in many configurations
(verified in the flow-gap-analyst research round). The env fallback
is therefore REQUIRED, not a nice-to-have:

- ``ALIVE_WORLD_ROOT`` — the established env var name (matches
  ``alive-common.sh`` L157). Primary.
- ``ALIVE_WORLD_PATH`` — accepted as an alias for forward compat. If
  both are set, ``ALIVE_WORLD_ROOT`` wins and a stderr log records the
  conflict.

The env value is itself walked upward within its own bound (same rule
as a Root) so ``ALIVE_WORLD_ROOT=~/world/02_Life/people/ben-flint``
still resolves to ``~/world`` if that satisfies the predicate.

Single-world v0.1
-----------------
If multiple candidate Roots each yield a different World, the server
picks the World with the SHORTEST realpath (most-encompassing — the
one that contains the others, if they're nested; otherwise the first
by sort). Others are logged to stderr. Multi-world with per-call
``world_id`` is v0.2.

Fallback never promotes Root
----------------------------
A Root that does NOT satisfy the predicate (on step 0 or any ancestor
within the Root bound) does NOT become a World. This was locked in
codex plan-review critical #1: promoting a Root to World invites
surprise scans and silent scope expansion. If no Root satisfies the
predicate, the env fallback is consulted; if that also fails, the
server raises ``WorldNotFoundError`` (mapped to ``ERR_NO_WORLD`` by
the envelope).

Public API
----------
- ``is_world_root(path)`` — predicate check, no walk.
- ``discover_world(roots=None, env=None)`` — resolve the single World
  from Roots + env. Returns an absolute realpath string or raises
  ``WorldNotFoundError``.
- ``audit_log_path(world_root)`` — canonical path to the audit log
  (``<world>/.alive/_mcp/audit.log``). Does NOT create the file; the
  audit writer (T12) owns creation and rotation.

The env parameter on ``discover_world`` is an optional mapping for
tests — defaults to ``os.environ``. Callers should rarely pass it.

References
----------
- codex plan review (fn-10-60k) round 3: locked "Roots-as-boundary,
  never walk above Root, never promote Root to World".
- Hermes predicate: ``hermes/memory-provider/__init__.py`` at
  ``_find_world_root``.
- Hook predicate: ``plugins/alive/hooks/scripts/alive-common.sh:126``
  (the ``01_Archive + 02_Life`` check inside ``find_world``).
"""

from __future__ import annotations

import os
import sys
from typing import Iterable, List, Mapping, Optional

from alive_mcp.errors import WorldNotFoundError


# Sentinels for the World predicate. Both are valid on their own.
_DOMAIN_SENTINELS = ("01_Archive", "02_Life")
_ALIVE_SENTINEL = ".alive"

# Env var names (primary + alias).
_ENV_PRIMARY = "ALIVE_WORLD_ROOT"
_ENV_ALIAS = "ALIVE_WORLD_PATH"


def _has_domain_sentinels(path: str) -> bool:
    """Return True if ``path`` contains BOTH ``01_Archive`` and ``02_Life``."""
    return all(
        os.path.isdir(os.path.join(path, name)) for name in _DOMAIN_SENTINELS
    )


def _has_alive_sentinel(path: str) -> bool:
    """Return True if ``path`` contains a ``.alive/`` subdirectory."""
    return os.path.isdir(os.path.join(path, _ALIVE_SENTINEL))


def is_world_root(path: str) -> bool:
    """Return True if ``path`` satisfies the World predicate.

    Accepts either the legacy-hook predicate (``01_Archive`` AND
    ``02_Life``) or the Hermes predicate (``.alive/``). Both are valid
    independently; neither is required alongside the other. See the
    module docstring for the rationale and source references.

    A missing or non-directory input returns False — callers don't have
    to guard with ``os.path.isdir`` first.
    """
    if not path or not os.path.isdir(path):
        return False
    return _has_domain_sentinels(path) or _has_alive_sentinel(path)


def _walk_within_bound(start: str, bound: str) -> Optional[str]:
    """Walk upward from ``start`` toward ``bound``, returning the first World ancestor.

    Returns the absolute realpath of the first ancestor that satisfies
    ``is_world_root``, where the walk is stopped at ``bound``
    (inclusive — ``bound`` itself is tested). Returns ``None`` if
    nothing between ``start`` and ``bound`` matches.

    The walk never steps ABOVE ``bound``. If ``start`` is not contained
    in ``bound`` (or equal to it), returns ``None`` immediately — that
    shape is the "Root is not above World" case the caller diagnoses.

    Both ``start`` and ``bound`` are realpathed before comparison so
    symlinks don't create false "outside-bound" verdicts.
    """
    if not start or not bound:
        return None

    resolved_start = os.path.realpath(start)
    resolved_bound = os.path.realpath(bound)

    # ``start`` must be at or below ``bound`` or the walk is over before
    # it begins. ``os.path.commonpath`` is the right primitive here for
    # the same reason as in ``paths.py``: segment-wise, not string
    # prefix.
    try:
        common = os.path.commonpath([resolved_start, resolved_bound])
    except ValueError:
        # Different drives on Windows, or other normalization mismatch.
        # Treat as "not contained", bail.
        return None
    if common != resolved_bound:
        return None

    # Climb from ``resolved_start`` upward one directory at a time,
    # stopping after we've tested ``resolved_bound`` itself. Each
    # iteration checks the current directory against the predicate
    # BEFORE climbing — so a walk that starts AT a World root returns
    # immediately.
    current = resolved_start
    while True:
        if is_world_root(current):
            return current
        if current == resolved_bound:
            # We've just tested the bound and it didn't match. Stop.
            return None
        parent = os.path.dirname(current)
        if parent == current:
            # Reached filesystem root without satisfying the bound — can
            # only happen if ``bound`` is the filesystem root itself
            # AND we didn't match. Bail.
            return None
        current = parent


def _candidates_from_roots(roots: Iterable[str]) -> List[str]:
    """Resolve each Root to a World root, dropping Roots with no match.

    Walks upward within each Root's own bounds (never above). Returns a
    list of unique absolute realpaths, preserving first-seen order.
    Roots that don't yield a World are silently dropped from this
    return value — the caller aggregates across Roots and env to
    decide whether the overall discovery failed.
    """
    seen: set[str] = set()
    candidates: List[str] = []
    for root in roots:
        if not root:
            continue
        # Walk within the Root itself as the bound. A Root that points
        # AT a World returns on step 0; a Root pointing at a walnut
        # INSIDE a World (which sits at or below the Root) returns the
        # enclosing World; a Root that sits ABOVE a potential World
        # returns None here (we never walk DOWN into the Root).
        match = _walk_within_bound(start=root, bound=root)
        if match is None:
            continue
        if match in seen:
            continue
        seen.add(match)
        candidates.append(match)
    return candidates


def _candidate_from_env(env: Mapping[str, str]) -> Optional[str]:
    """Resolve the env fallback to a World root, honoring primary-vs-alias.

    ``ALIVE_WORLD_ROOT`` wins if both are set. When both are set with
    DIFFERENT values, a stderr log notes the conflict so the human can
    reconcile their shell init. The env path is walked upward within
    its own bound (same rule as a Root).
    """
    primary = env.get(_ENV_PRIMARY, "").strip()
    alias = env.get(_ENV_ALIAS, "").strip()

    chosen: Optional[str] = None
    if primary and alias and os.path.normpath(primary) != os.path.normpath(alias):
        sys.stderr.write(
            "alive-mcp: both {0} and {1} are set with different values; "
            "using {0} (primary). Unset one to silence this.\n".format(
                _ENV_PRIMARY, _ENV_ALIAS
            )
        )
    if primary:
        chosen = primary
    elif alias:
        chosen = alias

    if not chosen:
        return None

    return _walk_within_bound(start=chosen, bound=chosen)


def _pick_shortest(candidates: List[str]) -> str:
    """Pick the single World from a list of candidates, logging losers to stderr.

    Selection rule (v0.1): shortest realpath by character count. Rationale:
    the shortest path is the most-encompassing — when Roots point at
    different walnuts inside the same World, they all resolve to the
    same World (already de-duped in ``_candidates_from_roots``); when
    Roots point at genuinely different Worlds, the one with the
    shortest path is typically the user's "main" World and the others
    are nested or sibling test worlds. Multi-world selection with
    ``world_id`` arrives in v0.2.

    Ties broken by lexicographic sort for determinism in tests.
    """
    assert candidates, "_pick_shortest called with empty list"
    if len(candidates) == 1:
        return candidates[0]

    # Sort: shortest first, then lexicographic within equal-length
    # buckets. ``sorted`` is stable, but we sort by ``(len, value)``
    # explicitly to make the tiebreaker readable.
    ordered = sorted(candidates, key=lambda p: (len(p), p))
    winner = ordered[0]
    losers = ordered[1:]

    sys.stderr.write(
        "alive-mcp: multiple World candidates resolved; picking shortest "
        "realpath {!r}. Dropped: {!r}\n".format(winner, losers)
    )
    return winner


def discover_world(
    roots: Optional[Iterable[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve the single World root from MCP Roots + env fallback.

    Order of precedence:

    1. For each Root in ``roots`` (iteration order), walk upward within
       that Root's own bounds and collect the first ancestor satisfying
       ``is_world_root``.
    2. If at least one Root yielded a World, pick the shortest realpath
       (v0.1 single-world rule) and return it.
    3. Otherwise, consult the env fallback. ``ALIVE_WORLD_ROOT`` wins
       over ``ALIVE_WORLD_PATH`` when both are set.
    4. If neither Roots nor env yields a World, raise
       ``WorldNotFoundError`` with a pointed diagnostic.

    ``roots`` defaults to an empty list; ``env`` defaults to
    ``os.environ``. Tests pass explicit dicts to control the
    environment without mutating the process.

    Returns an absolute, realpathed, NON-case-folded World root path.
    Callers that need to do case-insensitive compares pass the result
    through the ``paths`` module helpers — discovery does not
    case-fold.
    """
    if roots is None:
        roots = ()
    if env is None:
        env = os.environ

    candidates = _candidates_from_roots(roots)
    if candidates:
        return _pick_shortest(candidates)

    env_match = _candidate_from_env(env)
    if env_match is not None:
        return env_match

    # Diagnostic payload: tell the human the exact shapes that failed.
    # Keep it short — the envelope layer (T4/T5) wraps this in the
    # protocol error response with a ``hint`` field, but the exception
    # message is the authoritative diagnostic.
    tried_roots = list(roots)
    primary = env.get(_ENV_PRIMARY, "")
    alias = env.get(_ENV_ALIAS, "")
    raise WorldNotFoundError(
        "no World resolved from Roots or env. "
        "tried_roots={!r} {}={!r} {}={!r}. "
        "Hint: set {} to your World root (the directory that contains "
        "01_Archive/ + 02_Life/ or .alive/), or widen your client Roots "
        "to point AT or INSIDE the World (not above it).".format(
            tried_roots, _ENV_PRIMARY, primary, _ENV_ALIAS, alias, _ENV_PRIMARY
        )
    )


def audit_log_path(world_root: str) -> str:
    """Return the canonical audit log path under ``<world>/.alive/_mcp/``.

    This path is the only filesystem write target alive-mcp v0.1 has.
    The audit writer (T12) owns file creation, mode (``0o600``), and
    rotation. ``audit_log_path`` is pure — it computes the path and
    returns it without touching disk — so T5's lifespan can thread the
    path into the writer without racing it.

    The computation uses ``os.path.join`` so cross-platform separators
    work. The input is NOT realpathed here; the caller is expected to
    have already realpathed the World during discovery. The result is
    safe against the "Root at/below World" acceptance criterion
    because it's always anchored at the resolved World.
    """
    return os.path.join(world_root, _ALIVE_SENTINEL, "_mcp", "audit.log")


__all__ = [
    "audit_log_path",
    "discover_world",
    "is_world_root",
]
