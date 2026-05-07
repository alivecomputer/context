"""``verify`` -- live-read verification for plugin-surface drift (phase 11).

Architectural bet: verification expectations for the **plugin surface**
(hooks events, manifest commands/agents, skill manifests) are derived
at runtime by parsing the live plugin code rather than from a hand-
maintained checklist. Three error classes drive the report:

1. **Malformed**          -- a parse refusal on plugin code. The plugin
                              itself is broken; the verify report
                              surfaces a diagnostic and the upgrade
                              refuses (caller routes the exit).
2. **Missing-path**        -- a user extension references a plugin path
                              that the live plugin no longer exposes.
                              Classified as a v2-pattern detection
                              (probably a stale extension).
3. **Catalog-match**       -- a user extension's content matches a
                              ``walkthrough_eligible: True`` entry from
                              the retired-pattern catalog. R13: arbitrary
                              divergence is left alone; only catalog
                              matches surface as walkthrough items.

Scope discipline (R5 narrow audit):
* No hardcoded plugin-code path literals (the hooks-manifest filename,
  the plugin-manifest filename, the world-state directory name, etc.).
  All path knowledge enters via the ``PluginSurfacePaths`` dataclass
  populated by ``file_snapshot.py`` (R5-allowlisted path-provider).
* No direct disk reads. Every byte arrives via the ``read_provider``
  callable -- ``Path.read_bytes`` on a real run; the post-state overlay
  on a dry-run.

Side-effect freedom (R7): no module-level write primitives. Pure
functions only.

Stdlib-only (R10): no PyYAML / ruamel imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import retired_patterns
from ._phase_helpers import compute_byte_offsets
from .parse import ParseError
from .parse import hooks as _hooks_parse
from .parse import manifest as _manifest_parse
from .parse import skill_frontmatter as _skill_parse


__all__ = (
    "PluginSurfacePaths",
    "MalformedFinding",
    "MissingPathFinding",
    "CatalogMatchFinding",
    "VerificationReport",
    "verify",
    "scan_user_extensions",
)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginSurfacePaths:
    """Paths verify needs to read.

    Populated by ``file_snapshot.py`` (the explicit path-provider; this
    keeps verify ignorant of plugin-code path literals so the R5 audit
    stays clean).

    ``user_extension_paths`` is the user-content roster: every captured
    path under the world's user-extension trees (``.alive/skills/``,
    ``.alive/rules/``, ``.alive/hooks/``) that the catalog matcher can
    inspect. Empty when no user extensions are present.
    """

    hooks_json: Path
    plugin_json: Path
    skill_manifests: Tuple[Path, ...]
    user_extension_paths: Tuple[Path, ...] = ()


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MalformedFinding:
    """Class-1 error: a plugin-code file failed to parse.

    ``kind`` names the parser that refused (``hooks`` | ``manifest`` |
    ``skill_frontmatter``). ``message`` is the human-readable detail
    from the underlying ``ParseError``.
    """

    path: str
    kind: str
    message: str


@dataclass(frozen=True)
class MissingPathFinding:
    """Class-2 finding: a user extension references a path the live
    plugin no longer exposes.

    ``user_extension_path`` is the file the reference came from;
    ``referenced_path`` is the path string that didn't resolve;
    ``rationale`` describes why the classifier believes this is a v2
    pattern detection (always: "plugin path not present in live
    manifest scan").
    """

    user_extension_path: str
    referenced_path: str
    rationale: str


@dataclass(frozen=True)
class CatalogMatchFinding:
    """Class-3 finding: a user extension matches a catalog entry that
    is ``walkthrough_eligible: True``.

    Forwards the structured data T8 needs: which catalog entry, which
    user file, where in the bytes the match landed.
    """

    user_extension_path: str
    pattern_id: int
    span_start: int
    span_end: int
    matched_bytes: bytes
    surface_message: str


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationReport:
    """Aggregate verify output.

    Three lists, each one of the three error classes. Empty lists =
    nothing to flag for that class.
    """

    malformed: Tuple[MalformedFinding, ...] = ()
    missing_path: Tuple[MissingPathFinding, ...] = ()
    catalog_match: Tuple[CatalogMatchFinding, ...] = ()

    def is_clean(self) -> bool:
        """True iff no findings of any class."""
        return (
            not self.malformed
            and not self.missing_path
            and not self.catalog_match
        )


# ---------------------------------------------------------------------------
# Internal: derive plugin-surface expectations from live manifests
# ---------------------------------------------------------------------------


def _safe_parse(
    fn: Callable[..., Any],
    *,
    data: bytes,
    path: str,
    kind: str,
    malformed: List[MalformedFinding],
) -> Optional[Any]:
    """Run *fn* on *data*. On ``ParseError``, append a malformed finding
    and return None so the caller can keep going."""
    try:
        return fn(data, path=path)
    except ParseError as exc:
        malformed.append(
            MalformedFinding(
                path=path,
                kind=kind,
                message="{}: {}".format(type(exc).__name__, str(exc)),
            )
        )
        return None


def _derive_referenced_paths(events: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    """Collect every command-string fragment from the events mapping.

    Hook commands are shell strings; we extract the substrings that
    look like filesystem paths so the missing-path classifier can
    cross-check against actual on-disk paths. The classifier is
    deliberately loose: it accepts paths under the plugin install
    tree (``${CLAUDE_PLUGIN_ROOT}/...`` template form) and absolute
    paths, leaving everything else to the catalog matcher.
    """
    out: List[str] = []
    # Liberal regex for shell-quoted or unquoted path-ish tokens.
    pat = re.compile(
        r"\$\{CLAUDE_PLUGIN_ROOT\}/[^\s\"'`;|<>()]+"
        r"|\$\{ALIVE_PLUGIN_ROOT\}/[^\s\"'`;|<>()]+"
    )
    for groups in events.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if not isinstance(cmd, str):
                    continue
                for m in pat.finditer(cmd):
                    out.append(m.group(0))
    return out


# ---------------------------------------------------------------------------
# Internal: user-extension scanning (catalog match + missing-path)
# ---------------------------------------------------------------------------


def scan_user_extensions(
    *,
    user_extension_paths: Tuple[Path, ...],
    referenced_paths: Tuple[str, ...],
    read_provider: Callable[[Path], bytes],
    plugin_root_resolver: Optional[Callable[[str], Optional[Path]]] = None,
    world_root: Optional[Path] = None,
) -> Tuple[List[MissingPathFinding], List[CatalogMatchFinding]]:
    """Pure scan of user-extension content.

    Two passes per file:

    1. Catalog-match: every ``walkthrough_eligible: True`` regex is run
       only against files inside that catalog entry's declared
       ``target_path_glob`` scope (the catalog explicitly targets
       user-extension trees -- ``.alive/skills/``, ``.alive/rules/``,
       ``.alive/hooks/``). Each hit becomes a ``CatalogMatchFinding``.

    2. Missing-path: a templated plugin reference is flagged when it is
       NOT a member of the live-manifest roster (*referenced_paths*).
       This is the canonical R5 contract -- "is this path exposed by
       the live plugin surface?" -- so a stale plugin-local file that
       still ships on disk but is no longer wired through the manifest
       still produces a finding. When a *plugin_root_resolver* is also
       supplied, references known to the manifest are additionally
       cross-checked through *read_provider* as a secondary existence
       check (covering the edge case where the manifest references a
       file that was removed without the manifest being updated).

    Returned lists are deterministic in encounter order (caller-supplied
    iteration over *user_extension_paths*).
    """
    catalog_matches: List[CatalogMatchFinding] = []
    missing: List[MissingPathFinding] = []

    # Pre-compile catalog regexes once. The catalog is small.
    catalog_compiled = [
        (i, re.compile(retired_patterns.CATALOG[i].pattern_signature))
        for i in range(len(retired_patterns.CATALOG))
        if retired_patterns.CATALOG[i].walkthrough_eligible
    ]

    referenced = set(referenced_paths)
    template_pat = re.compile(
        r"\$\{CLAUDE_PLUGIN_ROOT\}/[^\s\"'`;|<>()]+"
        r"|\$\{ALIVE_PLUGIN_ROOT\}/[^\s\"'`;|<>()]+"
    )
    world_root_str = str(world_root) if world_root is not None else None

    for ext_path in user_extension_paths:
        try:
            data = read_provider(ext_path)
        except (FileNotFoundError, KeyError):
            # The path was in the snapshot roster but no longer present
            # under the read_provider's view. Skip silently -- a
            # disappeared file is not a user-extension issue.
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # Non-text user extension (rare): no catalog content
            # signatures can match, no template references can be
            # extracted; skip.
            continue

        # Catalog matches -- scope-filtered against each entry's
        # target_path_glob (a path that doesn't fall in the entry's
        # declared user-extension tree never produces a match, even if
        # its content happens to contain the regex signature).
        ext_path_str = str(ext_path)
        for pat_id, regex in catalog_compiled:
            entry = retired_patterns.CATALOG[pat_id]
            if not retired_patterns._path_in_target_scope(
                ext_path_str,
                entry.target_path_glob,
                world_root=world_root_str,
            ):
                continue
            for m in regex.finditer(text):
                start_chars, end_chars = m.span()
                start_bytes, end_bytes = compute_byte_offsets(
                    text, start_chars, end_chars,
                )
                catalog_matches.append(
                    CatalogMatchFinding(
                        user_extension_path=ext_path_str,
                        pattern_id=pat_id,
                        span_start=start_bytes,
                        span_end=end_bytes,
                        matched_bytes=text[start_chars:end_chars].encode(
                            "utf-8"
                        ),
                        surface_message=entry.surface_message,
                    )
                )

        # Missing-path classification: classify against the live
        # manifest roster (R5 canonical contract). A reference that is
        # NOT in *referenced* is reported as missing. When a resolver is
        # supplied AND the reference IS in the roster, we additionally
        # validate that the resolved path can actually be read -- this
        # covers the edge case where the manifest references a file
        # that was removed without the manifest being updated.
        for m in template_pat.finditer(text):
            ref = m.group(0)
            in_roster = ref in referenced
            if not in_roster:
                missing.append(
                    MissingPathFinding(
                        user_extension_path=ext_path_str,
                        referenced_path=ref,
                        rationale=(
                            "plugin path not present in live "
                            "manifest scan"
                        ),
                    )
                )
                continue
            if plugin_root_resolver is not None:
                resolved = plugin_root_resolver(ref)
                if resolved is None:
                    continue
                try:
                    read_provider(resolved)
                except (FileNotFoundError, KeyError):
                    missing.append(
                        MissingPathFinding(
                            user_extension_path=ext_path_str,
                            referenced_path=ref,
                            rationale=(
                                "plugin path in live manifest scan but "
                                "not readable through read_provider"
                            ),
                        )
                    )
    return missing, catalog_matches


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def verify(
    read_provider: Callable[[Path], bytes],
    world_root: Path,
    plugin_surface_paths: PluginSurfacePaths,
    *,
    plugin_root_resolver: Optional[Callable[[str], Optional[Path]]] = None,
) -> VerificationReport:
    """Run live-read verification.

    Parameters
    ----------
    read_provider
        Callable ``Path -> bytes``. Wired to ``Path.read_bytes`` on a
        real run (phase 11 reads disk fresh, NOT the start-of-run
        FileSnapshot, since cleanup/migrate phases mutate disk between
        snapshot capture and verify). Wired to
        ``PostStateOverlay.read_through(snapshot)`` on a dry-run.
    world_root
        Resolved world root. Threaded to the catalog matcher and used
        to anchor relative-path classification. Currently informational;
        accepted to match the contract published in the epic spec so
        future per-walnut splits land cleanly.
    plugin_surface_paths
        Dataclass populated by ``file_snapshot.py`` listing the plugin
        files verify must read. Verify NEVER references plugin path
        literals directly; everything arrives here.

    Returns
    -------
    VerificationReport
        Aggregate of the three error-class findings.
    """
    # ``world_root`` is threaded into the user-extension scope check so
    # catalog matches resolve absolute paths against the catalog's
    # ``<world>``-templated target globs.
    malformed: List[MalformedFinding] = []

    # 1. Parse hooks manifest.
    hooks_data: Optional[bytes] = None
    try:
        hooks_data = read_provider(plugin_surface_paths.hooks_json)
    except (FileNotFoundError, KeyError) as exc:
        malformed.append(
            MalformedFinding(
                path=str(plugin_surface_paths.hooks_json),
                kind="hooks",
                message="read failed: {}".format(exc),
            )
        )
    hooks_obj = None
    if hooks_data is not None:
        hooks_obj = _safe_parse(
            _hooks_parse.parse,
            data=hooks_data,
            path=str(plugin_surface_paths.hooks_json),
            kind="hooks",
            malformed=malformed,
        )

    # 2. Parse manifest.
    manifest_data: Optional[bytes] = None
    try:
        manifest_data = read_provider(plugin_surface_paths.plugin_json)
    except (FileNotFoundError, KeyError) as exc:
        malformed.append(
            MalformedFinding(
                path=str(plugin_surface_paths.plugin_json),
                kind="manifest",
                message="read failed: {}".format(exc),
            )
        )
    manifest_obj = None
    if manifest_data is not None:
        manifest_obj = _safe_parse(
            _manifest_parse.parse,
            data=manifest_data,
            path=str(plugin_surface_paths.plugin_json),
            kind="manifest",
            malformed=malformed,
        )

    # 3. Parse every skill manifest. Frontmatter only is the minimum
    # acceptable shape.
    for skill_path in plugin_surface_paths.skill_manifests:
        try:
            skill_data = read_provider(skill_path)
        except (FileNotFoundError, KeyError) as exc:
            malformed.append(
                MalformedFinding(
                    path=str(skill_path),
                    kind="skill_frontmatter",
                    message="read failed: {}".format(exc),
                )
            )
            continue
        _safe_parse(
            _skill_parse.parse,
            data=skill_data,
            path=str(skill_path),
            kind="skill_frontmatter",
            malformed=malformed,
        )

    # 4. Derive the live plugin-surface roster (referenced template
    # paths). Both hook command strings AND manifest path-bearing
    # fields contribute. The manifest's ``command_paths`` come straight
    # from optional commands/agents/hooks arrays the plugin schema
    # supports; merging them prevents false ``missing_path`` findings
    # for user references to a path the manifest exposes but the hook
    # manifest doesn't.
    referenced_set: Tuple[str, ...]
    referenced_collected: List[str] = []
    if hooks_obj is not None:
        referenced_collected.extend(
            _derive_referenced_paths(hooks_obj["events"])
        )
    if manifest_obj is not None:
        for raw in manifest_obj.get("command_paths", []):
            if isinstance(raw, str) and raw:
                referenced_collected.append(raw)
    # Stable sort-friendly: preserve encounter order, dedup.
    seen = set()
    referenced_dedup: List[str] = []
    for s in referenced_collected:
        if s in seen:
            continue
        seen.add(s)
        referenced_dedup.append(s)
    referenced_paths = tuple(referenced_dedup)

    # 5. User-extension scan -> missing_path[] + catalog_match[].
    missing_path: List[MissingPathFinding] = []
    catalog_match: List[CatalogMatchFinding] = []
    if plugin_surface_paths.user_extension_paths:
        missing_path, catalog_match = scan_user_extensions(
            user_extension_paths=plugin_surface_paths.user_extension_paths,
            referenced_paths=referenced_paths,
            read_provider=read_provider,
            plugin_root_resolver=plugin_root_resolver,
            world_root=world_root,
        )

    return VerificationReport(
        malformed=tuple(malformed),
        missing_path=tuple(missing_path),
        catalog_match=tuple(catalog_match),
    )
