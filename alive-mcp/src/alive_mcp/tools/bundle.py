"""Bundle-centric read tools (fn-10-60k.7 / T7).

Three tools, all read-only, all annotated
``ToolAnnotations(readOnlyHint=True, destructiveHint=False,
openWorldHint=False, idempotentHint=True)``:

* :func:`list_bundles` -- inventory a walnut's bundles as
  ``{bundles: [{path, name, goal, status, updated, due, outcome,
  phase}, ...]}``. 8-key subset of the frozen 9-key manifest set:
  drops ``context`` (too large for list view -- available via
  ``get_bundle``) and ``active_sessions`` (internal, not useful
  cross-session).
* :func:`get_bundle` -- read a single bundle's manifest plus derived
  counts. Returns ``{manifest: {...9 keys...}, derived: {task_counts,
  raw_file_count, last_updated}}``. The ``manifest`` dict carries
  every one of the 9 frozen keys the vendored parser extracts;
  missing values appear as ``None`` so the shape is stable across
  bundles.
* :func:`read_bundle_manifest` -- narrower wrapper returning only
  ``{manifest: {...9 keys...}, warnings: [...]}``. Retained as a
  distinct tool for clients that prefer the minimal interface and
  want to surface per-key parse warnings (the derived counts carry
  I/O cost that this surface avoids).

Frozen 9-key manifest contract
------------------------------
The vendored ``walnut_paths._parse_manifest_minimal`` (see
``claude-code/plugins/alive/scripts/walnut_paths.py`` L170-228) is a
regex-minimal parser that extracts exactly these 9 keys and nothing
more:

* Simple scalars (L190): ``goal, status, updated, due, name, outcome,
  phase``.
* Multi-line or single-line context block (L198-214): ``context``.
* List derived from the ``squirrels:`` YAML list (L216-226):
  ``active_sessions``.

Any other YAML content in the manifest (``sources``,
``linked_bundles``, ``sensitivity``, ``species``, ``version``,
``tags``, ``people``, ``discovered``, ``installs``, etc.) is SILENTLY
DROPPED. This is by design -- alive-mcp has no PyYAML dep and the
regex-minimal parser was purpose-built for this. Any future need for
additional keys means extending the vendored parser with explicit
tests per key; v0.1 does not do that.

Return-shape guarantees
-----------------------
* :func:`list_bundles` returns the 8-key subset (no ``context``, no
  ``active_sessions``). Missing scalars -> ``None`` so the caller can
  dispatch on presence without special-casing missing keys.
* :func:`get_bundle` returns all 9 keys. Missing values -> ``None``.
  Derived fields:
    - ``task_counts`` -- ``{urgent, active, todo, blocked, done}``
      aggregated from ``tasks.json`` files inside the bundle.
    - ``raw_file_count`` -- count of regular files under
      ``<bundle>/raw/``. Zero when the directory is absent.
    - ``last_updated`` -- the ``updated`` field if present, else an
      ISO date derived from the bundle's most-recent file mtime.
* :func:`read_bundle_manifest` returns the same 9-key manifest plus a
  ``warnings`` list. Warnings accumulate ``"could not parse '<key>'"``
  entries when a key is completely absent from the parser output
  (defense-in-depth -- the parser already drops malformed keys, so
  the warnings list is typically empty on well-formed manifests; it
  exists so clients can distinguish "not present in the manifest"
  from "parser failure" when auditing).

Bundle identifier convention
----------------------------
The ``bundle`` param is a POSIX-relative path from the walnut root,
matching what :func:`list_bundles` returns in each entry's ``path``
field. The vendored :func:`walnut_paths.scan_bundles` surfaces
bundles under three layouts -- v3 flat (``<walnut>/foo``), v2 nested
(``<walnut>/bundles/foo``), and v1 legacy
(``<walnut>/_core/_capsules/foo``) -- all keyed by their POSIX
relpath. Callers pass back the ``path`` field verbatim.

Error posture
-------------
* ``ERR_NO_WORLD`` when the lifespan never resolved a World.
* ``ERR_PATH_ESCAPE`` when ``walnut`` or ``bundle`` would leave the
  World root after realpath resolution.
* ``ERR_WALNUT_NOT_FOUND`` when ``walnut`` doesn't resolve to a
  walnut. Uses the same fuzzy-suggestion wiring as the walnut tools.
* ``ERR_BUNDLE_NOT_FOUND`` when the bundle lookup fails. Includes a
  "Did you mean" suggestion prefix with fuzzy matches on the bundle
  tail when candidates exist.
* Malformed manifests never raise -- the parser returns ``None`` or
  drops individual keys, so :func:`get_bundle` returns partial data
  with a ``warnings`` list populated (for :func:`read_bundle_manifest`
  which surfaces warnings; :func:`get_bundle` does not).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from difflib import get_close_matches
from typing import Any, Dict, List, Optional, Tuple

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from alive_mcp import envelope, errors
from alive_mcp._vendor import walnut_paths
from alive_mcp._vendor._pure import tasks_pure
from alive_mcp.paths import is_inside, safe_join
from alive_mcp.tools._audit_stub import audited
from alive_mcp.tools.walnut import _resolve_walnut, _walnut_not_found_envelope

logger = logging.getLogger("alive_mcp.tools.bundle")


# ---------------------------------------------------------------------------
# Frozen key sets.
# ---------------------------------------------------------------------------

#: The 9 keys ``walnut_paths._parse_manifest_minimal`` can extract. Any
#: key not in this set is DROPPED by the parser and therefore not
#: present on tool output. See module docstring for why.
_MANIFEST_KEYS: Tuple[str, ...] = (
    "name",
    "goal",
    "outcome",
    "status",
    "phase",
    "updated",
    "due",
    "context",
    "active_sessions",
)

#: Subset returned by ``list_bundles``. Drops ``context`` (too large
#: for list view) and ``active_sessions`` (internal, not useful
#: cross-session). ``path`` is prepended by the list tool as the
#: canonical identifier.
_LIST_BUNDLE_KEYS: Tuple[str, ...] = (
    "name",
    "goal",
    "status",
    "updated",
    "due",
    "outcome",
    "phase",
)


def _normalize_manifest(parsed: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the 9-key manifest dict with missing keys as ``None``.

    The vendored parser returns only keys it successfully extracted.
    Tools guarantee a stable shape across bundles, so we backfill
    missing keys with ``None``. ``active_sessions`` specifically is
    normalized to an empty list (not ``None``) because the parser
    always emits at least an empty list for that key -- this keeps
    the type invariant (``list[str]``) explicit and matches the
    parser's contract.
    """
    out: Dict[str, Any] = {}
    src = parsed or {}
    for key in _MANIFEST_KEYS:
        if key == "active_sessions":
            value = src.get(key, [])
            # Defensive: ensure list type even if parser was bypassed.
            out[key] = list(value) if isinstance(value, (list, tuple)) else []
        else:
            value = src.get(key)
            # Empty string -> None so clients can dispatch on presence
            # uniformly. The parser can emit "" for quoted-empty
            # scalars (``goal: ''``); treating that as missing keeps
            # the shape meaningful.
            out[key] = value if value else None
    return out


def _list_subset(manifest_nine: Dict[str, Any]) -> Dict[str, Any]:
    """Project the 9-key manifest to the 7-key list-view subset.

    Called once per bundle by :func:`list_bundles`. The ``path`` field
    is injected by the caller (it isn't in the manifest).
    """
    return {key: manifest_nine.get(key) for key in _LIST_BUNDLE_KEYS}


def _collect_warnings(parsed: Optional[Dict[str, Any]]) -> List[str]:
    """Return ``["could not parse '<key>'", ...]`` for every missing key.

    Called by :func:`read_bundle_manifest`. An empty or ``None``
    parser result yields a warning for every key. A well-formed
    manifest with all 9 keys yields an empty list.

    NOTE: the parser silently drops keys it can't extract rather than
    flagging them, so this is a best-effort reconstruction of what
    went wrong. It captures ``active_sessions`` only when absent
    entirely (the parser emits ``[]`` when the ``squirrels:`` list is
    present-but-empty; that's a valid zero-sessions state, not a
    parse failure).
    """
    if parsed is None:
        return [
            "could not parse '{}'".format(key)
            for key in _MANIFEST_KEYS
        ]
    warnings: List[str] = []
    for key in _MANIFEST_KEYS:
        if key == "active_sessions":
            # Parser always produces this key (even as []), so a
            # missing entry means the parser itself dropped it --
            # which shouldn't happen but we flag it if it does.
            if key not in parsed:
                warnings.append("could not parse '{}'".format(key))
            continue
        if key not in parsed or not parsed.get(key):
            warnings.append("could not parse '{}'".format(key))
    return warnings


# ---------------------------------------------------------------------------
# Bundle resolution.
# ---------------------------------------------------------------------------


def _scan_bundles_safe(walnut_abs: str, world_root: str) -> Dict[str, Dict[str, Any]]:
    """Wrap :func:`walnut_paths.scan_bundles` with containment checks.

    The vendored scanner returns POSIX-relpath keys under the walnut
    root. Each relpath is re-validated against the World root via
    :func:`safe_join` so a symlinked bundle directory whose realpath
    escapes the World is filtered before it reaches the envelope
    layer. Escaping bundles are dropped silently (indistinguishable
    from "not a bundle" at the client, matching the security posture
    of :func:`walnut._kernel_file_in_world`).
    """
    try:
        raw = walnut_paths.scan_bundles(walnut_abs)
    except OSError as exc:
        # Propagate permission/IO errors to the caller so the tool
        # layer can map to ERR_PERMISSION_DENIED.
        logger.warning("scan_bundles failed for %r: %s", walnut_abs, exc)
        raise
    safe: Dict[str, Dict[str, Any]] = {}
    for relpath, manifest in raw.items():
        # Re-validate each bundle path resolves inside the World
        # after symlink resolution. scan_bundles returns paths
        # relative to the walnut root; we check containment against
        # the World root because the walnut itself already resolved
        # inside the World.
        segments = [p for p in relpath.split("/") if p]
        try:
            resolved = safe_join(walnut_abs, *segments)
        except errors.PathEscapeError:
            logger.warning(
                "bundle at %r under walnut %r escapes World via "
                "symlink; dropping",
                relpath,
                walnut_abs,
            )
            continue
        if not is_inside(world_root, resolved):
            logger.warning(
                "bundle realpath at %r escapes World root; dropping",
                relpath,
            )
            continue
        safe[relpath] = manifest
    return safe


def _resolve_bundle(
    world_root: str,
    walnut_abs: str,
    bundle: str,
) -> Tuple[str, str, Dict[str, Any]]:
    """Resolve a bundle relpath to ``(canonical_relpath, abs_path, manifest)``.

    Strategy:

    1. Reject empty input.
    2. Normalize separators (accept both POSIX and OS-native on the
       wire, return POSIX).
    3. Scan bundles once; look up by both the requested relpath AND
       its tail (``os.path.basename``) to allow callers to pass a
       bare bundle name when unambiguous.
    4. If the requested relpath matches directly, use it. If a bare
       name matches exactly one bundle, use that. If it matches
       multiple, raise ``BundleNotFoundError`` (ambiguous).
    5. Validate the resolved abs_path is inside the World root.

    Raises ``BundleNotFoundError`` with an empty manifest when the
    bundle can't be located. The caller builds the suggestions list.
    """
    if not bundle or bundle in (".", "/"):
        raise errors.BundleNotFoundError("bundle identifier is empty")

    # Normalize separators. We accept both forward slashes and OS-
    # native paths on input; internal keying is always POSIX.
    normalized = bundle.replace("\\", "/").strip("/")
    if not normalized:
        raise errors.BundleNotFoundError(
            "bundle identifier is empty after normalization"
        )

    # Scan under the walnut. Escape-checking filters done in the
    # helper -- a malicious symlink bundle is dropped here.
    try:
        bundles = _scan_bundles_safe(walnut_abs, world_root)
    except OSError:
        # Permission / IO error reading the walnut. Surface as
        # bundle-not-found because the caller can't distinguish
        # "no such bundle" from "can't list" via the envelope; the
        # audit log (T12) captures the specific errno.
        raise errors.BundleNotFoundError(
            "bundle listing failed for walnut"
        )

    if normalized in bundles:
        relpath = normalized
    else:
        # Bare-name fallback: match on basename. Only accept if the
        # match is unambiguous so ambiguous names (two bundles
        # sharing a tail across layouts) fail loudly.
        tail = os.path.basename(normalized)
        if tail and tail != normalized:
            # Caller supplied a multi-segment path that didn't match;
            # don't silently fall back to the tail lookup -- that
            # would mask typos in the prefix segments.
            raise errors.BundleNotFoundError(
                "bundle {!r} not found under walnut".format(normalized)
            )
        matches = [k for k in bundles if os.path.basename(k) == tail]
        if len(matches) == 1:
            relpath = matches[0]
        else:
            raise errors.BundleNotFoundError(
                "bundle {!r} not found under walnut".format(normalized)
            )

    # Re-derive the absolute path. scan_bundles already verified the
    # manifest exists, but we go through safe_join so a race with a
    # symlink flip between scan and tool return would still be
    # caught by the containment check.
    segments = [p for p in relpath.split("/") if p]
    abs_path = safe_join(walnut_abs, *segments)
    return relpath, abs_path, bundles[relpath]


def _suggest_bundle_relpaths(
    walnut_abs: str,
    world_root: str,
    missing: str,
    max_suggestions: int = 3,
) -> List[str]:
    """Return up to ``max_suggestions`` close matches for a missing bundle.

    Matches on bundle tail first (basename) then full relpath. Mirror
    of :func:`walnut._suggest_walnut_paths` so the two tools present
    identical affordance to callers.
    """
    if not missing:
        return []
    try:
        bundles = _scan_bundles_safe(walnut_abs, world_root)
    except OSError:
        return []
    if not bundles:
        return []
    relpaths = list(bundles.keys())
    tail = os.path.basename(missing.replace("\\", "/").rstrip("/"))
    by_tail: Dict[str, List[str]] = {}
    for r in relpaths:
        by_tail.setdefault(os.path.basename(r), []).append(r)
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
    return get_close_matches(
        missing, relpaths, n=max_suggestions, cutoff=0.4
    )


def _bundle_not_found_envelope(
    walnut: str,
    walnut_abs: str,
    world_root: str,
    missing: str,
) -> Dict[str, Any]:
    """Build the ERR_BUNDLE_NOT_FOUND envelope with fuzzy suggestions.

    Mirrors :func:`walnut._walnut_not_found_envelope` so both tools
    present the same affordance. Fuzzy matches are prepended to the
    static codebook list with a "Did you mean" lead.
    """
    near = _suggest_bundle_relpaths(walnut_abs, world_root, missing)
    if near:
        logger.info(
            "bundle not found: %r under walnut %r; near-misses: %r",
            missing,
            walnut,
            near,
        )
        codebook = list(errors.SUGGESTIONS.get(errors.ERR_BUNDLE_NOT_FOUND, ()))
        combined = (
            ["Did you mean one of these bundle paths? {}".format(", ".join(near))]
            + codebook
        )
        return envelope.error(
            errors.ERR_BUNDLE_NOT_FOUND,
            walnut=walnut,
            bundle=missing,
            suggestions=combined,
        )
    return envelope.error(
        errors.ERR_BUNDLE_NOT_FOUND,
        walnut=walnut,
        bundle=missing,
    )


# ---------------------------------------------------------------------------
# Derived fields for get_bundle.
# ---------------------------------------------------------------------------


def _task_counts_for_bundle(bundle_abs: str) -> Dict[str, int]:
    """Aggregate task counts from tasks.json files under a bundle.

    Mirrors the counting logic in
    ``tasks_pure.summary_from_walnut`` without the bundle-level
    branching (scan every ``tasks.json`` under the bundle, recursive).
    Returns zeros when no task file exists.
    """
    counts = {"urgent": 0, "active": 0, "todo": 0, "blocked": 0, "done": 0}
    try:
        task_files = tasks_pure._all_task_files(bundle_abs)
    except OSError:
        return counts
    for tf in task_files:
        data = tasks_pure._read_tasks_json(tf)
        if data is None:
            continue
        for t in data.get("tasks", []):
            if not isinstance(t, dict):
                continue
            priority = t.get("priority", "todo")
            status = t.get("status", "todo")
            if priority == "urgent":
                counts["urgent"] += 1
            if status == "active":
                counts["active"] += 1
            elif status == "todo":
                counts["todo"] += 1
            elif status == "blocked":
                counts["blocked"] += 1
            elif status == "done":
                counts["done"] += 1
    return counts


def _raw_file_count(bundle_abs: str) -> int:
    """Count regular files under ``<bundle>/raw/``. Zero when absent.

    Descends recursively; counts symlinks that point to regular
    files (the alternative -- strict realpath containment -- would
    require per-entry checks and the benefit is marginal for a
    count). Broken symlinks and directory symlinks to outside the
    World are skipped implicitly because ``os.walk`` defaults to
    ``followlinks=False``, which prevents traversal into foreign
    trees.
    """
    raw_dir = os.path.join(bundle_abs, "raw")
    if not os.path.isdir(raw_dir):
        return 0
    count = 0
    try:
        for _root, _dirs, files in os.walk(raw_dir, followlinks=False):
            for f in files:
                count += 1
    except OSError:
        return count
    return count


def _bundle_last_updated(bundle_abs: str, manifest_updated: Optional[str]) -> str:
    """Return the bundle's last-updated timestamp.

    Prefer the manifest's ``updated`` field when present. Fall back
    to the most recent mtime under the bundle directory (as an ISO
    date) so clients always get something meaningful. Empty-string
    fallback ``"1970-01-01"`` matches ``tasks_pure._dir_last_touched``
    so callers that compose both surfaces see consistent sentinels.
    """
    if manifest_updated:
        return manifest_updated
    latest = 0.0
    try:
        for root, _dirs, files in os.walk(bundle_abs, followlinks=False):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    mt = os.path.getmtime(fp)
                    if mt > latest:
                        latest = mt
                except OSError:
                    continue
    except OSError:
        pass
    if latest == 0.0:
        return "1970-01-01"
    return datetime.fromtimestamp(latest).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# App-context accessor.
# ---------------------------------------------------------------------------


def _get_world_root(ctx: Context) -> Optional[str]:
    """Return the resolved World root, or None if not yet resolved.

    Duplicates the walnut-tools helper -- intentional, so the bundle
    module doesn't import a private from its sibling. Both trivially
    read ``ctx.request_context.lifespan_context.world_root``.
    """
    lifespan = getattr(ctx.request_context, "lifespan_context", None)
    if lifespan is None:
        return None
    return getattr(lifespan, "world_root", None)


# ---------------------------------------------------------------------------
# Tools.
# ---------------------------------------------------------------------------


@audited
async def list_bundles(
    ctx: Context,
    walnut: str,
) -> Dict[str, Any]:
    """List bundles in ``walnut`` with an 8-key view per entry.

    Returns an envelope whose ``structuredContent`` is:

    .. code-block:: python

        {
          "bundles": [
            {
              "path": "bundles/shielding-review",
              "name": "Shielding Review",
              "goal": "Lock the vendor + produce v1 brief",
              "status": "draft",
              "updated": "2026-04-15",
              "due": None,
              "outcome": None,
              "phase": None,
            },
            ...
          ]
        }

    ``path`` is the canonical identifier callers echo back in
    :func:`get_bundle` / :func:`read_bundle_manifest`. Entries are
    sorted by ``path`` (lexicographic) for determinism across runs.

    Drops ``context`` (too large for list view -- retrieve via
    :func:`get_bundle`) and ``active_sessions`` (internal, not
    useful cross-session). Every remaining key is guaranteed to
    appear with a value or ``None``.
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
        bundles = _scan_bundles_safe(walnut_abs, world_root)
    except PermissionError as exc:
        logger.warning("list_bundles denied for %r: %s", walnut, exc)
        return envelope.error(
            errors.ERR_PERMISSION_DENIED,
            walnut=walnut,
            file="bundles",
        )
    except OSError as exc:
        logger.warning("list_bundles failed for %r: %s", walnut, exc)
        return envelope.error(
            errors.ERR_PERMISSION_DENIED,
            walnut=walnut,
            file="bundles",
        )

    # Build the list-view records. Sorted by path so the output is
    # deterministic -- tests and callers can rely on position.
    records: List[Dict[str, Any]] = []
    for relpath in sorted(bundles.keys()):
        manifest_nine = _normalize_manifest(bundles[relpath])
        subset = _list_subset(manifest_nine)
        record: Dict[str, Any] = {"path": relpath}
        record.update(subset)
        records.append(record)

    return envelope.ok({"bundles": records})


@audited
async def get_bundle(
    ctx: Context,
    walnut: str,
    bundle: str,
) -> Dict[str, Any]:
    """Return the 9-key manifest plus derived counts for one bundle.

    Returns:

    .. code-block:: python

        {
          "manifest": {
            "name": "...", "goal": "...", "outcome": None,
            "status": "draft", "phase": None,
            "updated": "2026-04-15", "due": None,
            "context": "...", "active_sessions": []
          },
          "derived": {
            "task_counts": {"urgent": 0, "active": 2, "todo": 5,
                            "blocked": 0, "done": 1},
            "raw_file_count": 12,
            "last_updated": "2026-04-15"
          }
        }

    The ``manifest`` dict always carries all 9 keys; missing values
    appear as ``None`` (or ``[]`` for ``active_sessions``). The
    ``derived`` counts are recomputed per call from on-disk state.
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
        _, bundle_abs, parsed = _resolve_bundle(world_root, walnut_abs, bundle)
    except errors.PathEscapeError:
        return envelope.error(errors.ERR_PATH_ESCAPE)
    except errors.BundleNotFoundError:
        return _bundle_not_found_envelope(
            walnut, walnut_abs, world_root, bundle
        )

    manifest_nine = _normalize_manifest(parsed)

    task_counts = _task_counts_for_bundle(bundle_abs)
    raw_count = _raw_file_count(bundle_abs)
    last_updated = _bundle_last_updated(bundle_abs, manifest_nine.get("updated"))

    return envelope.ok(
        {
            "manifest": manifest_nine,
            "derived": {
                "task_counts": task_counts,
                "raw_file_count": raw_count,
                "last_updated": last_updated,
            },
        }
    )


@audited
async def read_bundle_manifest(
    ctx: Context,
    walnut: str,
    bundle: str,
) -> Dict[str, Any]:
    """Return the 9-key manifest and a warnings list for one bundle.

    Returns:

    .. code-block:: python

        {
          "manifest": { ...9 keys, missing as None/[] ... },
          "warnings": ["could not parse 'phase'", ...]
        }

    The narrower shape (no ``derived`` field) is for clients that
    want the manifest surface without the per-call I/O cost of
    counting tasks + raw files. ``warnings`` lists every frozen key
    the parser failed to extract; on a well-formed manifest with
    every field present the list is empty.
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
        _, _bundle_abs, parsed = _resolve_bundle(world_root, walnut_abs, bundle)
    except errors.PathEscapeError:
        return envelope.error(errors.ERR_PATH_ESCAPE)
    except errors.BundleNotFoundError:
        return _bundle_not_found_envelope(
            walnut, walnut_abs, world_root, bundle
        )

    manifest_nine = _normalize_manifest(parsed)
    warnings = _collect_warnings(parsed)

    return envelope.ok(
        {
            "manifest": manifest_nine,
            "warnings": warnings,
        }
    )


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


_BUNDLE_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=False,
    idempotentHint=True,
)


def register(server: FastMCP[Any]) -> None:
    """Register the three bundle tools on ``server``.

    Called by :func:`alive_mcp.server.build_server` alongside the
    walnut tools. The annotations are identical -- all three tools
    are read-only and closed-world.
    """
    server.tool(
        name="list_bundles",
        description=(
            "List bundles in a walnut. Returns {bundles: [{path, name, "
            "goal, status, updated, due, outcome, phase}, ...]} -- "
            "subset of the manifest keys (drops 'context' and "
            "'active_sessions' for list view). Use 'path' as the "
            "canonical bundle identifier in get_bundle / "
            "read_bundle_manifest."
        ),
        annotations=_BUNDLE_TOOL_ANNOTATIONS,
    )(list_bundles)

    server.tool(
        name="get_bundle",
        description=(
            "Read a bundle's manifest plus derived counts. Returns "
            "{manifest: {name, goal, outcome, status, phase, updated, "
            "due, context, active_sessions}, derived: {task_counts, "
            "raw_file_count, last_updated}}. Manifest carries all 9 "
            "frozen keys; missing values appear as null."
        ),
        annotations=_BUNDLE_TOOL_ANNOTATIONS,
    )(get_bundle)

    server.tool(
        name="read_bundle_manifest",
        description=(
            "Read a bundle's manifest without derived counts. Returns "
            "{manifest: {...9 keys...}, warnings: [...]}. Use when the "
            "task / raw-file counts are not needed; cheaper than "
            "get_bundle because it skips disk scans."
        ),
        annotations=_BUNDLE_TOOL_ANNOTATIONS,
    )(read_bundle_manifest)


__all__ = [
    "list_bundles",
    "get_bundle",
    "read_bundle_manifest",
    "register",
]
