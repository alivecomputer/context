"""Search tools (fn-10-60k.8 / T8).

Two read-only grep-style tools with cursor pagination:

* :func:`search_world` -- substring match across every searchable file in
  every walnut in the current World. Returns ``{matches, next_cursor,
  skipped}`` paginated by an opaque base64url-encoded JSON cursor.
* :func:`search_walnut` -- the same surface scoped to a single walnut
  identified by POSIX relpath.

Tools are annotated
``ToolAnnotations(readOnlyHint=True, destructiveHint=False,
openWorldHint=False, idempotentHint=True)``.

Frozen file inclusion rules (v0.1, deterministic)
------------------------------------------------
Extensions searched: ``.md``, ``.yaml``, ``.yml``, ``.json``.

Per walnut, in fixed order:

1. ``_kernel/key.md``
2. ``_kernel/log.md``
3. ``_kernel/insights.md``
4. ``_kernel/now.json`` (v3 only -- v2 fallback is NOT searched;
   see the explicitly-excluded list below)
5. Each discovered bundle's ``context.manifest.yaml`` (top-level only),
   sorted by the bundle's POSIX relpath from the walnut root.

Explicitly NOT searched in v0.1 (reserved for later):

* Any file under a bundle's ``raw/`` directory (binary detection and
  large-file handling are v0.2).
* ``_kernel/history/*.md`` chapter files (too noisy for a grep surface
  -- clients that want chapters hit the T9 ``read_log`` tool once it
  lands).
* ``_kernel/links.yaml`` and ``_kernel/people.yaml`` (overflow files,
  v0.2 adds dedicated reader tools).
* ``_kernel/_generated/now.json`` (v2 fallback). The reader tools
  (``get_walnut_state``, ``read_walnut_kernel``) accept the v2
  location because they're identifying a single file; search is a
  content surface where the ``_kernel/now.json`` inclusion rule is
  frozen -- v2 projections are reserved for v0.2 along with the
  other overflow files.

Skip dirs mirror the vendored ``walnut_paths._SKIP_DIRS`` set:
``node_modules``, ``.git``, ``__pycache__``, ``.venv``, ``venv``,
``dist``, ``build``, ``.next``, ``target``, ``templates``, ``raw``.

Max file size 500KB (``_MAX_FILE_BYTES``). Files exceeding the cap are
skipped with an entry appended to ``structuredContent.skipped`` shaped
``{"path": <posix_relpath>, "reason": "file_too_large"}``. Path
anchoring differs per tool:

* :func:`search_world` emits walnut-anchored paths
  (``04_Ventures/alive/_kernel/log.md``) so clients can disambiguate
  which walnut the skipped file belongs to without cross-referencing
  the ``matches`` list.
* :func:`search_walnut` emits walnut-relative paths
  (``_kernel/log.md``) -- the caller already supplied the walnut
  scope, so re-prefixing would be noise.

Either way the path format is what the caller would echo back to a
targeted reader tool.

Query matching
--------------
* Case-insensitive by default (``case_sensitive: bool = False``).
* Literal substring match (NOT regex in v0.1).
* Unicode normalization: both the query AND each scanned line are
  folded to NFKC before comparison. NFKC is the compatibility-
  composed form -- stronger than NFC, which only canonicalizes (e.g.
  ``e`` + combining acute -> ``\u00e9``). NFKC additionally collapses
  compatibility equivalents (ligature ``\ufb00`` -> ``ff``, full-width
  Latin -> ASCII, superscripts to normal digits, etc.). This matches
  the spec's "Unicode-case-folded query matches across NFKC-
  equivalent strings" acceptance criterion.

Stable ordering
---------------
Load-bearing for T14 Inspector contract snapshots: same query against
same fixture MUST produce identical results across calls.

1. Walnuts sorted by realpath ascending (spec-frozen). Ties broken by
   POSIX relpath so the ordering is fully deterministic even when
   two walnuts happen to share a realpath prefix.
2. Files within a walnut: the fixed order above.
3. Matches within a file: by line number ascending.

Cursor pagination
-----------------
Cursors are base64url-encoded JSON: ``{"v": 1, "wi": int, "fi": int,
"lo": int}`` where:

* ``v`` -- schema version (``1``). Future changes bump this and tests
  assert the default stays 1 across the v0.1 lifecycle.
* ``wi`` -- walnut index (0-based position in the sorted walnut list).
* ``fi`` -- file index (0-based position in the fixed per-walnut file
  order, after the file list has been built for that walnut).
* ``lo`` -- line offset within the current file. Next read resumes at
  line ``lo + 1`` (1-based line numbers).

The codec validates base64url decoding, JSON parsing, and the four
integer fields. Any failure -> :class:`errors.InvalidCursorError`
(``ERR_INVALID_CURSOR``) with guidance to restart from page 1.

Cursor stability and World mutation
-----------------------------------
Cursors are stable across calls within a server run *as long as the
walnut inventory + file plan for the active walnut are stable*. The
``wi`` / ``fi`` indices reference a list shape; mutating that shape
between calls can invalidate the cursor.

This is the deliberate v0.1 trade-off: stateless index-based cursors
keep server memory O(active requests) rather than O(in-flight
paginations × World size) -- which matters when a World has
thousands of walnuts and pagination sessions may span minutes. The
cost is that World mutations between pages are not snapshot-
consistent:

* **Walnut added before the cursor's slot** -- the cursor still
  points at index ``wi`` in the new (longer) list, but that index
  now references a DIFFERENT walnut. Added walnuts whose alphabetic
  slot precedes the cursor will NOT appear in this pagination run;
  callers that need snapshot consistency must restart from page 1.
* **Walnut added after the cursor's slot** -- appears on a later
  page naturally; no duplication, no skips.
* **Walnut removed at or before the cursor's wi** -- the cursor
  points beyond the end of the (shorter) list. Detected by
  :func:`_cursor_in_range` and surfaced as ``ERR_INVALID_CURSOR``
  so the caller restarts from page 1 rather than seeing a silent
  "empty page = search done" signal.
* **File removed at or before the cursor's fi** -- same detection +
  surfacing as walnut removal.
* **Content edited within a file** -- ``lo`` is a line offset, not
  a byte offset; edits that preserve line count resume correctly,
  edits that add/remove lines shift subsequent match positions but
  do not corrupt pagination shape (the scanner tolerates an ``lo``
  past the file end by yielding zero matches and advancing).

Callers that need stronger guarantees (a large search with parallel
writers) should finish a search before the World changes, or
retry from page 1 on ``ERR_INVALID_CURSOR``. The spec accepts this
trade-off for v0.1 to avoid materializing a snapshot of the entire
World per server run.

Result shape per match
----------------------
.. code-block:: python

    {
      "walnut": "04_Ventures/alive",       # POSIX relpath
      "file": "_kernel/log.md",             # POSIX relpath from walnut root
      "line_number": 42,                    # 1-based
      "content": "...matched line, 200-char cap...",
      "context_before": ["line40", "line41"],   # <= 2 strings
      "context_after":  ["line43", "line44"],   # <= 2 strings
    }

``content`` is the matched line with a hard 200-char truncation (UTF-8
codepoint boundary preserved -- we slice on the Python string, not the
bytes). Context arrays are at most 2 entries each; near file start /
end they shrink accordingly (the array is shorter, NOT padded with
empty strings).

Limits
------
``limit`` default 20, max 100. Values outside ``[1, 100]`` are clamped
rather than rejected (matches the T6 list-walnuts posture: predictable
behavior beats error-on-out-of-bounds at the schema boundary).

Error posture
-------------
* ``ERR_NO_WORLD`` when the lifespan never resolved a World.
* ``ERR_INVALID_CURSOR`` when the cursor fails decode / validation.
* ``ERR_WALNUT_NOT_FOUND`` on ``search_walnut`` for a missing walnut.
* ``ERR_PATH_ESCAPE`` on walnut resolution if the input escapes World.

Files that are unreadable (permission denied, I/O error, decode
failure) are dropped silently from the search -- search is a
best-effort surface and noisy permission errors would dominate the
response body on realistic worlds. The audit log (T12) captures the
specific errno for diagnostics.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from alive_mcp import envelope, errors
from alive_mcp.paths import is_inside
from alive_mcp.tools._audit_stub import audited
from alive_mcp.tools.walnut import (
    _iter_walnut_paths,
    _resolve_walnut,
    _walnut_not_found_envelope,
)

logger = logging.getLogger("alive_mcp.tools.search")


# ---------------------------------------------------------------------------
# Frozen constants.
# ---------------------------------------------------------------------------

#: Maximum file size we will open for scanning. Files larger than this
#: are listed in ``structuredContent.skipped`` with reason
#: ``file_too_large`` rather than being silently ignored.
_MAX_FILE_BYTES = 500 * 1024  # 500KB

#: Cap on matched-line text returned in ``content``. Long lines are
#: truncated -- 200 characters is generous enough to show the match
#: plus surrounding context within the line but short enough to keep
#: paginated responses bounded.
_CONTENT_MAX_CHARS = 200

#: Context window size (lines before and after the match).
_CONTEXT_WINDOW = 2

#: Default and cap for the ``limit`` parameter.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

#: Cursor schema version. Bumping this is a breaking change for
#: clients mid-pagination -- v0.1 locks this at 1.
_CURSOR_VERSION = 1

#: Searchable file extensions (lowercase, with leading dot).
_SEARCHABLE_EXTS: frozenset[str] = frozenset({".md", ".yaml", ".yml", ".json"})

#: Directories pruned during bundle discovery. Mirrors the union of
#: :data:`walnut_paths._SKIP_DIRS` and the system/build noise dirs
#: plus ``templates`` + ``raw`` (explicitly called out in the task
#: spec). Duplicating the list here keeps the search module
#: independent of future changes to the vendored set -- a new skip
#: added to the vendor side should also be evaluated for this module.
_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    "target",
    "templates",
    "raw",
})

#: Fixed per-walnut kernel file order. Relpaths from the walnut root.
#: v2 ``_kernel/_generated/now.json`` is NOT searched in v0.1 -- only
#: the v3 ``_kernel/now.json`` contributes to the plan, and when it's
#: absent now.json simply doesn't appear for that walnut. The reader
#: tools accept the v2 location; search does not. See the module
#: docstring's "Explicitly NOT searched" block for the full rationale.
_KERNEL_FILE_ORDER: Tuple[str, ...] = (
    "_kernel/key.md",
    "_kernel/log.md",
    "_kernel/insights.md",
    "_kernel/now.json",
)

#: Reason string for the ``skipped`` list when a file exceeds the cap.
_SKIP_REASON_TOO_LARGE = "file_too_large"


# ---------------------------------------------------------------------------
# Cursor codec.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Cursor:
    """Decoded cursor state.

    ``wi`` -- walnut index (0-based) in the sorted walnut list. For
    :func:`search_walnut` this is always 0 because the walnut list has
    exactly one entry.

    ``fi`` -- file index (0-based) in the active walnut's file plan.

    ``lo`` -- line offset within the active file (0 = start; next
    iteration resumes at line ``lo + 1`` using 1-based line numbers).
    """

    wi: int
    fi: int
    lo: int

    def encode(self) -> str:
        return _encode_cursor(self)


def _encode_cursor(c: _Cursor) -> str:
    """Serialize a cursor to a base64url JSON token.

    The JSON representation uses sorted keys so the encoded token is
    deterministic for a given tuple -- two cursors with the same
    ``(wi, fi, lo)`` always produce the same string. This is what the
    Inspector contract snapshots compare against.
    """
    raw = json.dumps(
        {"v": _CURSOR_VERSION, "wi": c.wi, "fi": c.fi, "lo": c.lo},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(token: Optional[str]) -> _Cursor:
    """Decode a base64url JSON cursor token.

    Returns ``_Cursor(0, 0, 0)`` for ``None`` or empty string (start
    of results). Raises :class:`errors.InvalidCursorError` for any
    malformed input: bad base64, bad JSON, missing keys, wrong version,
    or negative indices.

    Deliberately strict: search pagination is a contract surface and
    a lenient decoder would mask client bugs that would otherwise
    produce duplicate or skipped results across pages.
    """
    if token is None or token == "":
        return _Cursor(wi=0, fi=0, lo=0)
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise errors.InvalidCursorError(
            "cursor decode failed: {}".format(exc.__class__.__name__)
        ) from exc

    if not isinstance(data, dict):
        raise errors.InvalidCursorError("cursor payload is not a JSON object")

    # Schema validation. All four keys must be present; version must
    # match; indices must be non-negative integers.
    version = data.get("v")
    if version != _CURSOR_VERSION:
        raise errors.InvalidCursorError(
            "cursor schema version {!r} unsupported".format(version)
        )
    for key in ("wi", "fi", "lo"):
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise errors.InvalidCursorError(
                "cursor field {!r} must be a non-negative integer".format(key)
            )

    return _Cursor(wi=int(data["wi"]), fi=int(data["fi"]), lo=int(data["lo"]))


# ---------------------------------------------------------------------------
# Query normalization.
# ---------------------------------------------------------------------------


def _normalize_query(query: str, case_sensitive: bool) -> str:
    """Canonicalize a query string for substring comparison.

    NFKC-folds so compatibility-equivalent codepoints compare equal.
    NFKC is strictly stronger than NFC: NFC only canonicalizes (e.g.
    ``e`` + combining acute -> composed ``\u00e9``); NFKC additionally
    collapses compatibility equivalents (ligature ``\ufb00`` -> ``ff``,
    full-width Latin ``\uff21`` -> ``A``, Roman-numeral ``\u2160`` ->
    ``I``, etc.). This matches the spec's "NFKC-equivalent strings"
    acceptance criterion.

    When ``case_sensitive`` is ``False``, applies ``str.casefold`` AFTER
    NFKC-folding. This is the conventional order for case-insensitive
    unicode substring matching: NFKC gives us canonical form, then
    casefold lowercases using the locale-independent case-fold table.
    """
    normalized = unicodedata.normalize("NFKC", query)
    if case_sensitive:
        return normalized
    return normalized.casefold()


def _normalize_line(line: str, case_sensitive: bool) -> str:
    """Canonicalize a scanned line the same way as the query.

    Same NFKC + optional casefold transform as
    :func:`_normalize_query` so the two strings are comparable.
    Isolated into its own helper because the hot path (one call per
    scanned line) can be micro-optimized later without touching
    callers.
    """
    normalized = unicodedata.normalize("NFKC", line)
    if case_sensitive:
        return normalized
    return normalized.casefold()


# ---------------------------------------------------------------------------
# File plan assembly (fixed order per walnut).
# ---------------------------------------------------------------------------


def _bundle_manifest_plan(walnut_abs: str) -> List[str]:
    """Return bundle manifest relpaths under ``walnut_abs``, sorted.

    Local discovery walk using ONLY the spec-frozen :data:`_SKIP_DIRS`
    set + dotfile pruning + nested-walnut boundary detection. The
    vendored :func:`walnut_paths.find_bundles` has its own skip list
    (``_kernel``, ``_core``, ``01_Archive``, ``_archive``,
    ``_references``, etc.) that is both broader AND narrower than the
    search spec's set -- ``templates`` isn't in the vendor set, and
    ``01_Archive``/``_archive`` are in the vendor set but not the
    search spec. Inheriting vendor pruning would violate the "frozen
    inclusion rules" requirement, so we walk the tree ourselves.

    Discovery rules (matches the task spec):

    * A directory is a bundle iff it contains
      ``context.manifest.yaml``. ``companion.md`` (v1 legacy) is NOT
      a bundle for search purposes -- search is v3/v2 only.
    * Skip ``_kernel`` (the walnut's own identity dir) and any
      directory in :data:`_SKIP_DIRS` or starting with ``.``.
    * Stop descending at any nested walnut boundary
      (``<dir>/_kernel/key.md`` exists): nested walnuts are their own
      inventory and shouldn't bleed into the parent's bundle list.
    * Return POSIX-relative paths from ``walnut_abs``, sorted
      ascending.

    The walnut root itself is never a bundle, even if a stray
    manifest sits there (matches the vendored posture at
    ``walnut_paths.find_bundles``).
    """
    walnut_abs = os.path.abspath(walnut_abs)
    kept: List[str] = []
    nested_walnut_roots: set[str] = set()

    try:
        walker = os.walk(walnut_abs, followlinks=False)
    except OSError as exc:
        logger.warning(
            "bundle discovery failed for %r: %s", walnut_abs, exc
        )
        return []

    # Need the kernel dir name constant at the top of the walk.
    _KERNEL = "_kernel"
    try:
        for root, dirs, files in walker:
            rel = os.path.relpath(root, walnut_abs)

            # Prune hidden + skip dirs in-place (load-bearing: os.walk
            # only honors mutations on this list reference). Skip
            # ``_kernel`` explicitly so the walnut's identity dir is
            # never part of the bundle plan.
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d != _KERNEL
                and d not in _SKIP_DIRS
            ]

            # If we're below a nested walnut boundary, stop descending.
            if rel != ".":
                rel_posix = rel.replace(os.sep, "/")
                inside_nested = False
                for nested in nested_walnut_roots:
                    if rel_posix == nested or rel_posix.startswith(
                        nested + "/"
                    ):
                        inside_nested = True
                        break
                if inside_nested:
                    dirs[:] = []
                    continue

                # Detect a fresh nested-walnut boundary: a non-root
                # directory containing _kernel/key.md. Mark and stop
                # descending; its interior is a separate walnut's
                # inventory.
                if os.path.isfile(os.path.join(root, _KERNEL, "key.md")):
                    nested_walnut_roots.add(rel_posix)
                    dirs[:] = []
                    continue

            # Bundle detection. v2/v3 layout only -- v1 ``companion.md``
            # is NOT surfaced by search. The walnut root itself is
            # skipped even if it carries a stray manifest (matches the
            # vendor's posture + keeps search output clean).
            if rel == ".":
                continue
            if "context.manifest.yaml" in files:
                kept.append(rel.replace(os.sep, "/"))
    except OSError as exc:
        # Partial walk is acceptable -- log and return what we have.
        # Without this, a permission failure deep in the tree would
        # take out the entire bundle plan.
        logger.warning(
            "bundle discovery walk interrupted for %r: %s",
            walnut_abs, exc,
        )

    kept.sort()
    return ["{}/context.manifest.yaml".format(rel) for rel in kept]


def _walnut_file_plan(world_root: str, walnut_abs: str) -> List[str]:
    """Return the ordered list of searchable relpaths for one walnut.

    Relpaths are POSIX-relative to the walnut root. Order:

    1. ``_kernel/key.md``
    2. ``_kernel/log.md``
    3. ``_kernel/insights.md``
    4. ``_kernel/now.json``
    5. Each bundle's ``<bundle_relpath>/context.manifest.yaml``,
       bundles sorted alphabetically by relpath.

    The frozen spec inclusion list is exactly those four kernel files
    plus the top-level bundle manifests. v2 ``_kernel/_generated/
    now.json`` is NOT searched even when present -- that's reserved
    for v0.2. Reader tools (``get_walnut_state``,
    ``read_walnut_kernel``) accept the v2 location because they
    identify a single file by name; search is a content surface
    where the inclusion rule is frozen.

    Files that don't exist on disk are dropped at the assembly stage
    so pagination indices refer to files that are candidates for
    reading. Missing files don't become "skipped" entries (skipped is
    reserved for files we found but couldn't scan).

    Each relpath is containment-validated before returning: a
    symlinked file whose realpath escapes the World is dropped at
    this stage so the scanner never opens it.
    """
    plan: List[str] = []
    for rel in _KERNEL_FILE_ORDER:
        abs_path = os.path.join(walnut_abs, rel)
        if not os.path.isfile(abs_path):
            continue
        if not is_inside(world_root, abs_path):
            logger.warning(
                "kernel file at %r escapes World via symlink; dropping",
                rel,
            )
            continue
        plan.append(rel)

    for rel in _bundle_manifest_plan(walnut_abs):
        abs_path = os.path.join(walnut_abs, rel)
        if not os.path.isfile(abs_path):
            continue
        if not is_inside(world_root, abs_path):
            logger.warning(
                "bundle manifest at %r escapes World via symlink; dropping",
                rel,
            )
            continue
        plan.append(rel)

    return plan


# ---------------------------------------------------------------------------
# Per-file scanning.
# ---------------------------------------------------------------------------


def _read_lines_or_none(path: str) -> Optional[List[str]]:
    """Read ``path`` as UTF-8 text and return its lines (without EOL).

    Returns ``None`` on any I/O error (permission denied, missing,
    decode failure). The caller treats ``None`` as "drop this file
    silently" -- search is best-effort, and surfacing per-file read
    errors would dominate the response on realistic worlds where
    some kernel files are unreadable by the server user.

    File size is checked OUTSIDE this helper (the caller stats the
    file before calling here so the skipped-list entry is emitted
    before the read attempt). Keeping the size check separate means
    :func:`_read_lines_or_none` has one job: read or signal read
    failure.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (IOError, OSError, UnicodeDecodeError) as exc:
        logger.debug("search read failed for %r: %s", path, exc)
        return None
    # ``splitlines`` drops trailing empty strings; that's fine for
    # display. Line numbers start at 1 for the first element.
    return text.splitlines()


def _truncate_content(line: str) -> str:
    """Cap ``line`` at :data:`_CONTENT_MAX_CHARS` characters.

    Python string slicing is codepoint-aware -- we slice by
    codepoints, not bytes, so a multi-byte UTF-8 character never gets
    split. No ellipsis is appended: the protocol consumer knows the
    cap from the tool description and can request more context via
    reader tools if needed.
    """
    if len(line) <= _CONTENT_MAX_CHARS:
        return line
    return line[:_CONTENT_MAX_CHARS]


def _context_slice(
    lines: List[str],
    index: int,
) -> Tuple[List[str], List[str]]:
    """Return ``(context_before, context_after)`` around ``lines[index]``.

    Each side is at most :data:`_CONTEXT_WINDOW` entries. Near file
    edges the arrays shrink (they are NOT padded) so the receiver can
    distinguish "no context available" from "context was empty text".

    Each context line is also capped at :data:`_CONTENT_MAX_CHARS` so
    a single long adjacent line can't blow up the response size.
    """
    start = max(0, index - _CONTEXT_WINDOW)
    end = min(len(lines), index + 1 + _CONTEXT_WINDOW)
    before = [_truncate_content(lines[i]) for i in range(start, index)]
    after = [_truncate_content(lines[i]) for i in range(index + 1, end)]
    return before, after


def _iter_matches_in_file(
    lines: List[str],
    normalized_query: str,
    case_sensitive: bool,
    start_line: int,
) -> Iterable[Tuple[int, str, List[str], List[str]]]:
    """Yield ``(line_number, content, before, after)`` for each match.

    ``start_line`` is 1-based and INCLUSIVE: the first line considered
    is ``lines[start_line - 1]``. ``line_number`` in the yielded
    tuple is 1-based.

    Matches are emitted in line-number ascending order -- scanning is
    linear so this is automatic.
    """
    # Guard the start index. ``start_line`` of 0 (first call) becomes
    # index 0; values beyond len(lines) yield nothing.
    start_idx = max(0, start_line - 1)
    for i in range(start_idx, len(lines)):
        line = lines[i]
        haystack = _normalize_line(line, case_sensitive)
        if normalized_query in haystack:
            before, after = _context_slice(lines, i)
            yield (i + 1, _truncate_content(line), before, after)


# ---------------------------------------------------------------------------
# Pagination engine.
# ---------------------------------------------------------------------------


@dataclass
class _SearchState:
    """Accumulator threaded through the search loop.

    Kept as a dataclass (not a frozen record) so the loop can update
    the counters in place without repeated allocation. Lifetime is
    bounded to a single tool call.
    """

    matches: List[Dict[str, Any]]
    skipped: List[Dict[str, str]]
    next_cursor: Optional[_Cursor]

    def has_room(self, limit: int) -> bool:
        return len(self.matches) < limit


def _cursor_in_range(
    cursor: _Cursor,
    world_root: str,
    walnut_inventory: List[Tuple[str, str]],
) -> bool:
    """Return True iff ``cursor`` points at a valid position in the inventory.

    A cursor is in-range when:

    * ``wi`` is either 0 (origin) or a valid index into
      ``walnut_inventory`` (``0 <= wi < len(walnut_inventory)``), AND
    * ``fi`` is either 0 or a valid index into the active walnut's
      file plan.

    The file-plan check is only performed for the single referenced
    walnut (``walnut_inventory[cursor.wi]``) so we don't pay bundle-
    discovery cost for every walnut just to validate a cursor.

    ``lo`` itself is NOT range-checked here -- lines within a file
    are free to rebind (edits to a kernel file between calls can
    legitimately change line numbering) and the scanner tolerates a
    too-high ``lo`` by yielding zero matches from that file. ``wi``
    and ``fi``, in contrast, reference a sequence shape that is
    trivially observable at call time, so we reject obviously-stale
    cursors that couldn't possibly produce correct results.

    Two origin cases pass transparently:

    * ``_Cursor(0, 0, 0)`` (fresh call, no cursor) -- passes even
      when ``walnut_inventory`` is empty because ``wi=0`` is the
      terminating condition (the scanner returns immediately).
    * ``_Cursor(wi, 0, 0)`` with ``wi == len(walnut_inventory)`` -- a
      cursor that was correct when written but has since "advanced
      past the end" as walnuts were removed. Treat as invalid so
      the caller restarts cleanly instead of seeing an empty page.

    Called right after cursor decode + inventory assembly in both
    tools. Returning False maps to :data:`errors.ERR_INVALID_CURSOR`.
    """
    # Origin cursor always valid -- empty World just yields empty
    # results without erroring on the cursor shape.
    if cursor.wi == 0 and cursor.fi == 0 and cursor.lo == 0:
        return True

    if cursor.wi >= len(walnut_inventory):
        return False

    # ``fi`` is scoped to the active walnut's file plan. We only
    # compute the plan for the single referenced walnut -- paying
    # bundle-discovery cost for all walnuts just to validate a
    # cursor would defeat the lazy-plan optimization elsewhere.
    # A zero ``fi`` with an empty file plan is valid (scanner
    # advances walnut immediately); a non-zero ``fi`` beyond the
    # plan is not.
    if cursor.fi > 0:
        _, walnut_abs = walnut_inventory[cursor.wi]
        file_plan = _walnut_file_plan(world_root, walnut_abs)
        if cursor.fi >= len(file_plan):
            return False
    return True


def _resolve_limit(limit: int) -> int:
    """Clamp ``limit`` into ``[1, _MAX_LIMIT]``.

    Out-of-bounds values are clamped rather than rejected -- matches
    the T6 ``list_walnuts`` posture. Negative / zero values fall back
    to :data:`_DEFAULT_LIMIT` (the helpful default) rather than
    clamping to 1, which would be a confusing signal for "I want the
    normal page size".
    """
    if limit <= 0:
        return _DEFAULT_LIMIT
    if limit > _MAX_LIMIT:
        return _MAX_LIMIT
    return limit


def _run_search(
    world_root: str,
    walnut_inventory: List[Tuple[str, str]],
    normalized_query: str,
    case_sensitive: bool,
    limit: int,
    cursor: _Cursor,
    prefix_skipped_with_walnut: bool,
) -> _SearchState:
    """Execute the paginated search with lazy per-walnut file plans.

    ``walnut_inventory`` is ``[(walnut_relpath, walnut_abs), ...]``
    -- just the walnut identity pairs. File plans are computed
    lazily when a walnut is actually visited. This matters: the
    acceptance target is <3s for a world-wide search, and eager
    plan assembly would pay bundle-discovery cost for every walnut
    even when the first page of results comes from the first file
    in the first walnut. A paginated ``limit=20`` call on a 43-walnut
    World should do work proportional to "files scanned until
    limit reached", not "every bundle discovered in every walnut".

    Cursor semantics:

    * ``cursor.wi`` -- index into ``walnut_inventory``.
    * ``cursor.fi`` -- index into the active walnut's file plan,
      computed on-demand when that walnut is visited.
    * ``cursor.lo`` -- line offset within the current file.

    ``prefix_skipped_with_walnut`` controls the anchoring of entries
    in ``structuredContent.skipped``:

    * ``True`` (``search_world``) -- skipped paths include the
      walnut relpath prefix (e.g. ``04_Ventures/alive/_kernel/log.md``).
      Callers can disambiguate which walnut each skipped file
      belongs to without cross-referencing ``matches``.
    * ``False`` (``search_walnut``) -- skipped paths are walnut-
      relative (e.g. ``_kernel/log.md``). The caller already supplied
      the walnut scope so re-prefixing would be redundant.

    Match records always carry an explicit ``walnut`` field, so the
    anchoring toggle only affects ``skipped``.

    Returns a :class:`_SearchState` with ``matches`` (at most
    ``limit``), ``skipped`` (files exceeding the size cap), and
    ``next_cursor`` (None when the search is exhausted).
    """
    state = _SearchState(matches=[], skipped=[], next_cursor=None)

    wi = cursor.wi
    while wi < len(walnut_inventory):
        walnut_rel, walnut_abs = walnut_inventory[wi]
        # Lazy: only build the file plan now that we've reached this
        # walnut. The plan assembly triggers a bundle-discovery walk
        # for this walnut only, keeping world-wide search cost
        # proportional to walnuts actually traversed.
        file_plan = _walnut_file_plan(world_root, walnut_abs)
        # File index: on the FIRST walnut we may resume mid-walnut;
        # on every subsequent walnut we start at 0.
        fi = cursor.fi if wi == cursor.wi else 0
        while fi < len(file_plan):
            file_rel = file_plan[fi]
            abs_path = os.path.join(walnut_abs, file_rel)

            # Size check first so skipped entries are emitted even
            # when the size cap would cause a read failure below.
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                # File vanished between plan assembly and scan.
                # Treat as missing -> drop silently, advance the
                # cursor. Skipping the file keeps pagination
                # consistent (next page doesn't re-try this file).
                fi += 1
                continue

            if size > _MAX_FILE_BYTES:
                # Emit a skipped entry so clients see why the file
                # wasn't searched. Anchoring depends on the tool:
                # search_world prefixes the walnut relpath so
                # entries are globally unambiguous; search_walnut
                # leaves the path walnut-relative (the caller
                # already scoped the tool to that walnut).
                if prefix_skipped_with_walnut and walnut_rel:
                    skipped_path = "{}/{}".format(walnut_rel, file_rel)
                else:
                    skipped_path = file_rel
                state.skipped.append(
                    {"path": skipped_path, "reason": _SKIP_REASON_TOO_LARGE}
                )
                fi += 1
                continue

            lines = _read_lines_or_none(abs_path)
            if lines is None:
                # Unreadable / decode failure -- drop silently.
                # Advance the cursor so the next page doesn't retry.
                fi += 1
                continue

            # Line offset: on the FIRST file of the FIRST walnut we
            # resume at cursor.lo; every subsequent file starts at
            # line 0 (1-based 1).
            line_offset = cursor.lo if (wi == cursor.wi and fi == cursor.fi) else 0
            # Convert to 1-based "start at line N+1": line_offset of
            # 0 means "start at line 1".
            start_line = line_offset + 1

            match_iter = _iter_matches_in_file(
                lines, normalized_query, case_sensitive, start_line
            )
            for line_number, content, before, after in match_iter:
                if not state.has_room(limit):
                    # Pagination boundary mid-file: we have NOT yet
                    # appended ``line_number`` -- it's the first
                    # match beyond the budget. The cursor must make
                    # the next call resume AT ``line_number`` so the
                    # match isn't skipped. We set ``lo = line_number
                    # - 1``; the scanner computes ``start_line = lo
                    # + 1`` which then equals ``line_number``.
                    state.next_cursor = _Cursor(
                        wi=wi, fi=fi, lo=line_number - 1
                    )
                    return state
                state.matches.append(
                    {
                        "walnut": walnut_rel,
                        "file": file_rel,
                        "line_number": line_number,
                        "content": content,
                        "context_before": before,
                        "context_after": after,
                    }
                )

            fi += 1

        wi += 1

    # Exhausted all walnuts + files without hitting the limit.
    state.next_cursor = None
    return state


# ---------------------------------------------------------------------------
# App-context accessor (mirrors the walnut/bundle tool modules).
# ---------------------------------------------------------------------------


def _get_world_root(ctx: Context) -> Optional[str]:
    """Return the resolved World root or None.

    Duplicated from the walnut + bundle tool modules so the search
    module doesn't reach into a sibling's privates. Trivial read of
    ``ctx.request_context.lifespan_context.world_root``.
    """
    lifespan = getattr(ctx.request_context, "lifespan_context", None)
    if lifespan is None:
        return None
    return getattr(lifespan, "world_root", None)


# ---------------------------------------------------------------------------
# Tools.
# ---------------------------------------------------------------------------


@audited
async def search_world(
    ctx: Context,
    query: str,
    limit: int = _DEFAULT_LIMIT,
    cursor: Optional[str] = None,
    case_sensitive: bool = False,
) -> Dict[str, Any]:
    """Search every searchable file in every walnut of the current World.

    Parameters
    ----------
    query:
        Literal substring to match. Empty and whitespace-only queries
        short-circuit to ``matches=[]`` with ``next_cursor=None`` --
        the naive ``"" in line`` check would match every scanned line
        and flood callers with noise, so we reject up-front rather
        than paginate through the whole World. Both the query and
        every scanned line are NFKC-folded before comparison
        (compatibility-composition form -- stronger than NFC,
        collapses ligatures / full-width / compatibility duplicates).
    limit:
        Maximum matches per response. Default 20, cap 100; out-of-bounds
        values are clamped.
    cursor:
        Opaque base64url pagination token returned by a prior call.
        ``None`` or empty string starts at the first walnut's first
        file, line 1. Malformed cursors, cursors with an unsupported
        schema version, or cursors pointing past the current
        walnut/file inventory all return ``ERR_INVALID_CURSOR``.
    case_sensitive:
        Default ``False``. When ``False``, ``str.casefold`` is applied
        to both the query and every scanned line after NFKC folding.

    Returns
    -------
    dict
        Envelope whose ``structuredContent`` is ``{matches, next_cursor,
        skipped}``. ``matches`` is a list of match records (see module
        docstring); ``next_cursor`` is ``None`` when the search is
        exhausted; ``skipped`` is a list of ``{path, reason}`` entries
        for files the scanner couldn't process (currently only
        ``file_too_large``).
    """
    world_root = _get_world_root(ctx)
    if world_root is None:
        return envelope.error(errors.ERR_NO_WORLD)

    try:
        decoded_cursor = _decode_cursor(cursor)
    except errors.InvalidCursorError:
        return envelope.error(errors.ERR_INVALID_CURSOR)

    # Empty / whitespace-only query guard. The naive ``"" in line``
    # check would match every scanned line and flood the caller
    # with noise. We reject up-front rather than paginating through
    # the whole World for a zero-information query.
    if not query or not query.strip():
        return envelope.ok(
            {"matches": [], "next_cursor": None, "skipped": []}
        )

    resolved_limit = _resolve_limit(limit)
    normalized_query = _normalize_query(query, case_sensitive)

    # Walnut inventory. ``_iter_walnut_paths`` returns POSIX-sorted
    # relpaths; the search spec sorts by REALPATH (tie-broken by
    # relpath for determinism). We re-sort here so a walnut whose
    # realpath differs from its lexical path (symlinked ALIVE
    # domains, iCloud mirrors, monorepo worktrees) lands in the
    # canonical spec position.
    try:
        walnut_relpaths = _iter_walnut_paths(world_root)
    except (PermissionError, OSError) as exc:
        logger.warning("search_world walnut inventory failed: %s", exc)
        return envelope.error(
            errors.ERR_PERMISSION_DENIED,
            walnut="(world inventory)",
            file="list",
        )

    # Build the walnut inventory: just identity pairs (relpath, abs).
    # File plans are built lazily inside ``_run_search`` only for
    # walnuts actually visited. At 43 walnuts this matters -- eager
    # plan assembly would trigger a bundle-discovery walk per walnut
    # regardless of whether the limit was satisfied early.
    #
    # Sort key: (realpath, relpath). Realpath is primary (spec);
    # relpath tie-break guarantees fully deterministic ordering even
    # when two walnuts happen to share a realpath.
    walnut_relpaths = sorted(
        walnut_relpaths,
        key=lambda rel: (
            os.path.realpath(os.path.join(world_root, rel)),
            rel,
        ),
    )
    walnut_inventory: List[Tuple[str, str]] = [
        (rel, os.path.join(world_root, rel)) for rel in walnut_relpaths
    ]

    # Cursor range validation. A structurally valid cursor can still
    # point outside the current inventory (World mutated between
    # calls -- walnut removed, file deleted). Catching that here
    # surfaces the stale cursor as ``ERR_INVALID_CURSOR`` instead of
    # silently returning an empty page that looks like "search
    # exhausted" to the caller. The range check is scoped to the
    # referenced walnut only (doesn't pay bundle-discovery cost for
    # every walnut in the inventory).
    if not _cursor_in_range(decoded_cursor, world_root, walnut_inventory):
        return envelope.error(errors.ERR_INVALID_CURSOR)

    state = _run_search(
        world_root,
        walnut_inventory,
        normalized_query,
        case_sensitive,
        resolved_limit,
        decoded_cursor,
        prefix_skipped_with_walnut=True,
    )

    return envelope.ok(
        {
            "matches": state.matches,
            "next_cursor": state.next_cursor.encode() if state.next_cursor else None,
            "skipped": state.skipped,
        }
    )


@audited
async def search_walnut(
    ctx: Context,
    walnut: str,
    query: str,
    limit: int = _DEFAULT_LIMIT,
    cursor: Optional[str] = None,
    case_sensitive: bool = False,
) -> Dict[str, Any]:
    """Search every searchable file in a single walnut.

    Same parameters as :func:`search_world` plus ``walnut`` (POSIX
    relpath from the World root). Same response shape. The ``walnut``
    field on every match record equals the input ``walnut`` value --
    callers can differentiate in mixed UIs if they aggregate results
    from multiple scoped calls.

    Empty / whitespace-only queries short-circuit the same way as
    :func:`search_world`. NFKC + optional casefold normalization,
    same 500KB file cap, same cursor codec -- the two tools share
    the scanner and only differ in scope and skipped-path anchoring.

    Error paths:

    * ``ERR_NO_WORLD`` -- no World resolved yet.
    * ``ERR_PATH_ESCAPE`` -- input escapes the World via ``..`` /
      absolute / symlink.
    * ``ERR_WALNUT_NOT_FOUND`` -- input path does not resolve to a
      walnut (no ``_kernel/key.md``). Includes fuzzy-match suggestions.
    * ``ERR_INVALID_CURSOR`` -- cursor fails decode / validation, or
      points past the current file plan (e.g. bundle removed between
      calls).
    """
    world_root = _get_world_root(ctx)
    if world_root is None:
        return envelope.error(errors.ERR_NO_WORLD)

    try:
        walnut_abs = _resolve_walnut(world_root, walnut)
    except errors.PathEscapeError:
        return envelope.error(errors.ERR_PATH_ESCAPE)
    except errors.WalnutNotFoundError:
        return _walnut_not_found_envelope(world_root, walnut)

    try:
        decoded_cursor = _decode_cursor(cursor)
    except errors.InvalidCursorError:
        return envelope.error(errors.ERR_INVALID_CURSOR)

    # Empty / whitespace-only query guard (matches search_world).
    if not query or not query.strip():
        return envelope.ok(
            {"matches": [], "next_cursor": None, "skipped": []}
        )

    resolved_limit = _resolve_limit(limit)
    normalized_query = _normalize_query(query, case_sensitive)

    # Normalize the walnut identifier back to POSIX so match records
    # echo the same shape the caller supplied. ``_resolve_walnut``
    # accepts both POSIX and OS-native separators.
    walnut_posix = walnut.replace("\\", "/").strip("/")

    # Single-walnut inventory -- ``_run_search`` builds the file plan
    # lazily so we don't pay bundle-discovery cost if the cursor
    # validates as out-of-range or the query short-circuits.
    walnut_inventory = [(walnut_posix, walnut_abs)]

    # Cursor range validation -- same posture as search_world.
    # Detects stale cursors pointing past the current file plan
    # (e.g. a bundle removed between calls) and surfaces them as
    # ERR_INVALID_CURSOR so the caller restarts cleanly.
    if not _cursor_in_range(decoded_cursor, world_root, walnut_inventory):
        return envelope.error(errors.ERR_INVALID_CURSOR)

    state = _run_search(
        world_root,
        walnut_inventory,
        normalized_query,
        case_sensitive,
        resolved_limit,
        decoded_cursor,
        prefix_skipped_with_walnut=False,
    )

    return envelope.ok(
        {
            "matches": state.matches,
            "next_cursor": state.next_cursor.encode() if state.next_cursor else None,
            "skipped": state.skipped,
        }
    )


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


_SEARCH_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=False,
    idempotentHint=True,
)


def register(server: FastMCP[Any]) -> None:
    """Register ``search_world`` and ``search_walnut`` on ``server``.

    Called by :func:`alive_mcp.server.build_server` alongside the
    walnut and bundle tool registrations. Annotations are identical
    to the other read-only tools in v0.1.
    """
    server.tool(
        name="search_world",
        description=(
            "Substring-search every searchable file across every walnut "
            "in the active ALIVE World. Returns "
            "{matches: [{walnut, file, line_number, content, "
            "context_before, context_after}, ...], next_cursor, skipped}. "
            "Case-insensitive by default. Cursor-paginate when "
            "'next_cursor' is non-null. Files >500KB are listed in "
            "'skipped' with reason 'file_too_large'."
        ),
        annotations=_SEARCH_TOOL_ANNOTATIONS,
    )(search_world)

    server.tool(
        name="search_walnut",
        description=(
            "Substring-search every searchable file in a single walnut. "
            "Returns the same shape as search_world. Use 'walnut' as a "
            "POSIX-relative path from the World root (e.g. "
            "'04_Ventures/alive'). Cursor-paginate when 'next_cursor' "
            "is non-null."
        ),
        annotations=_SEARCH_TOOL_ANNOTATIONS,
    )(search_walnut)


__all__ = [
    "search_world",
    "search_walnut",
    "register",
]
