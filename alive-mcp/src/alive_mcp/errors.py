"""Error taxonomy for alive-mcp v0.1.

This module is the single source of truth for the error types and error
codes alive-mcp surfaces across its public seams: tool responses, resource
reads, world discovery, and path safety. The envelope schema (the
``{content, structuredContent, isError}`` shape returned by FastMCP tools)
lives in :mod:`alive_mcp.envelope`, which imports the codebook from here.

Taxonomy (v0.1 frozen set)
--------------------------
Nine codes cover every failure mode a read-only v0.1 tool can emit:

=========================== ========================================
Code                         Seam
=========================== ========================================
``ERR_NO_WORLD``            World discovery (T3)
``ERR_WALNUT_NOT_FOUND``    Walnut-tool layer (T6)
``ERR_BUNDLE_NOT_FOUND``    Bundle-tool layer (T7)
``ERR_KERNEL_FILE_MISSING`` Tool-level precondition (T6, T9)
``ERR_PERMISSION_DENIED``   Any OSError on read (POSIX ``EACCES``)
``ERR_PATH_ESCAPE``         Path safety (T3, CVE-2025-53109 boundary)
``ERR_INVALID_CURSOR``      Cursor pagination (T8)
``ERR_TOOL_TIMEOUT``        Per-tool deadline (T5 shell)
``ERR_AUDIT_DISK_FULL``     Audit writer (T12, ENOSPC)
=========================== ========================================

Design notes
------------
- **Codes live in :class:`ErrorCode`**, a ``str``/``Enum`` mixin. The
  spec asked for an ``enum.Enum``; ``str, Enum`` gives us type-safe
  identity in Python AND clean JSON serialization (each member IS its
  string value). This means ``ErrorCode.ERR_PATH_ESCAPE ==
  "ERR_PATH_ESCAPE"`` is ``True``, so downstream code that ran on the
  earlier string-constant API keeps working — the exported
  ``ERR_PATH_ESCAPE`` constants are now thin aliases pointing at the
  enum members.
- **Each code has a user-facing message template and a suggestions
  list**, held in :class:`ErrorSpec` records and wired up via the
  :data:`ERRORS` mapping. Templates use ``{placeholders}`` for values
  the caller provides (walnut name, bundle name, query) — never
  absolute filesystem paths. ``mask_error_details=True`` on FastMCP
  prevents raw exception strings leaking; this module is the
  allow-list of what CAN leak.
- **Exceptions carry a ``code`` attribute.** The tool layer catches
  :class:`AliveMcpError` and emits an envelope error using ``exc.code``
  and the codebook template. This keeps envelope mapping in one place
  (T5) instead of scattered try/except ladders.
- **Re-exports from :mod:`alive_mcp._vendor._pure`** are intentional.
  ``WorldNotFoundError``, ``KernelFileError``, and
  ``MalformedYAMLWarning`` live in the vendor package because the
  extracted pure helpers raise them. alive-mcp surface code imports
  from here so it does not have to reach into ``_vendor`` — which is
  private. If a future task renames or re-homes any of these, this
  file is the only public seam that needs updating.

Security context
----------------
``PathEscapeError`` is the detection boundary for CVE-2025-53109-class
bugs (filesystem MCP servers bypassing their allowed-root check via a
prefix comparison). See :mod:`alive_mcp.paths` for the detection
implementation and the rationale for using ``os.path.commonpath`` over
``startswith``.

Message safety
--------------
**No message template in this module contains an absolute filesystem
path.** The ``mask_error_details=True`` promise is that user-facing
errors never leak server-side paths; this module enforces that promise
at the template level. Callers that need to log a path for debugging
route it through the audit log (T12), not the envelope.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping, Union

# Re-export vendor error types so consumers never reach into ``_vendor``
# directly. ``_vendor`` is private; ``errors`` is public.
from alive_mcp._vendor._pure import (  # noqa: F401  (re-export)
    KernelFileError,
    MalformedYAMLWarning,
    WorldNotFoundError,
)


# -----------------------------------------------------------------------------
# Error code enum.
#
# ``str, Enum`` mixin: members are both strings and enum members. This
# gives type-safe identity (``isinstance(code, ErrorCode)``) AND clean
# JSON serialization (``json.dumps(ErrorCode.ERR_PATH_ESCAPE)`` emits
# ``'"ERR_PATH_ESCAPE"'``). Critically, ``ErrorCode.ERR_PATH_ESCAPE ==
# "ERR_PATH_ESCAPE"`` evaluates to ``True``, so the exported string
# constants below are interchangeable with the enum members at any
# comparison site.
#
# Python 3.11+ offers ``StrEnum`` as a canonical name for this mixin;
# we use the explicit ``str, Enum`` form because the project targets
# 3.10-3.13 per pyproject.toml.
# -----------------------------------------------------------------------------


class ErrorCode(str, enum.Enum):
    """Frozen set of error codes alive-mcp emits.

    The enum values are the wire identifiers (``"ERR_*"``). The
    :attr:`wire` property returns the short form (no ``ERR_`` prefix),
    which is what goes into the response envelope per the
    Merge/Workato convention.
    """

    ERR_NO_WORLD = "ERR_NO_WORLD"
    ERR_WALNUT_NOT_FOUND = "ERR_WALNUT_NOT_FOUND"
    ERR_BUNDLE_NOT_FOUND = "ERR_BUNDLE_NOT_FOUND"
    ERR_KERNEL_FILE_MISSING = "ERR_KERNEL_FILE_MISSING"
    ERR_PERMISSION_DENIED = "ERR_PERMISSION_DENIED"
    ERR_PATH_ESCAPE = "ERR_PATH_ESCAPE"
    ERR_INVALID_CURSOR = "ERR_INVALID_CURSOR"
    ERR_TOOL_TIMEOUT = "ERR_TOOL_TIMEOUT"
    ERR_AUDIT_DISK_FULL = "ERR_AUDIT_DISK_FULL"

    @property
    def wire(self) -> str:
        """Short-form wire identifier (drops the ``ERR_`` prefix).

        ``ErrorCode.ERR_WALNUT_NOT_FOUND.wire == "WALNUT_NOT_FOUND"``.
        This is what the envelope's ``structuredContent['error']`` field
        carries, following the Merge/Workato best-practice naming.
        """
        return self.value.removeprefix("ERR_")


# Type alias used by envelope factories that accept either the enum
# member or the raw string constant.
CodeLike = Union[ErrorCode, str]


# -----------------------------------------------------------------------------
# Module-level constants.
#
# Every :class:`ErrorCode` member is re-exported as an ``ERR_*`` module
# attribute. Because the enum is a ``str, Enum``, these constants
# compare equal to bare strings AND to each other via identity. This
# preserves the T3-era API where callers import ``ERR_PATH_ESCAPE``
# and compare with ``==``.
# -----------------------------------------------------------------------------

ERR_NO_WORLD: ErrorCode = ErrorCode.ERR_NO_WORLD
ERR_WALNUT_NOT_FOUND: ErrorCode = ErrorCode.ERR_WALNUT_NOT_FOUND
ERR_BUNDLE_NOT_FOUND: ErrorCode = ErrorCode.ERR_BUNDLE_NOT_FOUND
ERR_KERNEL_FILE_MISSING: ErrorCode = ErrorCode.ERR_KERNEL_FILE_MISSING
ERR_PERMISSION_DENIED: ErrorCode = ErrorCode.ERR_PERMISSION_DENIED
ERR_PATH_ESCAPE: ErrorCode = ErrorCode.ERR_PATH_ESCAPE
ERR_INVALID_CURSOR: ErrorCode = ErrorCode.ERR_INVALID_CURSOR
ERR_TOOL_TIMEOUT: ErrorCode = ErrorCode.ERR_TOOL_TIMEOUT
ERR_AUDIT_DISK_FULL: ErrorCode = ErrorCode.ERR_AUDIT_DISK_FULL


# -----------------------------------------------------------------------------
# Error specs: message template + actionable suggestions per code.
#
# Held as :class:`ErrorSpec` records for type safety and grouped via
# the :data:`ERRORS` mapping. The convenience :data:`MESSAGES` and
# :data:`SUGGESTIONS` dicts are derived projections — the spec record
# is the single source of truth.
#
# **No template contains an absolute filesystem path.** Placeholders
# carry walnut names, bundle names, kernel file stems, and query
# strings — all caller-facing identifiers, none server-internal paths.
# The audit log (T12) captures internal paths for debugging; the
# envelope never does.
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorSpec:
    """User-facing error template + recovery suggestions for a code.

    ``slots=True`` would be cleaner but is 3.10+; we use the default
    dataclass form for compatibility with the widest ``_pure`` import
    chain. ``frozen=True`` catches accidental mutation of the codebook.
    """

    code: ErrorCode
    message: str
    suggestions: tuple[str, ...]


ERRORS: Mapping[ErrorCode, ErrorSpec] = {
    ErrorCode.ERR_NO_WORLD: ErrorSpec(
        code=ErrorCode.ERR_NO_WORLD,
        message=(
            "No ALIVE World could be located. Set ALIVE_WORLD_ROOT in the "
            "server environment, or widen the client's Roots to include a "
            "directory that contains '.alive/' or both '01_Archive/' and "
            "'02_Life/'."
        ),
        suggestions=(
            "Set the ALIVE_WORLD_ROOT environment variable to the World root.",
            "Configure the MCP client's Roots to include the World directory.",
            "Verify the World directory contains '.alive/' or the legacy "
            "'01_Archive/' + '02_Life/' pair.",
        ),
    ),
    ErrorCode.ERR_WALNUT_NOT_FOUND: ErrorSpec(
        code=ErrorCode.ERR_WALNUT_NOT_FOUND,
        message="No walnut found at '{walnut}' in this World.",
        suggestions=(
            "Call 'list_walnuts' to see available walnuts in this World.",
            "Walnut paths are POSIX-relative from the World root (e.g. "
            "'04_Ventures/nova-station'), not bare names.",
        ),
    ),
    ErrorCode.ERR_BUNDLE_NOT_FOUND: ErrorSpec(
        code=ErrorCode.ERR_BUNDLE_NOT_FOUND,
        message="No bundle found at '{bundle}' in walnut '{walnut}'.",
        suggestions=(
            "Call 'list_bundles' with the walnut path to see available bundles.",
            "Bundle paths are relative to the walnut root (e.g. "
            "'bundles/shielding-review').",
        ),
    ),
    ErrorCode.ERR_KERNEL_FILE_MISSING: ErrorSpec(
        code=ErrorCode.ERR_KERNEL_FILE_MISSING,
        message=(
            "The '{file}' kernel file has not been written for walnut "
            "'{walnut}'."
        ),
        suggestions=(
            "Fresh walnuts legitimately lack some kernel files. If the "
            "walnut is active, it has not been saved yet.",
            "Valid 'file' values: 'key', 'log', 'insights', 'now'.",
        ),
    ),
    ErrorCode.ERR_PERMISSION_DENIED: ErrorSpec(
        code=ErrorCode.ERR_PERMISSION_DENIED,
        message="Permission denied reading '{file}' in walnut '{walnut}'.",
        suggestions=(
            "Check filesystem permissions on the walnut directory.",
            "Ensure the MCP server process has read access to the World root.",
        ),
    ),
    ErrorCode.ERR_PATH_ESCAPE: ErrorSpec(
        code=ErrorCode.ERR_PATH_ESCAPE,
        message=(
            "The requested path is outside the authorized World root and "
            "was rejected."
        ),
        suggestions=(
            "Paths must resolve inside the authorized World root after "
            "symlink resolution.",
            "Use POSIX-relative paths returned by 'list_walnuts' and "
            "'list_bundles' verbatim; do not construct absolute paths.",
        ),
    ),
    ErrorCode.ERR_INVALID_CURSOR: ErrorSpec(
        code=ErrorCode.ERR_INVALID_CURSOR,
        message="The pagination cursor is invalid or has expired.",
        suggestions=(
            "Drop the 'cursor' argument and retry from the first page.",
            "Cursors do not survive server restarts.",
        ),
    ),
    ErrorCode.ERR_TOOL_TIMEOUT: ErrorSpec(
        code=ErrorCode.ERR_TOOL_TIMEOUT,
        message="Tool '{tool}' exceeded its {timeout_s:.1f}s deadline.",
        suggestions=(
            "Narrow the query to reduce the search space.",
            "Use cursor pagination (smaller 'limit') to stay under the deadline.",
        ),
    ),
    ErrorCode.ERR_AUDIT_DISK_FULL: ErrorSpec(
        code=ErrorCode.ERR_AUDIT_DISK_FULL,
        message=(
            "The audit log could not be written (disk full or permissions). "
            "Tool results are not being recorded."
        ),
        suggestions=(
            "Free disk space on the filesystem holding '.alive/_mcp/'.",
            "Rotate or archive existing audit logs if retention is not needed.",
        ),
    ),
}


# Convenience projections, keyed by the enum member. Because the enum
# is a ``str, Enum`` mixin, string keys (``"ERR_PATH_ESCAPE"``) also
# work here — ``ErrorCode.ERR_PATH_ESCAPE == "ERR_PATH_ESCAPE"`` is
# ``True`` and hashes identically.
MESSAGES: Mapping[ErrorCode, str] = {
    code: spec.message for code, spec in ERRORS.items()
}
SUGGESTIONS: Mapping[ErrorCode, tuple[str, ...]] = {
    code: spec.suggestions for code, spec in ERRORS.items()
}


# The frozen v0.1 code set, as an enum tuple. Tools MUST emit only
# codes in this set; tests assert template+suggestion coverage across
# it exactly.
ERROR_CODES: tuple[ErrorCode, ...] = tuple(ErrorCode)


# -----------------------------------------------------------------------------
# Base class + code-carrying exceptions.
#
# The tool/resource layer (T5+) catches ``AliveMcpError`` and maps
# ``exc.code`` into the response envelope. Callers inside tool code raise
# the specific subclass; the code constant is already attached via the
# class attribute.
#
# Subclass-per-code is a deliberate choice over a single parameterized
# exception:
# - ``except WalnutNotFoundError`` reads better than ``except AliveMcpError
#   if exc.code == ErrorCode.ERR_WALNUT_NOT_FOUND``.
# - Type checkers can narrow on subclass.
# - Codes stay discoverable via ``ERROR_CODES`` without needing to
#   instantiate anything.
# -----------------------------------------------------------------------------


class AliveMcpError(Exception):
    """Base for all alive-mcp errors that map to a protocol error code.

    Subclasses set ``code`` as a class attribute. The tool envelope layer
    (T4/T5) reads ``exc.code`` to build the response. Raw exception
    messages are NEVER surfaced to clients — the codebook's message
    template is always preferred, so the ``mask_error_details=True``
    guarantee holds even when a subclass is raised with sensitive
    detail in ``str(exc)`` (e.g. "escape via /etc/passwd").
    """

    code: CodeLike = "ERR_UNKNOWN"


class PathEscapeError(AliveMcpError):
    """Raised when a candidate path escapes its allowed World root.

    The detection is defense-in-depth: realpath BOTH sides, then use
    ``os.path.commonpath`` (NOT ``startswith``) to compare. See the
    module docstring on :mod:`alive_mcp.paths` for the full rationale
    and the CVE reference.
    """

    code: ErrorCode = ErrorCode.ERR_PATH_ESCAPE


class WalnutNotFoundError(AliveMcpError):
    """Raised when a requested walnut path does not resolve to a walnut."""

    code: ErrorCode = ErrorCode.ERR_WALNUT_NOT_FOUND


class BundleNotFoundError(AliveMcpError):
    """Raised when a requested bundle path does not resolve to a bundle."""

    code: ErrorCode = ErrorCode.ERR_BUNDLE_NOT_FOUND


class KernelFileMissingError(AliveMcpError):
    """Raised when a kernel file is absent on disk (not corrupt).

    Distinct from :class:`KernelFileError` (re-exported from
    ``_vendor._pure``), which fires when a kernel file IS on disk but
    unreadable. Missing-on-disk is a tool precondition; present-but-
    unreadable is a vendor-layer I/O failure.
    """

    code: ErrorCode = ErrorCode.ERR_KERNEL_FILE_MISSING


class PermissionDeniedError(AliveMcpError):
    """Raised when the OS denies read access to a walnut or bundle file."""

    code: ErrorCode = ErrorCode.ERR_PERMISSION_DENIED


class InvalidCursorError(AliveMcpError):
    """Raised when a pagination cursor fails to decode or validate."""

    code: ErrorCode = ErrorCode.ERR_INVALID_CURSOR


class ToolTimeoutError(AliveMcpError):
    """Raised when a tool invocation exceeds its per-call deadline."""

    code: ErrorCode = ErrorCode.ERR_TOOL_TIMEOUT


class AuditDiskFullError(AliveMcpError):
    """Raised when the audit writer cannot append to audit.log."""

    code: ErrorCode = ErrorCode.ERR_AUDIT_DISK_FULL


__all__ = [
    # Enum + type alias.
    "ErrorCode",
    "CodeLike",
    # Codebook primitives.
    "ErrorSpec",
    "ERRORS",
    "ERROR_CODES",
    "MESSAGES",
    "SUGGESTIONS",
    # Module-level code aliases (preserve T3 API).
    "ERR_NO_WORLD",
    "ERR_WALNUT_NOT_FOUND",
    "ERR_BUNDLE_NOT_FOUND",
    "ERR_KERNEL_FILE_MISSING",
    "ERR_PERMISSION_DENIED",
    "ERR_PATH_ESCAPE",
    "ERR_INVALID_CURSOR",
    "ERR_TOOL_TIMEOUT",
    "ERR_AUDIT_DISK_FULL",
    # Exception base + subclasses.
    "AliveMcpError",
    "AuditDiskFullError",
    "BundleNotFoundError",
    "InvalidCursorError",
    "KernelFileError",
    "KernelFileMissingError",
    "MalformedYAMLWarning",
    "PathEscapeError",
    "PermissionDeniedError",
    "ToolTimeoutError",
    "WalnutNotFoundError",
    "WorldNotFoundError",
]
