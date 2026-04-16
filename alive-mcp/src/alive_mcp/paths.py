"""Path safety for alive-mcp — realpath + boundary-aware containment.

Every tool and resource handler that touches the filesystem goes through
the helpers in this module. The contract is simple: given a candidate
path and an allowed root (the resolved World root), either return a
normalized absolute path guaranteed to live inside the allowed root, or
raise ``PathEscapeError``. There is no "maybe" state and no string-level
check.

Why commonpath and not startswith
---------------------------------
CVE-2025-53109 (Anthropic filesystem MCP server, Jul 2025) is the
canonical example of the prefix-check bug this module exists to prevent:
comparing ``resolved_candidate.startswith(resolved_root)`` accepts
``<root>_sibling`` as a child of ``<root>``. ``os.path.commonpath`` does
not — it splits on the path separator and compares segment-by-segment.

The check is therefore::

    try:
        if os.path.commonpath([candidate, root]) == root:
            # inside
    except ValueError:
        # Windows: different drives -> raises. Treat as "outside".

The ``try`` / ``except ValueError`` wrapper is load-bearing: on Windows,
``commonpath`` raises when its arguments live on different drives. That
legitimately means "not contained" and must NOT be mapped to "allowed".

Why realpath both sides
-----------------------
Symlinks are the next trap. A candidate path that lexically sits inside
the allowed root can still resolve (via a symlink) to ``/etc/passwd``.
``os.path.realpath`` follows symlinks on both the candidate and the
allowed root, so the containment check runs on concrete filesystem
locations. The allowed root is also realpathed because the caller may
pass a path through a symlink (e.g. ``~/world`` pointing at iCloud's
``Library/Mobile Documents/...``).

Consequence: symlinks INSIDE the World that point to locations INSIDE
the World are fine — the resolved target is still contained. Symlinks
whose resolved target leaves the World are rejected. Documented in the
spec's "Symlink policy" section.

Platform case-fold policy (v0.1)
--------------------------------
HFS+/APFS (macOS default) and NTFS (Windows) are case-insensitive by
default. On those platforms, the containment check must treat
``/Users/foo`` and ``/Users/FOO`` as equivalent or it will reject
legitimate paths whose case drifted (user-typed input, client-supplied
Roots with a different case than the actual directory entry).

The task spec asks for ``os.path.normcase`` on darwin/win32. That's
the right primitive on Windows (it lowercases and flips separators) —
but on macOS, Python's ``os.path`` IS ``posixpath``, and
``posixpath.normcase`` is a no-op. So ``normcase`` alone would NOT
satisfy the acceptance criterion on macOS.

Resolution (v0.1): apply BOTH ``os.path.normcase`` AND
``str.casefold()`` on darwin/win32. ``casefold`` does the real
case-insensitive comparison; ``normcase`` handles Windows-specific
separator normalization. On Linux, neither is applied — POSIX
filesystems are case-sensitive and we respect that. This matches the
task acceptance criterion exactly and matches the spirit of the epic
spec's "Case-sensitivity note" (which describes ``normcase`` as
imperfect on macOS and ``casefold`` as the true fix).

Only the comparison values are case-folded; the returned path is the
raw realpath, so callers still get a valid filesystem path back.

Public API
----------
- ``safe_join(allowed_root, *parts)`` — join + resolve + contain.
- ``resolve_under(allowed_root, candidate)`` — validate a fully-formed
  candidate is contained; return the resolved absolute path.
- ``is_inside(allowed_root, candidate)`` — boolean check, no raise.

All three helpers share the SAME core resolution routine, ``_resolve``,
so there is one code path for both boolean and raising variants. The
raising variants translate "not inside" to ``PathEscapeError`` with a
diagnostic hint.

References
----------
- CVE-2025-53109: https://nvd.nist.gov/vuln/detail/CVE-2025-53109
- codex plan review rounds (fn-10-60k, 2026-04-16):
  locked "realpath + commonpath, try/except ValueError for Windows,
  NOT startswith" before T3 implementation.
"""

from __future__ import annotations

import os
import sys
from typing import Tuple

from alive_mcp.errors import PathEscapeError


# Platforms whose default filesystem is case-insensitive. We apply
# ``os.path.normcase`` before the boundary comparison on these. See
# module docstring for the v0.1 policy.
_CASE_FOLD_PLATFORMS = ("darwin", "win32")


def _normcase_if_needed(path: str) -> str:
    """Case-fold ``path`` for containment comparison on darwin/win32.

    Applies BOTH ``os.path.normcase`` and ``str.casefold``:

    - ``os.path.normcase`` does the right thing on Windows
      (lowercases, flips ``/`` to ``\\``) and is a no-op elsewhere.
    - ``str.casefold`` does the actual case-insensitive comparison on
      darwin (where ``os.path.normcase`` is a no-op because macOS'
      ``os.path`` is ``posixpath``).

    On Linux this returns the input unchanged — POSIX filesystems are
    case-sensitive and ``Foo`` and ``foo`` are distinct files.

    This transforms the value ONLY for comparison; the returned value
    is kept internal. Callers receive the raw realpath so they can
    stat/open it.
    """
    if sys.platform in _CASE_FOLD_PLATFORMS:
        return os.path.normcase(path).casefold()
    return path


def _realpath_both(candidate: str, allowed_root: str) -> Tuple[str, str]:
    """Resolve both sides to absolute, symlink-followed, case-normalized paths.

    Returns ``(resolved_candidate, resolved_root)``. The pair is ready to
    feed into ``os.path.commonpath`` — both are absolute, both follow
    symlinks, both are case-normalized on platforms where that matters.

    ``os.path.realpath`` returns an absolute path even when given a
    relative input, so this doubles as the "normalize relative paths"
    step. The candidate is NOT joined against the root here — that's
    ``safe_join``'s responsibility.
    """
    resolved_candidate = os.path.realpath(candidate)
    resolved_root = os.path.realpath(allowed_root)

    resolved_candidate = _normcase_if_needed(resolved_candidate)
    resolved_root = _normcase_if_needed(resolved_root)

    return resolved_candidate, resolved_root


def _contained(resolved_candidate: str, resolved_root: str) -> bool:
    """Return True iff ``resolved_candidate`` is at or under ``resolved_root``.

    Uses ``os.path.commonpath`` segment-wise comparison — the only
    stdlib primitive that distinguishes ``<root>`` from
    ``<root>_sibling``. ``startswith`` does not and is the bug behind
    CVE-2025-53109. See module docstring.

    The ``ValueError`` branch catches the Windows "different drives"
    case (e.g. candidate on ``C:`` and root on ``D:``) and any other
    normalization-mismatch ``commonpath`` rejects. Either way, "cannot
    establish a common path" means "not inside" — return False.
    """
    try:
        common = os.path.commonpath([resolved_candidate, resolved_root])
    except ValueError:
        return False
    return common == resolved_root


def _resolve(
    allowed_root: str,
    candidate: str,
) -> Tuple[str, str, str, bool]:
    """Core resolver shared by the public helpers.

    Returns ``(raw_resolved_candidate, resolved_root_case, resolved_candidate_case, inside)``.

    The raw resolved candidate (pre case-fold) is the value to RETURN to
    callers when the check passes — we don't want to hand back a
    lowercased path just because we used it for comparison. The
    case-folded values are the ones ``commonpath`` operates on.
    """
    # First, absolute + symlink-resolved (no case fold yet). This is
    # what we hand back on success so callers get a real filesystem
    # path, not a lowercased artifact.
    raw_resolved_candidate = os.path.realpath(candidate)
    raw_resolved_root = os.path.realpath(allowed_root)

    # Then case-fold copies for the containment check on HFS+/NTFS.
    resolved_candidate_case = _normcase_if_needed(raw_resolved_candidate)
    resolved_root_case = _normcase_if_needed(raw_resolved_root)

    inside = _contained(resolved_candidate_case, resolved_root_case)
    return (
        raw_resolved_candidate,
        resolved_root_case,
        resolved_candidate_case,
        inside,
    )


# -----------------------------------------------------------------------------
# Public API.
# -----------------------------------------------------------------------------


def is_inside(allowed_root: str, candidate: str) -> bool:
    """Return True iff ``candidate`` resolves to a path at or under ``allowed_root``.

    Non-raising variant of ``resolve_under``. Use when the caller needs
    to branch on containment (e.g. choosing which of several roots to
    try) rather than fail on escape.
    """
    _, _, _, inside = _resolve(allowed_root, candidate)
    return inside


def resolve_under(allowed_root: str, candidate: str) -> str:
    """Return the resolved absolute path of ``candidate`` if it's inside ``allowed_root``.

    Raises ``PathEscapeError`` if the resolved candidate is not
    contained in the resolved root. The returned path has symlinks
    followed and is absolute, but is NOT case-folded (callers get the
    real filesystem path, not the ``normcase`` artifact).

    This is the helper to use when the candidate is already a
    fully-formed path (e.g. decoded from a ``alive://`` URI or pulled
    from config). For building paths from World-relative components,
    use ``safe_join``.
    """
    raw_resolved, _, _, inside = _resolve(allowed_root, candidate)
    if not inside:
        raise PathEscapeError(
            "path escapes allowed root: "
            "candidate={!r} root={!r}".format(candidate, allowed_root)
        )
    return raw_resolved


def safe_join(allowed_root: str, *parts: str) -> str:
    """Join ``parts`` onto ``allowed_root`` and validate containment.

    This is the primary helper for MCP tool handlers: given a
    World-relative walnut path and a file name, produce a safe absolute
    path or raise. It handles three failure shapes uniformly:

    1. ``parts`` contains ``..`` segments that would climb above the
       root — rejected.
    2. ``parts`` contains an absolute path that replaces the root —
       ``os.path.join`` semantics: an absolute later component
       overrides earlier components. We still realpath the result and
       run the commonpath check, which catches this.
    3. A symlink in the joined result points outside the root — caught
       by the realpath step.

    Raises ``PathEscapeError`` on any of the above.
    """
    # ``os.path.join`` happily accepts an absolute later component and
    # drops everything before it — this is the attack vector for
    # ``safe_join(root, "/etc/passwd")``. realpath + commonpath catches
    # it because ``/etc/passwd`` is not under the root.
    joined = os.path.join(allowed_root, *parts)
    raw_resolved, _, _, inside = _resolve(allowed_root, joined)
    if not inside:
        raise PathEscapeError(
            "path escapes allowed root: "
            "parts={!r} root={!r}".format(parts, allowed_root)
        )
    return raw_resolved


__all__ = [
    "is_inside",
    "resolve_under",
    "safe_join",
]
