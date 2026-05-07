"""Parser for the plugin's manifest (the JSON descriptor).

Consumes ``bytes``, returns:

::

    {
        "name": str,
        "version": str,
        "description": str | None,
        "author": dict | None,
        "homepage": str | None,
        "repository": str | None,
        "license": str | None,
        "command_paths": [str, ...],   # path-bearing references derived
                                       # from optional commands/agents/
                                       # hooks fields, in encounter order
        "extras": {<unknown-keys>: <value>, ...},
    }

Only ``name`` and ``version`` are required; everything else is optional.
The parser is permissive on extra top-level keys (preserved in
``extras``) and strict on the two required strings because the rest of
the upgrade pipeline keys off them.

Path-bearing fields
-------------------
The Claude Code plugin schema lets manifests declare extra surface
arrays (``commands``, ``agents``, ``hooks``) that reference plugin-local
paths. ``command_paths`` aggregates every path-string it can find under
those arrays, in encounter order, so verify can merge them into the
live-manifest roster used by the missing-path classifier. Plugins that
do not use these arrays (the alive plugin today) produce an empty list.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from . import MalformedJSONError, MalformedManifestError


__all__ = ("parse",)


_REQUIRED = ("name", "version")
_KNOWN = (
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "commands",
    "agents",
    "hooks",
)
_SURFACE_ARRAY_FIELDS = ("commands", "agents", "hooks")
# Field names that carry path strings inside surface-array entries.
_PATH_KEYS = ("command", "path", "script", "source", "file")


def _iter_path_strings(obj: Any) -> Iterable[str]:
    """Yield every plausible path-string under *obj*.

    Walks lists / dicts recursively. Strings under any of the known
    *_PATH_KEYS* (``command``, ``path``, ``script``, ``source``,
    ``file``) are yielded directly. Plain strings inside lists are
    yielded too (catches the common ``["bin/x", "bin/y"]`` shape).

    Non-string scalars and unknown structure are silently skipped --
    the parser stays permissive so a future schema extension does not
    break the upgrade pipeline.
    """
    if isinstance(obj, str):
        yield obj
        return
    if isinstance(obj, list):
        for item in obj:
            yield from _iter_path_strings(item)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and key in _PATH_KEYS:
                yield value
            else:
                yield from _iter_path_strings(value)


def parse(data: bytes, *, path: str = "") -> Dict[str, Any]:
    """Parse the manifest bytes.

    Raises ``MalformedJSONError`` on JSON-decode failure;
    ``MalformedManifestError`` when required fields are missing or have
    the wrong type.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedJSONError(
            "manifest bytes are not valid UTF-8: {}".format(exc), path=path
        ) from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedJSONError(
            "manifest bytes are not valid JSON: {}".format(exc), path=path
        ) from exc

    if not isinstance(obj, dict):
        raise MalformedManifestError(
            "top-level value must be a JSON object; got {}".format(
                type(obj).__name__
            ),
            path=path,
        )

    for key in _REQUIRED:
        val = obj.get(key)
        if not isinstance(val, str) or not val:
            raise MalformedManifestError(
                "missing or non-string required field: {!r}".format(key),
                path=path,
            )

    description = obj.get("description")
    if description is not None and not isinstance(description, str):
        raise MalformedManifestError(
            "'description' must be a string when present", path=path
        )

    author = obj.get("author")
    if author is not None and not isinstance(author, dict):
        raise MalformedManifestError(
            "'author' must be an object when present", path=path
        )

    for key in ("homepage", "repository", "license"):
        val = obj.get(key)
        if val is not None and not isinstance(val, str):
            raise MalformedManifestError(
                "{!r} must be a string when present".format(key), path=path
            )

    # Aggregate path-bearing references from the optional surface
    # arrays. We are permissive about shape: anything that walks the
    # generic _iter_path_strings discovery counts.
    command_paths: List[str] = []
    for field_name in _SURFACE_ARRAY_FIELDS:
        if field_name not in obj:
            continue
        value = obj[field_name]
        if not isinstance(value, list):
            raise MalformedManifestError(
                "{!r} must be a list when present".format(field_name),
                path=path,
            )
        for s in _iter_path_strings(value):
            command_paths.append(s)

    extras = {k: v for k, v in obj.items() if k not in _KNOWN}
    return {
        "name": obj["name"],
        "version": obj["version"],
        "description": description,
        "author": author,
        "homepage": obj.get("homepage"),
        "repository": obj.get("repository"),
        "license": obj.get("license"),
        "command_paths": command_paths,
        "extras": extras,
    }
