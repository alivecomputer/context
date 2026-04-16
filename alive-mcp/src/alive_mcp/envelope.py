"""Structured response envelope for alive-mcp tools.

Every tool in the v0.1 roster (list_walnuts, get_walnut_state,
read_walnut_kernel, list_bundles, get_bundle, read_bundle_manifest,
search_world, search_walnut, read_log, list_tasks) returns the envelope
produced by :func:`ok` on success or :func:`error` on failure. The
envelope is hand-assembled to match MCP's ``CallToolResult`` schema
without importing any pydantic model — keeping ``envelope.py`` import-
cost zero and stdlib-pure is the point. FastMCP happily accepts
plain-dict returns that match the schema.

MCP ``CallToolResult`` schema (2025-06-18)
------------------------------------------

.. code-block:: python

    {
      "content": [TextContent | ImageContent | ...],   # required
      "structuredContent": dict | None,                # optional
      "isError": bool,                                 # default False
    }

``content`` is what clients display to humans (text blocks). New clients
read ``structuredContent`` for machine-parseable data. Clients that
still predate the structured-content field fall back to parsing
``content[0].text`` as JSON; we render that way so both paths work.

Error envelope shape
--------------------

The success envelope's ``structuredContent`` carries the tool's return
payload. Dict payloads flow through unchanged (merged with any
``**meta``); non-dict payloads (lists, scalars) are wrapped under a
``data`` key so ``structuredContent`` is always a JSON object, as MCP
requires. The error envelope's ``structuredContent`` is a fixed
record::

    {
      "error": "<code-without-ERR_-prefix>",     # e.g. "WALNUT_NOT_FOUND"
      "message": "<formatted template>",          # from errors.ERRORS
      "suggestions": ["...", "..."]               # from errors.ERRORS
    }

The ``error`` field drops the ``ERR_`` prefix to follow the
Merge/Workato convention cited in the spec — shorter identifiers, same
information content. The full :class:`ErrorCode` enum value (with
prefix) still drives everything internally.

Why hand-build instead of importing ``mcp.types.CallToolResult``
---------------------------------------------------------------

1. **Import cost.** ``envelope`` is hot — every tool call assembles
   one. Importing pydantic types pulls in validation machinery we don't
   need on the return path (FastMCP validates on the way out).
2. **Error isolation.** If the mcp SDK churns its model shape between
   1.27 and 2.0 (it is still pre-2.0), our envelope tests catch the
   drift at build time, not at runtime.
3. **Testability.** Pure-dict assembly means tests can assert
   ``response["structuredContent"]["error"] == "WALNUT_NOT_FOUND"``
   without instantiating a validator.

mask-error-details invariant
----------------------------

:func:`error` never emits an absolute filesystem path in ``message``.
Message templates in :mod:`alive_mcp.errors` are pre-audited for that
property (tested by the no-absolute-path tests in test_errors.py), and
the kwargs this module accepts are formatted into those templates
verbatim — so if a caller passes ``walnut="/Users/me/..."`` the leak
is at the call site, not here. Guidance: callers always pass caller-
facing identifiers (walnut names, bundle names, kernel file stems,
query strings). The T12 audit writer captures internal paths for
debugging; the envelope never does.

:func:`error_from_exception` takes the stricter stance: it NEVER
surfaces ``str(exc)`` to the client. Unknown codes degrade to "An
unknown error occurred." rather than echoing the exception message.
The tool layer's contract is that it only raises codes in
:data:`errors.ERROR_CODES`; this branch is the safety net if that
contract is ever violated, and preserving the mask-error-details
promise matters more than losing debug detail (which the audit log
captures anyway).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Tuple

from alive_mcp import errors


# Defense-in-depth redaction patterns. These run over every formatted
# error message and every string kwarg that reaches template formatting,
# so even if a caller accidentally passes an absolute path as context,
# the client sees ``<path>`` instead of the real location.
#
# POSIX: ``/`` followed by at least one path segment of letters, digits,
# dots, underscores, hyphens, or tildes — catches ``/etc/passwd``,
# ``/Users/foo/bar``, ``/.alive/_mcp/audit.log``, ``/_kernel/log.md``.
# The leading ``/`` must be at a word boundary (start of string, after
# whitespace, quote, ``=``, ``:``, ``(``, ``[``, ``{``, or similar) so
# we don't accidentally redact URL paths or relative-looking fragments.
_POSIX_ABS_PATH = re.compile(
    r"(?:(?<=^)|(?<=[\s'\"`(\[\{=,:;]))/[A-Za-z0-9._~\-]+(?:/[A-Za-z0-9._~\-]+)*"
)

# Windows: ``C:\`` or ``D:/`` drive letter followed by any path
# characters. Simpler than POSIX because the drive prefix is
# unambiguous.
_WINDOWS_ABS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s'\"`)]*")


def _redact_paths(text: str) -> str:
    """Strip absolute filesystem paths from ``text``, replacing with ``<path>``.

    Runs on EVERY user-facing error message and on every string kwarg
    before it reaches template formatting. This is defense-in-depth:
    the codebook templates are audited to not reference paths at all,
    but a caller that accidentally passes ``file="/Users/me/log.md"``
    would still get that path interpolated if we didn't redact here.

    Takes a moderately conservative approach — false positives (over-
    redacting something that happens to look like a path) are
    preferable to false negatives (leaking a real path) per the
    ``mask_error_details=True`` guarantee.
    """
    text = _POSIX_ABS_PATH.sub("<path>", text)
    text = _WINDOWS_ABS_PATH.sub("<path>", text)
    return text


def _sanitize_kwargs(template_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Redact absolute paths from string kwargs before templating."""
    out: dict[str, Any] = {}
    for k, v in template_kwargs.items():
        if isinstance(v, str):
            out[k] = _redact_paths(v)
        else:
            out[k] = v
    return out


def _text_content(text: str) -> dict[str, Any]:
    """Render a single MCP ``TextContent`` block as a plain dict.

    Shape matches ``mcp.types.TextContent`` (``{type: "text", text: str}``).
    ``annotations`` and ``_meta`` are omitted — both are optional and
    default to ``None`` on the wire. Keeping them out keeps the rendered
    JSON short and deterministic, which matters for Inspector snapshot
    diffs (T14).
    """
    return {"type": "text", "text": text}


def ok(data: Any, **meta: Any) -> dict[str, Any]:
    """Wrap a successful tool result in the MCP response envelope.

    Parameters
    ----------
    data:
        The tool's payload. Must be JSON-serializable. Lists, dicts, and
        scalars all work; the envelope does not impose a schema on top
        of the tool's own contract. This value goes into
        ``structuredContent`` verbatim.
    **meta:
        Optional metadata merged into ``structuredContent`` alongside
        ``data``. Typical keys are pagination signals (``next_cursor``,
        ``total``). Meta keys do NOT shadow the data shape — callers
        should not put application payload in kwargs. Rejected if any
        meta key collides with a key in ``data`` (when ``data`` is a
        dict); that would silently hide a field and is almost always a
        bug. The collision raises :class:`ValueError` up to the caller.

    Returns
    -------
    dict
        A dict with ``content``, ``structuredContent``, and ``isError``
        keys, matching MCP's ``CallToolResult`` schema.

    Notes
    -----
    ``content[0].text`` is a JSON serialization of the structured
    payload, so legacy clients that only read text content still get
    usable data. ``separators=(",", ":")`` keeps the text tight; LLMs
    read JSON fine either way but smaller payloads cost fewer tokens.
    """
    # Compute the collision set BEFORE any dict unpacking so the
    # collision check is identical for dict and non-dict payloads. For
    # non-dict payloads the wrapper injects a ``data`` key, so ``data``
    # in ``meta`` would silently replace the actual payload — the
    # exact shadowing bug we refuse to accept.
    if isinstance(data, dict):
        payload_keys = set(data)
    else:
        payload_keys = {"data"}

    overlap = payload_keys & set(meta)
    if overlap:
        raise ValueError(
            f"envelope.ok: meta keys collide with payload keys: {sorted(overlap)}"
        )

    if isinstance(data, dict):
        structured: dict[str, Any] = {**data, **meta}
    else:
        # Scalar / list payload. Wrap under a ``data`` key so the
        # structuredContent is always an object (MCP requires object).
        structured = {"data": data, **meta}

    text = json.dumps(
        structured, separators=(",", ":"), ensure_ascii=False, sort_keys=False
    )
    return {
        "content": [_text_content(text)],
        "structuredContent": structured,
        "isError": False,
    }


def _normalize_code(
    code: errors.CodeLike,
) -> Tuple[str, Optional[errors.ErrorCode]]:
    """Resolve ``code`` to (wire-form short-code, enum member or None).

    Accepts either an :class:`errors.ErrorCode` member or a raw string.
    Because the enum is a ``str, Enum`` mixin, comparison between the
    two is transparent — this helper exists so the caller gets a
    concrete enum member (for codebook lookup) OR a None (signaling the
    code is unknown to the codebook).
    """
    if isinstance(code, errors.ErrorCode):
        return code.wire, code

    if isinstance(code, str):
        enum_code: Optional[errors.ErrorCode]
        try:
            enum_code = errors.ErrorCode(code)
        except ValueError:
            # Try the wire form (prefix missing): ``"WALNUT_NOT_FOUND"``
            # -> ``ErrorCode.ERR_WALNUT_NOT_FOUND``. Callers that copy
            # the short code from ``structuredContent['error']`` back
            # into a call will hit this path.
            if not code.startswith("ERR_"):
                try:
                    enum_code = errors.ErrorCode(f"ERR_{code}")
                except ValueError:
                    enum_code = None
            else:
                enum_code = None
        short = (
            code.removeprefix("ERR_") if code.startswith("ERR_") else (code or "UNKNOWN")
        )
        return short, enum_code

    # Should never happen in typed call sites, but defend anyway.
    return "UNKNOWN", None


def error(code: errors.CodeLike, **template_kwargs: Any) -> dict[str, Any]:
    """Wrap an error in the MCP response envelope.

    Parameters
    ----------
    code:
        An :class:`errors.ErrorCode` member or the equivalent ``ERR_*``
        string. Unknown codes fall through to a generic ``UNKNOWN``
        envelope (the tool layer should not be emitting unknown codes,
        but the envelope refuses to ever crash a response path over it).
    **template_kwargs:
        Values substituted into the message template. Keys that the
        template does not reference are ignored — this is intentional,
        so callers can pass a consistent set of context kwargs
        (``walnut=..., bundle=...``) across calls without keeping per-
        code kwarg lists.

        **Never pass absolute filesystem paths.** The envelope does not
        strip them; it only trusts the templates in
        :mod:`alive_mcp.errors` to have no path placeholders. Callers
        are responsible for passing walnut names, bundle names, kernel
        file stems, and query strings — not server-internal paths.

    Returns
    -------
    dict
        A dict with ``content``, ``structuredContent`` (the error
        record), and ``isError=True``.

    Notes
    -----
    The ``error`` field in the structured record drops the ``ERR_``
    prefix, following the Merge/Workato convention (the spec cites it
    as the modern best-practice pattern). The full enum value stays
    the source of truth internally — this is a surface rename, not an
    alternate namespace.
    """
    short_code, enum_code = _normalize_code(code)

    spec = errors.ERRORS.get(enum_code) if enum_code is not None else None
    if spec is None:
        # Unknown code — still return a well-formed envelope. The tool
        # layer's contract is that it always emits known codes; this
        # branch is defense-in-depth so the envelope never itself
        # crashes a response.
        message = "An unknown error occurred."
        suggestions: tuple[str, ...] = ()
    else:
        short_code = enum_code.wire  # type: ignore[union-attr]
        # Defense-in-depth: redact absolute paths from string kwargs
        # BEFORE they reach template formatting, and again from the
        # final message. Belt AND suspenders — the codebook templates
        # do not reference paths, but a caller that passes
        # ``walnut="/Users/me/..."`` would otherwise see that path
        # interpolated, defeating mask_error_details=True.
        safe_kwargs = _sanitize_kwargs(dict(template_kwargs))
        try:
            message = spec.message.format(**safe_kwargs)
        except (KeyError, ValueError, TypeError, IndexError):
            # Any format failure — missing placeholder, spec mismatch
            # (``{timeout_s:.1f}`` with a non-numeric kwarg), or a
            # malformed spec — degrades to the unformatted template.
            # We do NOT surface the offending placeholder name or the
            # raw exception string: both count as internal detail that
            # the mask_error_details=True promise forbids leaking. The
            # audit log (T12) is the right channel for debug info.
            message = spec.message
        # Final sanitizer pass covers the (unlikely) case where a
        # template itself ever grew an absolute-path literal. The
        # no-absolute-path test in test_errors.py prevents that at
        # merge time, but this is cheap and belt-and-suspenders.
        message = _redact_paths(message)
        suggestions = spec.suggestions

    structured: dict[str, Any] = {
        "error": short_code,
        "message": message,
        "suggestions": list(suggestions),
    }

    text = json.dumps(
        structured, separators=(",", ":"), ensure_ascii=False, sort_keys=False
    )
    return {
        "content": [_text_content(text)],
        "structuredContent": structured,
        "isError": True,
    }


def error_from_exception(
    exc: errors.AliveMcpError, **extra_kwargs: Any
) -> dict[str, Any]:
    """Build an error envelope from an :class:`AliveMcpError`.

    Reads ``exc.code`` and passes it through :func:`error`. **Never
    surfaces ``str(exc)``** — the codebook template always wins, and
    unknown codes degrade to "An unknown error occurred." rather than
    echoing the exception message. This preserves the
    ``mask_error_details=True`` guarantee even when a subclass is
    raised with sensitive detail in its string form (e.g. "escape via
    /etc/passwd"). If a caller needs debug information about the raw
    exception, the audit log (T12) is the right channel — not the
    envelope.

    ``extra_kwargs`` are forwarded to :func:`error` for template
    substitution. The tool layer typically carries a context dict
    (``walnut=..., bundle=...``) for exactly this.
    """
    return error(exc.code, **extra_kwargs)


__all__ = [
    "ok",
    "error",
    "error_from_exception",
]
