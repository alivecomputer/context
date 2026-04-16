"""Error taxonomy for alive-mcp v0.1.

This module is the single source of truth for the error types and error
codes alive-mcp surfaces across its public seams: tool responses, resource
reads, world discovery, and path safety. The full envelope schema (the
``{ok, error: {code, message, hint}}`` shape returned by FastMCP tools)
lands in T4 (fn-10-60k.4); this T3-scaffolded file defines ONLY the
error classes and codes that path-safety and world-discovery need. T4
extends the taxonomy with kernel/tool-layer codes as it implements the
envelope.

Design notes
------------
- **Codes are string constants**, not an ``IntEnum``. The MCP protocol
  is JSON-over-stdio; string codes survive JSON serialization without
  ambiguity and read clearly in the audit log.
- **Exceptions carry a code attribute.** Tool layer catches the base
  ``AliveMcpError`` and emits an envelope error using
  ``exc.code`` / ``str(exc)``. This keeps the envelope mapping in one
  place (T4/T5) instead of scattering try/except ladders.
- **Re-exports from ``_vendor._pure``** are intentional.
  ``WorldNotFoundError`` and ``KernelFileError`` live in the vendor
  package because the extracted pure helpers raise them. alive-mcp
  surface code (this module's consumers) imports from here so it does
  not have to reach into ``_vendor`` — which is private. If T4 renames
  or re-homes any of these, this file is the only public seam that
  needs updating.

Security context
----------------
``PathEscapeError`` is the detection boundary for CVE-2025-53109-class
bugs (filesystem MCP servers bypassing their allowed-root check via a
prefix comparison). See ``paths.py`` for the detection implementation
and the rationale for using ``os.path.commonpath`` over ``startswith``.
"""

from __future__ import annotations

# Re-export vendor error types so consumers never reach into ``_vendor``
# directly. ``_vendor`` is private; ``errors`` is public.
from alive_mcp._vendor._pure import (  # noqa: F401  (re-export)
    KernelFileError,
    WorldNotFoundError,
)


# -----------------------------------------------------------------------------
# Error codes (string constants).
#
# The set below is the T3-minimum. T4 extends with tool-layer codes
# (ERR_KERNEL_FILE_MISSING, ERR_KERNEL_FILE_CORRUPT, ERR_INVALID_ARGS, ...)
# as it lands the envelope. Keep names aligned with the spec (ALL_CAPS,
# ``ERR_`` prefix, snake-shouty) so the audit log stays greppable.
# -----------------------------------------------------------------------------

ERR_PATH_ESCAPE = "ERR_PATH_ESCAPE"
"""Candidate path resolves outside its allowed World root.

Fires when:
- A relative or absolute input resolves (post-``realpath``) outside the
  configured World root, OR
- A symlink's target resolves outside the World root, OR
- Windows ``commonpath`` raises ``ValueError`` (paths on different
  drives — treated as "not inside").

This is the CVE-2025-53109 detection boundary. See ``paths.py`` for the
commonpath-based check and why ``startswith`` is NOT acceptable.
"""

ERR_NO_WORLD = "ERR_NO_WORLD"
"""No World could be resolved from the configured Roots or env fallback.

Fires when:
- The MCP ``roots/list`` handshake returned no Roots AND no env fallback
  is set, OR
- No Root, when walked upward within its own bounds, satisfies
  ``is_world_root``, OR
- The env fallback path does not satisfy ``is_world_root`` when walked
  within its own bound.

The diagnostic message should include a pointed suggestion: "set
``ALIVE_WORLD_ROOT`` to your World root, or widen your client Roots to
include it."
"""


# -----------------------------------------------------------------------------
# Base class + code-carrying exceptions.
#
# The tool/resource layer (T5+) catches ``AliveMcpError`` and maps
# ``exc.code`` into the response envelope. Callers inside path/world code
# raise the specific subclass with a human-readable message; the code
# constant is already attached via the class attribute.
# -----------------------------------------------------------------------------


class AliveMcpError(Exception):
    """Base for all alive-mcp errors that map to a protocol error code.

    Subclasses set ``code`` as a class attribute. The tool envelope layer
    (T4/T5) reads ``exc.code`` and ``str(exc)`` to build the response.
    """

    code: str = "ERR_UNKNOWN"


class PathEscapeError(AliveMcpError):
    """Raised when a candidate path escapes its allowed World root.

    The detection is defense-in-depth: realpath BOTH sides, then use
    ``os.path.commonpath`` (NOT ``startswith``) to compare. See the
    module docstring on ``paths.py`` for the full rationale and the
    CVE reference.
    """

    code: str = ERR_PATH_ESCAPE


__all__ = [
    # Codes (string constants).
    "ERR_NO_WORLD",
    "ERR_PATH_ESCAPE",
    # Exception base + subclasses.
    "AliveMcpError",
    "KernelFileError",
    "PathEscapeError",
    "WorldNotFoundError",
]
