"""Walnut-centric read tools (fn-10-60k.6 / T6).

Three tools, all read-only, all annotated
``ToolAnnotations(readOnlyHint=True, destructiveHint=False,
openWorldHint=False)``:

* :func:`list_walnuts` -- inventory the current World as
  ``{walnuts: [{path, name, domain, goal, health, updated}, ...],
  next_cursor}`` with opaque base64-encoded cursor pagination.
* :func:`get_walnut_state` -- read the ``_kernel/now.json`` projection
  for a walnut identified by POSIX-relpath, with v3 -> v2 fallback. Does
  NOT trigger on-demand assembly (that's v0.2 territory).
* :func:`read_walnut_kernel` -- read a whole kernel file
  (``key | log | insights | now``) and return ``{content, mime}``. No
  pagination -- T9's ``read_log`` owns chunking the log.

Frozen contract notes (from the epic spec, reproduced here so the file is
self-contained for reviewers):

* ``walnut`` param is ALWAYS a POSIX relative path from the World root
  (e.g. ``02_Life/people/ben-flint``). Not a bare name -- names collide
  across domains (there is a ``ben-flint`` under ``People/``, under
  ``02_Life/people/``, and potentially a past archive entry). ``path``
  is the canonical identifier callers echo back.
* ``domain`` is the first path segment. ``02_Life/people/ben-flint`` ->
  ``02_Life``. Non-ALIVE-prefixed paths (worlds without domain
  numbering) return ``domain = None``.
* ``health`` follows the Hermes algorithm documented at
  ``hermes/memory-provider/__init__.py::_find_walnuts``:
  ``days_since <= rhythm_days`` -> ``active``;
  ``<= rhythm_days * 2`` -> ``quiet``; else ``waiting``;
  ``unknown`` when ``rhythm`` or ``updated`` is missing.
* All three tools are wrapped with :func:`audited` so T12 can layer the
  audit writer on without touching tool code.
* Every error path returns an ``envelope.error`` envelope. Tools never
  raise out to FastMCP -- the caretaker contract forbids plain
  exceptions on the tool surface.

Walnut discovery heuristic
--------------------------
A directory is a walnut iff it contains ``_kernel/key.md``. We use
``Path.rglob("_kernel/key.md")`` under the resolved World root. This
matches Hermes' validated behavior at 43 walnuts in <1s on Patrick's
machine (the acceptance target). Rglob descends into hidden/dotfile
directories by default; we don't filter those out at the top level
because a World's ``.alive/`` doesn't contain walnuts and rglob natively
skips it when there's no ``_kernel/key.md`` inside (there isn't, so the
cost is cheap directory traversal, not a correctness hazard).

The domain sentinel list is consulted only for the ``domain`` field on
each walnut record, NOT as a filter -- Worlds that don't follow the
numbering convention still list their walnuts, just with
``domain = None``.

Cursor pagination
-----------------
Cursors are opaque tokens: base64url(``str(offset)``). Stateless (the
full walnut list is recomputed each call -- acceptable at 43-walnut
scale; T8 re-evaluates for search tools). Invalid cursors return
``ERR_INVALID_CURSOR``. Cursors do not survive server restart because
inventory ordering is a function of the filesystem at call time;
callers get a diagnostic via the suggestions field when they retry with
a stale cursor.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import get_close_matches
from typing import Any, List, Literal, Optional, Tuple

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from alive_mcp import envelope, errors
from alive_mcp._vendor._pure import project_pure
from alive_mcp.paths import is_inside, resolve_under, safe_join
from alive_mcp.tools._audit_stub import audited

logger = logging.getLogger("alive_mcp.tools.walnut")


# ---------------------------------------------------------------------------
# Constants + type aliases.
# ---------------------------------------------------------------------------

#: Domain sentinels -- the first path segment of a walnut path that
#: denotes an ALIVE-numbered domain. Used ONLY for the ``domain`` field
#: on walnut records; non-prefixed paths yield ``domain = None``.
_DOMAIN_PREFIXES: Tuple[str, ...] = (
    "01_Archive",
    "02_Life",
    "04_Ventures",
    "05_Experiments",
)

#: Max items returned by a single ``list_walnuts`` call. The default is
#: 50 per the task spec; clients can request smaller. Exceeding this cap
#: triggers ``ERR_VALIDATION``-style behavior at the schema boundary
#: (FastMCP converts int bounds violations into JSON-RPC InvalidParams
#: which is the MCP equivalent of validation failure).
LIST_WALNUTS_LIMIT_CAP = 500

#: Rhythm label -> day budget. Mirrors the Hermes table (see
#: ``hermes/memory-provider/__init__.py`` line 149 at pin). Unknown
#: rhythm labels fall through to ``weekly`` (the documented default).
_RHYTHM_DAYS = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
}

#: Whitelist of kernel file stems accepted by ``read_walnut_kernel``. The
#: schema-enforced literal keeps the set of possible values minimal so a
#: malformed ``file`` argument turns into a validation error at the
#: boundary rather than a mystery I/O failure.
KernelFile = Literal["key", "log", "insights", "now"]

#: File-stem -> on-disk basename. Separated so callers that pass the
#: stem (``log``) never have to know the file-name convention
#: (``log.md``). ``now`` stays without extension because it's paired with
#: the ``.json`` layout and the v3 / v2 fallback logic below.
_FILE_MAP = {
    "key": "key.md",
    "log": "log.md",
    "insights": "insights.md",
    # Sentinel -- ``now`` goes through _resolve_now_path, not this map.
    "now": None,
}

#: Mime types returned on success. Markdown for the three prose files,
#: JSON for now.json. Callers display ``content`` verbatim; ``mime`` is
#: the hint for rendering vs parsing.
_MIME_MAP = {
    "key": "text/markdown",
    "log": "text/markdown",
    "insights": "text/markdown",
    "now": "application/json",
}

#: Cached sentinel for ``_kernel`` subdirectory -- the one location a
#: walnut must have to be considered a walnut.
_KERNEL_DIRNAME = "_kernel"

#: Frontmatter regex. Matches a YAML block at the start of a file
#: bounded by ``---`` fences. Non-greedy body so the first closing fence
#: wins.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# ---------------------------------------------------------------------------
# Cursor codec.
# ---------------------------------------------------------------------------


def _encode_cursor(offset: int) -> str:
    """Base64url-encode an integer offset.

    The scheme is deliberately simple -- there is no HMAC, no version
    tag, no JSON envelope. Cursors are hints for pagination, not
    security tokens. Simple encoding keeps the surface area tight and
    makes invalid-cursor detection unambiguous (anything that doesn't
    base64-decode to an ASCII integer is invalid).
    """
    raw = str(offset).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(token: Optional[str]) -> int:
    """Decode a cursor, returning the integer offset. ``None`` yields 0.

    Raises :class:`errors.InvalidCursorError` if the token is malformed
    (not base64, decoded bytes are not an integer, integer is negative).
    Empty-string tokens are treated as ``None`` because some clients
    normalize missing fields to empty strings.
    """
    if token is None or token == "":
        return 0
    try:
        # urlsafe_b64decode requires correct padding; we stripped ``=``
        # during encoding, so re-pad here before decoding.
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = int(raw.decode("ascii"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise errors.InvalidCursorError(
            "cursor failed to decode: {}".format(exc.__class__.__name__)
        ) from exc
    if value < 0:
        raise errors.InvalidCursorError(
            "cursor offset is negative: {}".format(value)
        )
    return value


# ---------------------------------------------------------------------------
# Walnut record synthesis.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _WalnutRecord:
    """Per-walnut projection returned by ``list_walnuts``.

    Frozen + slotted: the records are assembled once and not mutated
    (matches the read-only tool posture) and the tight memory footprint
    keeps 43-walnut inventory trivial.
    """

    path: str
    name: str
    domain: Optional[str]
    goal: str
    health: str
    updated: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "domain": self.domain,
            "goal": self.goal,
            "health": self.health,
            "updated": self.updated,
        }


def _parse_frontmatter_fields(text: str, fields: Tuple[str, ...]) -> dict[str, str]:
    """Extract the named fields from a markdown file's YAML frontmatter.

    Stdlib-only parse; we read only the frontmatter block and scan
    line-by-line for ``key: value``. Values can be quoted; the quotes
    are stripped. Keys not present in the block are absent from the
    result. Missing frontmatter returns an empty dict.

    This is NOT a YAML parser -- it handles only the flat scalar
    fields we care about (``goal``, ``rhythm``, ``type``, etc.). Nested
    structures, lists, anchors etc. pass through untouched (their
    values aren't returned as part of ``fields``).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    wanted = set(fields)
    result: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key not in wanted:
            continue
        value = value.strip()
        # Strip matching quote pairs; leave mismatched quotes intact so
        # weird inputs don't silently alter.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def _read_text_or_empty(path: str, limit_bytes: Optional[int] = None) -> str:
    """Read a UTF-8 text file or return empty string on any I/O error.

    Used for best-effort frontmatter scans where a read failure is
    survivable (we just emit an empty ``goal``). ``limit_bytes`` caps
    how much of the file we read -- frontmatter is always at the top,
    so a few KB is plenty and shields us from pathological multi-MB
    kernel files.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            if limit_bytes is not None:
                return f.read(limit_bytes)
            return f.read()
    except (IOError, OSError, UnicodeDecodeError):
        return ""


def _kernel_file_in_world(
    world_root: str, walnut_abs: str, filename: str
) -> Optional[str]:
    """Resolve ``<walnut>/_kernel/<filename>`` and verify in-world containment.

    Returns the realpath when the file exists AND resolves inside the
    World root after symlink resolution. Returns ``None`` in every
    other case: the file is missing, the realpath escapes the World
    (symlink to ``/etc/passwd`` or similar), or an OS error prevents
    the stat.

    This is the single point of truth for "is this kernel file safe
    to open?" Every caller that touches ``_kernel/*`` content goes
    through here so a symlink-to-anywhere attack is caught uniformly.
    We accept symlinks that point INSIDE the World (common in monorepo
    worktrees, iCloud mirrors) and reject symlinks whose realpath
    leaves the World (the :mod:`alive_mcp.paths` contract).

    ``filename`` can include a POSIX-style subpath
    (``"_generated/now.json"``); it's split on forward slashes and
    joined via ``os.path.join`` so Windows separators stay honored.
    """
    parts = [p for p in filename.split("/") if p]
    if not parts:
        return None
    candidate = os.path.join(walnut_abs, _KERNEL_DIRNAME, *parts)
    if not os.path.isfile(candidate):
        return None
    # is_inside realpaths BOTH sides and uses commonpath -- handles
    # symlinks, absolute overrides, and Windows drive differences.
    if not is_inside(world_root, candidate):
        logger.warning(
            "kernel file at %r escapes World root via symlink or "
            "absolute path; rejecting",
            os.path.join(_KERNEL_DIRNAME, *parts),
        )
        return None
    return candidate


def _resolve_now_path(world_root: str, walnut_abs: str) -> Optional[str]:
    """Return the first existing + in-world ``now.json`` for ``walnut_abs``.

    v3 layout (``_kernel/now.json``) wins; v2 fallback
    (``_kernel/_generated/now.json``) is used only when v3 is absent.
    Each candidate is validated by :func:`_kernel_file_in_world`, so a
    symlinked now.json pointing outside the World is treated as
    missing. Returns ``None`` if neither path exists (or both escape).

    This checks EXISTENCE + containment only, not parseability. If a
    v3 file exists but is malformed, callers that want parse-aware
    fallback (get_walnut_state) use :func:`_iter_now_candidates`.
    """
    v3 = _kernel_file_in_world(world_root, walnut_abs, "now.json")
    if v3 is not None:
        return v3
    return _kernel_file_in_world(world_root, walnut_abs, "_generated/now.json")


def _iter_now_candidates(world_root: str, walnut_abs: str) -> List[str]:
    """Return every existing + in-world ``now.json`` in v3-then-v2 order.

    Used by ``get_walnut_state`` to mirror the vendor scanner's posture
    (``project_pure.scan_nested_walnuts``): when a v3 now.json exists
    but is malformed, try the v2 fallback before returning
    ``ERR_KERNEL_FILE_MISSING``. A symlinked now.json whose realpath
    leaves the World is dropped at this layer, NOT passed through and
    rejected later -- containment is a precondition for the file
    appearing on this list at all.
    """
    candidates: List[str] = []
    v3 = _kernel_file_in_world(world_root, walnut_abs, "now.json")
    if v3 is not None:
        candidates.append(v3)
    v2 = _kernel_file_in_world(world_root, walnut_abs, "_generated/now.json")
    if v2 is not None:
        candidates.append(v2)
    return candidates


def _read_now_for_health(world_root: str, walnut_abs: str) -> dict[str, Any]:
    """Return the parsed now.json if readable + in-world, else an empty dict.

    Best-effort -- a missing, escaping, or malformed now.json just
    means we can't derive ``health`` or ``updated``; the walnut still
    appears in the listing. Matches Hermes' posture. Delegates to the
    vendored pure parser so the list-path and tool-path share a
    single source of truth for now.json semantics. Symlinks whose
    realpath escapes the World are filtered at
    :func:`_kernel_file_in_world` and look the same as missing here.
    """
    now_path = _resolve_now_path(world_root, walnut_abs)
    if now_path is None:
        return {}
    data = project_pure.parse_now_json(now_path)
    return data if data is not None else {}


def _health_from(updated: str, rhythm: str) -> str:
    """Return ``active`` | ``quiet`` | ``waiting`` | ``unknown``.

    Matches Hermes' ``_find_walnuts`` algorithm verbatim. Unknown rhythm
    labels fall back to the weekly budget (7 days), which is the
    documented default in the rules.
    """
    if not updated or not rhythm:
        return "unknown"
    try:
        updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    now_dt = datetime.now(timezone.utc)
    # Some fixtures write naive timestamps; coerce to UTC so the subtract
    # doesn't raise.
    if updated_dt.tzinfo is None:
        updated_dt = updated_dt.replace(tzinfo=timezone.utc)
    days_since = (now_dt - updated_dt).days
    rhythm_days = _RHYTHM_DAYS.get(rhythm, 7)
    if days_since <= rhythm_days:
        return "active"
    if days_since <= rhythm_days * 2:
        return "quiet"
    return "waiting"


def _domain_from_path(posix_path: str) -> Optional[str]:
    """Return the first path segment if it's a known ALIVE domain, else None.

    ``02_Life/people/ben-flint`` -> ``02_Life``. ``People/ryn-okata`` ->
    ``None`` (the ``People/`` top-level is cross-cutting and not
    numbered; the Hermes example treats domain as the numbered segment
    only). Non-ALIVE Worlds without the numbering get ``None`` across
    the board.
    """
    if not posix_path:
        return None
    head = posix_path.split("/", 1)[0]
    return head if head in _DOMAIN_PREFIXES else None


#: Top-level directories we descend INTO when hunting for walnuts. The
#: spec's inventory heuristic (from the task brief) is "iterate ALIVE
#: domains (02_Life, 04_Ventures, 05_Experiments, 01_Archive). Walk
#: into 02_Life/people/ for person walnuts." ``People/`` is the
#: cross-cutting v2 person-walnut location that sits outside the ALIVE
#: numbering.
#:
#: At the World root, we ONLY descend into these directories -- other
#: siblings (``.alive/``, ``Documents/``, random user content) are
#: skipped. This keeps the traversal fast when the World root is
#: ``$HOME`` (the common macOS setup where ALIVE domains live alongside
#: ``Downloads/``, ``Library/``, etc., which would otherwise add
#: seconds of pointless I/O).
_WORLD_ROOT_ALLOWLIST: Tuple[str, ...] = _DOMAIN_PREFIXES + ("People",)

#: Directories we never descend into regardless of where we find them.
#: ``_kernel`` / ``.alive`` / ``raw`` are system paths. The rest are
#: build / VCS noise that a walnut should never house walnut roots
#: inside.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".alive",
    _KERNEL_DIRNAME,
    "raw",
    ".git",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".next",
    "target",
})


def _iter_walnut_paths(world_root: str) -> List[str]:
    """Return POSIX-relative paths of every walnut in the World, sorted.

    Walnut predicate: directory ``D`` containing ``D/_kernel/key.md``.
    Uses :func:`os.walk` with aggressive pruning:

    - **At the World root**, only descend into
      :data:`_WORLD_ROOT_ALLOWLIST` entries (ALIVE domain folders +
      ``People/``). This is load-bearing: when the World root IS
      ``$HOME`` (Patrick's setup), the unconstrained walk would visit
      ``Downloads/``, ``Library/`` (millions of files on macOS), and
      every other user directory -- pushing the 43-walnut inventory
      from <1s to 5-10s.
    - **Inside a domain**, skip ``.alive``, ``_kernel``, ``raw``, and
      the usual build / VCS noise.

    Nested walnuts (walnut-inside-walnut) ARE surfaced because sub-
    walnuts are a documented ALIVE pattern (``parent: [[foo]]`` in
    key.md). We don't treat a walnut's ``_kernel/key.md`` match as a
    boundary -- we just skip the ``_kernel/`` / ``raw/`` noise inside
    it and keep descending.

    Sort is lexicographic ascending by POSIX path -- deterministic
    across runs, so cursor offsets stay meaningful within a run and
    clients can cache partial results locally.
    """
    found: List[str] = []
    world_real = os.path.realpath(world_root)

    # Decide the top-level policy before we start walking. Standard
    # ALIVE worlds have at least one of the allowlist dirs at root;
    # non-standard worlds (``.alive/`` only, custom layouts, vendor
    # fixtures) do not -- we fall back to a permissive top-level
    # scan for those. The fallback still skips hidden dirs and the
    # usual system/build dirs.
    #
    # NOTE on error propagation: we deliberately let ``OSError`` from
    # ``os.listdir`` bubble up rather than silently returning an empty
    # list. A permission failure reading the World root is a real
    # signal the caller needs to surface as ``ERR_PERMISSION_DENIED``
    # -- the earlier silent-empty behavior masked that as
    # ``total=0`` with no error, which is ambiguous and misleading.
    top_entries = set(os.listdir(world_real))
    has_alive_layout = any(d in top_entries for d in _WORLD_ROOT_ALLOWLIST)

    at_root = True
    for root, dirs, files in os.walk(world_real, followlinks=False):
        # Walnut check FIRST (before pruning) so ``_kernel`` is still in
        # ``dirs`` when we look at it. A directory is a walnut iff it
        # contains ``_kernel/key.md`` on disk AND that file resolves
        # inside the World after symlink resolution. The second check
        # blocks a symlink attack where ``<walnut>/_kernel/key.md``
        # points at ``/etc/passwd`` -- we would otherwise count that
        # directory as a walnut and serve reads of the escape file
        # through ``read_walnut_kernel``.
        if _KERNEL_DIRNAME in dirs and root != world_real:
            key_path = _kernel_file_in_world(world_real, root, "key.md")
            if key_path is not None:
                rel = os.path.relpath(root, world_real)
                found.append(rel.replace(os.sep, "/"))

        # Prune for the next iteration. The first iteration is the
        # World root itself -- on a standard ALIVE world we allow ONLY
        # the domain folders + People/; on a non-standard world (no
        # domain dirs at root) we permit any non-dotfile, non-system
        # dir so the tiny vendor fixture and custom layouts still
        # resolve walnuts. Every subsequent iteration skips the
        # system + build noise but descends freely into content dirs
        # (so nested walnuts stay visible).
        if at_root:
            if has_alive_layout:
                dirs[:] = [d for d in dirs if d in _WORLD_ROOT_ALLOWLIST]
            else:
                dirs[:] = [
                    d for d in dirs
                    if d not in _SKIP_DIRS and not d.startswith(".")
                ]
            at_root = False
        else:
            dirs[:] = [
                d for d in dirs
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]

    found.sort()
    return found


def _build_record(world_root: str, posix_rel: str) -> _WalnutRecord:
    """Compose the per-walnut record from disk reads.

    Reads ``_kernel/key.md`` frontmatter for ``goal`` and ``rhythm``,
    and ``_kernel/now.json`` (v3 or v2 fallback) for ``updated``.
    ``health`` is derived from those two. Each read is best-effort --
    a malformed or escape-symlinked file yields an empty string for
    the affected field rather than dropping the walnut from the
    listing. Every disk read is containment-validated via
    :func:`_kernel_file_in_world`.
    """
    walnut_abs = os.path.join(world_root, posix_rel)
    # key.md: only read if it resolves inside the World. An escaping
    # symlink would leak file contents into the goal/rhythm fields,
    # defeating openWorldHint=False.
    key_path = _kernel_file_in_world(world_root, walnut_abs, "key.md")
    key_text = (
        _read_text_or_empty(key_path, limit_bytes=8 * 1024)
        if key_path is not None
        else ""
    )
    fm = _parse_frontmatter_fields(key_text, ("goal", "rhythm"))
    goal = fm.get("goal", "")
    rhythm = fm.get("rhythm", "")

    now = _read_now_for_health(world_root, walnut_abs)
    updated = now.get("updated", "") if isinstance(now, dict) else ""
    if not isinstance(updated, str):
        updated = ""

    return _WalnutRecord(
        path=posix_rel,
        name=os.path.basename(posix_rel),
        domain=_domain_from_path(posix_rel),
        goal=goal,
        health=_health_from(updated, rhythm),
        updated=updated,
    )


# ---------------------------------------------------------------------------
# Walnut resolution (for get_walnut_state + read_walnut_kernel).
# ---------------------------------------------------------------------------


def _resolve_walnut(world_root: str, walnut: str) -> str:
    """Resolve + validate a POSIX walnut path. Return absolute path.

    Raises:
        :class:`errors.WalnutNotFoundError` if the resolved path is not
            a walnut (no ``_kernel/key.md``) or the path is empty.
        :class:`errors.PathEscapeError` if the input escapes the World
            root via ``..`` / absolute / symlink.

    ``walnut`` is expected to be POSIX (forward slashes). We normalize
    to OS-native via ``os.path.join`` splitting -- split on ``/`` first,
    rejoin with ``os.sep`` so ``safe_join`` sees one component at a
    time and can reject absolute segments properly.
    """
    if not walnut or walnut in (".", "/"):
        raise errors.WalnutNotFoundError(
            "walnut path is empty or world-root"
        )
    # Accept both POSIX and OS-native separators on input; normalize
    # to a list of segments that safe_join can handle one-by-one.
    segments = [s for s in walnut.replace("\\", "/").split("/") if s]
    if not segments:
        raise errors.WalnutNotFoundError(
            "walnut path is empty after normalization"
        )
    # safe_join raises PathEscapeError if the result escapes world_root.
    abs_path = safe_join(world_root, *segments)
    # Walnut predicate: ``_kernel/key.md`` must exist AND resolve
    # inside the World after symlink resolution. An escaping symlink
    # looks the same as "no such walnut" from the client perspective
    # -- the server does not acknowledge a walnut whose identity file
    # is outside its authorization scope.
    key_path = _kernel_file_in_world(world_root, abs_path, "key.md")
    if key_path is None:
        raise errors.WalnutNotFoundError(
            "no _kernel/key.md at resolved walnut path (or the file "
            "resolves outside the World)"
        )
    return abs_path


def _suggest_walnut_paths(
    world_root: str, missing: str, max_suggestions: int = 3
) -> List[str]:
    """Return up to ``max_suggestions`` close matches for a missing walnut path.

    Suggestions are computed by:

    1. Listing every walnut path in the current World (cheap, already
       cached by rglob in the hot case).
    2. Fuzzy-matching the ``missing`` path's tail (``basename``)
       against each walnut's tail.
    3. Falling back to fuzzy-matching the full paths when no tail
       matches are found.

    This lives in the tool module (not ``errors.py``) because it reads
    the filesystem -- the error taxonomy stays pure string templates.
    """
    if not missing:
        return []
    try:
        walnuts = _iter_walnut_paths(world_root)
    except OSError:
        return []
    tail = os.path.basename(missing.rstrip("/"))
    if not tail:
        return []
    # Build a map from tail -> full path so tail matches yield real
    # suggestions.
    by_tail: dict[str, List[str]] = {}
    for w in walnuts:
        by_tail.setdefault(os.path.basename(w), []).append(w)
    tail_matches = get_close_matches(
        tail, list(by_tail.keys()), n=max_suggestions, cutoff=0.5
    )
    suggestions: List[str] = []
    for t in tail_matches:
        for full in by_tail[t]:
            if full not in suggestions:
                suggestions.append(full)
            if len(suggestions) >= max_suggestions:
                break
        if len(suggestions) >= max_suggestions:
            break
    if suggestions:
        return suggestions
    # Fallback: fuzzy full-path match.
    return get_close_matches(missing, walnuts, n=max_suggestions, cutoff=0.4)


def _walnut_not_found_envelope(world_root: str, missing: str) -> dict[str, Any]:
    """Build the ERR_WALNUT_NOT_FOUND envelope with fuzzy suggestions.

    Centralized so both ``get_walnut_state`` and ``read_walnut_kernel``
    (and T7 bundle tools when they surface the same error) share the
    same behavior: if fuzzy near-matches are found, they are PREPENDED
    to the static codebook guidance so clients see the most actionable
    hint first; if none are found, the static codebook list stands on
    its own. Near-misses are also logged at INFO so the audit trail
    (T12) captures them independent of what the client sees.
    """
    near = _suggest_walnut_paths(world_root, missing)
    if near:
        logger.info(
            "walnut not found: %r; near-misses: %r", missing, near
        )
        codebook = list(errors.SUGGESTIONS.get(errors.ERR_WALNUT_NOT_FOUND, ()))
        # Prepend a heading suggestion that introduces the fuzzy
        # matches so callers know they are suggestions, not
        # authoritative matches. Downstream clients can scan for the
        # leading "Did you mean" prefix if they want to split heuristic
        # from canonical guidance.
        combined = (
            ["Did you mean one of these walnut paths? {}".format(", ".join(near))]
            + codebook
        )
        return envelope.error(
            errors.ERR_WALNUT_NOT_FOUND,
            walnut=missing,
            suggestions=combined,
        )
    # No near-misses -- use the static codebook list.
    return envelope.error(errors.ERR_WALNUT_NOT_FOUND, walnut=missing)


# ---------------------------------------------------------------------------
# App-context accessor. Tools run inside FastMCP's request-context, which
# threads the lifespan ``AppContext`` through ``ctx.request_context``.
# ---------------------------------------------------------------------------


def _get_world_root(ctx: Context) -> Optional[str]:
    """Return the resolved World root, or None if discovery has not yet resolved.

    The caller emits ``ERR_NO_WORLD`` when ``None``. We read the
    AppContext directly rather than caching on import because the
    lifespan re-resolves the World on every
    ``notifications/roots/list_changed``; a cache here would serve
    stale values.
    """
    lifespan = getattr(ctx.request_context, "lifespan_context", None)
    if lifespan is None:
        return None
    return getattr(lifespan, "world_root", None)


# ---------------------------------------------------------------------------
# Tools.
# ---------------------------------------------------------------------------


@audited
async def list_walnuts(
    ctx: Context,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> dict[str, Any]:
    """List walnuts in the current World with pagination.

    Returns an envelope whose ``structuredContent`` is:

    .. code-block:: python

        {
          "walnuts": [
            {
              "path": "04_Ventures/alive",
              "name": "alive",
              "domain": "04_Ventures",
              "goal": "Build the ALIVE Context System",
              "health": "active",
              "updated": "2026-04-16T12:00:00Z",
            },
            ...
          ],
          "next_cursor": "NQ" | null,
          "total": 43,
        }

    Pagination is cursor-based but the cursor is just a base64url-encoded
    integer offset. The inventory is recomputed per call -- acceptable at
    the v0.1 scale target (Patrick's 43-walnut World returns in <1s;
    re-computing cheap at that size). ``total`` is the full count so
    clients can render "x of n" without walking the cursor.
    """
    world_root = _get_world_root(ctx)
    if world_root is None:
        return envelope.error(errors.ERR_NO_WORLD)

    # Clamp the limit to a sane range. Defensive: FastMCP's schema
    # layer coerces the type (so ``limit`` arrives as int), but the
    # SDK does not enforce bounds on its own. Clamping here rather
    # than returning an error keeps the behavior predictable --
    # callers that pass an oversized limit still get results, just
    # capped. Zero or negative gets clamped to the default instead
    # of returning zero rows (which is a confusing signal).
    if limit <= 0:
        limit = 50
    elif limit > LIST_WALNUTS_LIMIT_CAP:
        limit = LIST_WALNUTS_LIMIT_CAP

    try:
        offset = _decode_cursor(cursor)
    except errors.InvalidCursorError:
        return envelope.error(errors.ERR_INVALID_CURSOR)

    # Inventory is computed sync because os.walk is blocking IO. At 43
    # walnuts on local disk it completes in single-digit ms, so the
    # event loop pause is a non-issue. If a future World grows past the
    # <1s target, we can shim this through run_in_executor.
    try:
        paths = _iter_walnut_paths(world_root)
    except PermissionError as exc:
        # OSError subclass raised when the World root (or any walk-
        # path) denies read access. Surface the cause to the client
        # via ERR_PERMISSION_DENIED rather than returning an empty
        # inventory silently.
        logger.warning("list_walnuts traversal denied: %s", exc)
        return envelope.error(
            errors.ERR_PERMISSION_DENIED,
            walnut="(world inventory)",
            file="list",
        )
    except OSError as exc:
        # Other OS errors (ENOENT on the World root after a race,
        # bad NFS state, etc.) likewise surface as
        # ERR_PERMISSION_DENIED with a generic diagnostic -- the
        # audit log (T12) captures the specific errno.
        logger.warning("list_walnuts traversal failed: %s", exc)
        return envelope.error(
            errors.ERR_PERMISSION_DENIED,
            walnut="(world inventory)",
            file="list",
        )

    total = len(paths)
    # Clamp offset to [0, total] so a cursor past the end returns an
    # empty page rather than IndexError.
    start = min(offset, total)
    end = min(start + limit, total)
    page = paths[start:end]

    records = [_build_record(world_root, p).as_dict() for p in page]

    next_cursor: Optional[str]
    if end < total:
        next_cursor = _encode_cursor(end)
    else:
        next_cursor = None

    return envelope.ok(
        {
            "walnuts": records,
            "next_cursor": next_cursor,
            "total": total,
        }
    )


@audited
async def get_walnut_state(
    ctx: Context,
    walnut: str,
) -> dict[str, Any]:
    """Return the ``now.json`` projection for ``walnut`` (v3 with v2 fallback).

    ``walnut`` is a POSIX-relative path from the World root as returned
    by :func:`list_walnuts`. Resolution order:

    1. ``<walnut>/_kernel/now.json`` (v3 flat layout).
    2. ``<walnut>/_kernel/_generated/now.json`` (v2 layout).

    Does NOT trigger on-demand assembly -- that's v0.2 territory. If
    both paths are missing, returns ``ERR_KERNEL_FILE_MISSING``.
    Malformed JSON returns the same error plus logs the underlying
    cause to stderr (clients get the code, the audit log gets the
    diagnostic).
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

    # Try v3 first, then v2 -- mirrors project_pure.scan_nested_walnuts'
    # posture. If v3 exists but is malformed or has a non-dict root, we
    # fall through to v2 rather than surface a MISSING error that hid a
    # usable v2 projection. Only after EVERY candidate fails do we
    # emit ERR_KERNEL_FILE_MISSING. A symlinked now.json pointing
    # outside the World is dropped at the candidate-list step and
    # never reaches the parse loop.
    candidates = _iter_now_candidates(world_root, walnut_abs)
    if not candidates:
        return envelope.error(
            errors.ERR_KERNEL_FILE_MISSING,
            walnut=walnut,
            file="now",
        )

    # Probe permission separately so we can distinguish "unreadable
    # due to perms" from "parse failure" at the envelope layer. The
    # vendored parser swallows both into ``None`` with a warning --
    # fine for projection scanners, but tool callers need the
    # distinction for actionable suggestions.
    #
    # Detection uses a two-step probe to avoid TOCTOU-masking the
    # permission case as a parse failure:
    #
    # 1. ``os.access(R_OK)`` filters the common pre-emptive case
    #    (dir mode 0o000, file readable-by-owner-only called by a
    #    different user) without an open() attempt.
    # 2. If ``parse_now_json`` returns ``None``, a second narrow
    #    ``open(..., 'r')`` in an ``except PermissionError`` branch
    #    catches the race where perms change between the access
    #    probe and the parser's ``open()`` call. This distinguishes
    #    "file exists but we lost perms mid-read" from "file exists
    #    and parser gave up on content" reliably.
    last_error_was_permission = False
    for now_path in candidates:
        if not os.access(now_path, os.R_OK):
            last_error_was_permission = True
            continue
        data = project_pure.parse_now_json(now_path)
        if data is None:
            # parse_now_json already emitted a MalformedYAMLWarning;
            # the tool layer doesn't need to duplicate the log. We
            # DO need to disambiguate parse-failure from late-arriving
            # permission-failure, though, or a concurrent chmod would
            # get reported as MISSING. The extra open() is cheap at
            # v0.1 scale (one extra stat per failed candidate).
            try:
                with open(now_path, "rb"):
                    pass
            except PermissionError:
                last_error_was_permission = True
            except OSError:
                # File vanished / other OS issue -- treat as parse
                # failure (MISSING) rather than permission.
                pass
            continue
        # Return the parsed dict verbatim. Tool contract is "read
        # what's on disk" -- no projection / reshape. Callers that
        # need a stable subset filter on their side or use
        # read_walnut_kernel(file="now") for the raw text.
        return envelope.ok(data)

    # Every candidate failed. If the last failure was a permission
    # error (i.e. no candidate ever parsed AND at least one was
    # unreadable due to perms), surface that specifically; otherwise
    # the file(s) exist but are unusable which we map to MISSING
    # because the client can't distinguish "never written" from
    # "corrupt" without another tool (T12 audit has the detail).
    if last_error_was_permission:
        return envelope.error(
            errors.ERR_PERMISSION_DENIED,
            walnut=walnut,
            file="now",
        )
    return envelope.error(
        errors.ERR_KERNEL_FILE_MISSING,
        walnut=walnut,
        file="now",
    )


@audited
async def read_walnut_kernel(
    ctx: Context,
    walnut: str,
    file: KernelFile,
) -> dict[str, Any]:
    """Return the raw content of a kernel file as ``{content, mime}``.

    ``file`` is a literal drawn from :data:`KernelFile` (``key``,
    ``log``, ``insights``, ``now``). Unknown values are rejected by
    FastMCP's schema-layer validation -- the tool never sees them.

    For ``file="now"`` the v3 -> v2 fallback rule matches
    :func:`get_walnut_state`. For the three markdown files, only the
    v3 flat layout (``<walnut>/_kernel/<file>.md``) is checked --
    there is no v2 alternate location for those.

    Whole-file read, no pagination. The log tool with chapter-aware
    pagination is T9's ``read_log`` -- this tool is the "attach the
    whole thing" surface that resources (T10) mirror.
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

    # Resolve the on-disk path by file stem. Every candidate goes
    # through :func:`_kernel_file_in_world` so a symlinked kernel
    # file whose realpath escapes the World is filtered before we
    # open it. Without this, a walnut owner could point
    # ``_kernel/key.md`` at ``/etc/passwd`` and read_walnut_kernel
    # would happily serve its contents -- violating
    # ``openWorldHint=False``.
    if file == "now":
        target = _resolve_now_path(world_root, walnut_abs)
    else:
        basename = _FILE_MAP.get(file)
        if basename is None:
            # Defense-in-depth: the schema literal keeps this
            # unreachable, but a malformed client that side-steps
            # validation still hits an envelope, not a traceback.
            return envelope.error(errors.ERR_KERNEL_FILE_MISSING, walnut=walnut, file=file)
        target = _kernel_file_in_world(world_root, walnut_abs, basename)

    if target is None:
        return envelope.error(
            errors.ERR_KERNEL_FILE_MISSING,
            walnut=walnut,
            file=file,
        )

    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()
    except PermissionError:
        return envelope.error(
            errors.ERR_PERMISSION_DENIED,
            walnut=walnut,
            file=file,
        )
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "read_walnut_kernel: read failed for walnut=%r file=%r: %s",
            walnut,
            file,
            exc,
        )
        return envelope.error(
            errors.ERR_KERNEL_FILE_MISSING,
            walnut=walnut,
            file=file,
        )

    return envelope.ok(
        {
            "content": content,
            "mime": _MIME_MAP[file],
        }
    )


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------

#: Shared annotations for every tool in this module. The combination is
#: spec-frozen: read-only (no side effects), non-destructive (no
#: mutation possible), closed-world (scoped to the resolved World root).
#: MCP clients surface these in their UI so the human can reason about
#: safety without reading docs.
_WALNUT_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=False,
    idempotentHint=True,
)


def register(server: FastMCP[Any]) -> None:
    """Register the three walnut tools on ``server``.

    Called by :func:`alive_mcp.server.build_server` exactly once after
    the capability override is installed. The decorator-based
    registration means FastMCP builds the JSON schema from the
    function signatures -- keeping the docstrings + type hints here
    authoritative for the wire shape.
    """
    server.tool(
        name="list_walnuts",
        description=(
            "List walnuts in the active ALIVE World. Returns "
            "{walnuts: [{path, name, domain, goal, health, updated}, ...], "
            "next_cursor, total}. Use 'path' as the canonical identifier in "
            "follow-up calls; 'name' is display-only. Cursor-paginate when "
            "'next_cursor' is non-null."
        ),
        annotations=_WALNUT_TOOL_ANNOTATIONS,
    )(list_walnuts)

    server.tool(
        name="get_walnut_state",
        description=(
            "Read the current state projection (now.json) for a walnut. "
            "Returns the parsed dict: phase, updated, next, bundles, "
            "context, etc. Does not assemble on demand -- reads what is on "
            "disk. Accepts POSIX-relative walnut paths from list_walnuts."
        ),
        annotations=_WALNUT_TOOL_ANNOTATIONS,
    )(get_walnut_state)

    server.tool(
        name="read_walnut_kernel",
        description=(
            "Read a kernel file whole. file is one of 'key' (identity), "
            "'log' (full history), 'insights' (domain knowledge), or 'now' "
            "(current state JSON). Returns {content, mime}. For paginated "
            "log reads use the read_log tool instead."
        ),
        annotations=_WALNUT_TOOL_ANNOTATIONS,
    )(read_walnut_kernel)


__all__ = [
    "KernelFile",
    "list_walnuts",
    "get_walnut_state",
    "read_walnut_kernel",
    "register",
]
