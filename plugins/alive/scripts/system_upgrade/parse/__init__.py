"""``parse`` -- pure parsers for plugin-surface inputs.

Each submodule consumes ``bytes`` (already read by the caller via the
verify-side ``read_provider`` callable) and returns a structured shape
or raises a typed ``ParseError`` subclass. No filesystem access; no
references to plugin-code path literals; stdlib-only (R10).

The split into per-input modules (``hooks``, ``manifest``,
``skill_frontmatter``) lets the verify-side audit grep narrowly scope
forbidden literals: each parser's "what file am I parsing" knowledge
arrives via the ``PluginSurfacePaths`` dataclass populated by
``file_snapshot.py`` (the explicit, R5-allowlisted path-provider).

Stability
---------
Parsers tolerate every field actually used today; unknown fields are
preserved in the returned dict (``extras`` slot or pass-through). They
are deliberately permissive on harmless extras and strict on shape
mismatches that would mislead later phases (e.g., a hooks event whose
value is a JSON object instead of a list -> ``MalformedHooksError``).
"""

from __future__ import annotations


__all__ = (
    "ParseError",
    "MalformedJSONError",
    "MalformedHooksError",
    "MalformedManifestError",
    "MalformedFrontmatterError",
)


class ParseError(Exception):
    """Base class for every parser refusal. Carries the offending path."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


class MalformedJSONError(ParseError):
    """Raised when the bytes are not valid JSON."""


class MalformedHooksError(ParseError):
    """Raised when the hooks manifest parses but has an unexpected shape."""


class MalformedManifestError(ParseError):
    """Raised when ``plugin.json`` parses but has an unexpected shape."""


class MalformedFrontmatterError(ParseError):
    """Raised when SKILL.md frontmatter is missing/malformed."""
