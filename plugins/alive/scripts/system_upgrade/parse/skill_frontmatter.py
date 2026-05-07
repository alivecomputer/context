"""Parser for SKILL.md YAML-style frontmatter.

Stdlib-only (R10): no ``yaml`` / ``ruamel`` imports. The frontmatter
is a small, well-formed key/value subset (`key: value` per line, with
optional double-quoted values that may carry colons) so a hand-rolled
line parser is sufficient and avoids forbidden third-party deps.

Returns:

::

    {
        "name": str,
        "description": str | None,
        "user_invocable": bool | None,
        "extras": {<unknown-key>: <str>, ...},
        "body_offset": int,   # byte offset where the post-frontmatter
                              # body starts; useful for downstream tools
    }

The required field is ``name``; everything else is optional. Refusal
classes (``MalformedFrontmatterError``):

* the file does not begin with a ``---`` line (frontmatter absent);
* the closing ``---`` line is missing;
* a body line is not in ``key: value`` form.
"""

from __future__ import annotations

from typing import Any, Dict

from . import MalformedFrontmatterError


__all__ = ("parse",)


_FRONTMATTER_DELIM = "---"


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


_BOOL_TRUE = frozenset(("true",))
_BOOL_FALSE = frozenset(("false",))


def _coerce_bool(s: str, *, path: str = "") -> bool:
    """Return ``True``/``False`` for canonical YAML boolean literals.

    Strict: anything other than ``true``/``false`` raises
    ``MalformedFrontmatterError``. The output type for ``user-invocable``
    is declared ``bool | None``; a permissive coercion that returns the
    original string would silently violate that contract, so we refuse
    explicitly. The set of accepted strings is small by design --
    ``yes``/``no`` etc. are not part of the convention used by the
    rest of the plugin manifests.
    """
    norm = s.strip().lower()
    if norm in _BOOL_TRUE:
        return True
    if norm in _BOOL_FALSE:
        return False
    raise MalformedFrontmatterError(
        "expected 'true' or 'false' for boolean field; got {!r}".format(s),
        path=path,
    )


def parse(data: bytes, *, path: str = "") -> Dict[str, Any]:
    """Parse the YAML-style frontmatter of a SKILL.md.

    Raises ``MalformedFrontmatterError`` when the document does not open
    with ``---``, when the closing fence is absent, or when a body line
    is not in ``key: value`` form. Required field: ``name``.
    """
    # Track the BOM in bytes so body_offset stays accurate for
    # BOM-prefixed files. The decoded text drops the BOM character.
    bom = b"\xef\xbb\xbf"
    bom_bytes = 0
    if data.startswith(bom):
        data_no_bom = data[len(bom):]
        bom_bytes = len(bom)
    else:
        data_no_bom = data

    try:
        text = data_no_bom.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedFrontmatterError(
            "skill bytes are not valid UTF-8: {}".format(exc), path=path
        ) from exc

    # First content line MUST be ``---``.
    lines = text.splitlines(keepends=True)
    if not lines:
        raise MalformedFrontmatterError("file is empty", path=path)
    if lines[0].strip() != _FRONTMATTER_DELIM:
        raise MalformedFrontmatterError(
            "missing opening frontmatter delimiter ('---')", path=path
        )

    # Collect lines until the next ``---``. Track byte consumption on
    # the raw byte stream (after BOM strip) by encoding each line's
    # decoded text -- safe because UTF-8 round-trips losslessly here.
    body_lines = []
    closed = False
    consumed_bytes = len(lines[0].encode("utf-8"))
    for raw in lines[1:]:
        consumed_bytes += len(raw.encode("utf-8"))
        if raw.strip() == _FRONTMATTER_DELIM:
            closed = True
            break
        body_lines.append(raw)
    if not closed:
        raise MalformedFrontmatterError(
            "missing closing frontmatter delimiter ('---')", path=path
        )

    out: Dict[str, Any] = {
        "name": None,
        "description": None,
        "user_invocable": None,
        "extras": {},
        "body_offset": bom_bytes + consumed_bytes,
    }
    for raw in body_lines:
        line = raw.rstrip("\n").rstrip("\r")
        if not line.strip():
            continue
        # Comments are allowed, mirroring the convention used elsewhere
        # in plugin manifests; keep them out of the structured shape.
        if line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise MalformedFrontmatterError(
                "frontmatter line is not 'key: value': {!r}".format(line),
                path=path,
            )
        key, _, val = line.partition(":")
        key = key.strip()
        if not key:
            raise MalformedFrontmatterError(
                "frontmatter line has empty key: {!r}".format(line), path=path
            )
        cleaned = _strip_quotes(val)
        if key == "name":
            out["name"] = cleaned
        elif key == "description":
            out["description"] = cleaned
        elif key == "user-invocable":
            out["user_invocable"] = _coerce_bool(cleaned, path=path)
        else:
            out["extras"][key] = cleaned

    if not out["name"]:
        raise MalformedFrontmatterError(
            "missing required 'name' field in frontmatter", path=path
        )
    return out
