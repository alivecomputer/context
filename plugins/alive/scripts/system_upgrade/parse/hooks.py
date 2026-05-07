"""Parser for the plugin's hooks manifest.

Consumes ``bytes`` (read by the caller), returns a normalized shape:

::

    {
        "description": str | None,
        "events": {
            "<EventName>": [
                {
                    "matcher": str | None,
                    "hooks": [
                        {"type": str, "command": str, "timeout": int | None},
                        ...
                    ],
                },
                ...
            ],
            ...
        },
        "command_paths": [str, ...],   # every command string seen,
                                       # in encounter order; used by the
                                       # verify-side missing-path classifier
        "extras": {<top-level-unknown>: <value>, ...},
    }

The parser is intentionally permissive on extra top-level keys (these
land in ``extras``) and strict on the shape of the ``hooks`` mapping
because verify dispatches on its structure.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import MalformedHooksError, MalformedJSONError


__all__ = ("parse",)


def _decode(data: bytes, *, path: str = "") -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedJSONError(
            "hooks bytes are not valid UTF-8: {}".format(exc), path=path
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedJSONError(
            "hooks bytes are not valid JSON: {}".format(exc), path=path
        ) from exc


def _coerce_hook_entry(entry: Any, *, path: str, where: str) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        raise MalformedHooksError(
            "{}: hook entry must be an object; got {}".format(
                where, type(entry).__name__
            ),
            path=path,
        )
    htype = entry.get("type")
    cmd = entry.get("command")
    if not isinstance(htype, str) or not htype:
        raise MalformedHooksError(
            "{}: hook entry missing string 'type'".format(where), path=path
        )
    if not isinstance(cmd, str) or not cmd:
        raise MalformedHooksError(
            "{}: hook entry missing string 'command'".format(where), path=path
        )
    timeout = entry.get("timeout")
    # ``bool`` is a subclass of ``int`` in Python, so an `isinstance` check
    # alone would let ``true`` / ``false`` pass as a "valid" timeout.
    # Reject booleans explicitly to keep the type contract honest.
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int)
    ):
        raise MalformedHooksError(
            "{}: hook entry 'timeout' must be int or absent".format(where),
            path=path,
        )
    return {"type": htype, "command": cmd, "timeout": timeout}


def parse(data: bytes, *, path: str = "") -> Dict[str, Any]:
    """Parse the hooks manifest bytes.

    Raises ``MalformedJSONError`` on JSON-decode failure;
    ``MalformedHooksError`` on shape mismatches that would mislead
    downstream verification.
    """
    obj = _decode(data, path=path)
    if not isinstance(obj, dict):
        raise MalformedHooksError(
            "top-level value must be a JSON object; got {}".format(
                type(obj).__name__
            ),
            path=path,
        )

    description: Optional[str] = None
    if "description" in obj:
        desc = obj["description"]
        if desc is not None and not isinstance(desc, str):
            raise MalformedHooksError(
                "'description' must be a string when present", path=path
            )
        description = desc

    events_in = obj.get("hooks", {})
    if not isinstance(events_in, dict):
        raise MalformedHooksError(
            "'hooks' must be an object mapping event-name -> [matcher group, ...]",
            path=path,
        )

    events: Dict[str, List[Dict[str, Any]]] = {}
    command_paths: List[str] = []
    for event_name, group_list in events_in.items():
        if not isinstance(event_name, str):
            raise MalformedHooksError(
                "hooks key must be a string event-name", path=path
            )
        if not isinstance(group_list, list):
            raise MalformedHooksError(
                "{}: value must be a list of matcher groups; got {}".format(
                    event_name, type(group_list).__name__
                ),
                path=path,
            )
        normalized_groups: List[Dict[str, Any]] = []
        for idx, group in enumerate(group_list):
            if not isinstance(group, dict):
                raise MalformedHooksError(
                    "{}[{}]: matcher group must be an object".format(
                        event_name, idx
                    ),
                    path=path,
                )
            matcher = group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                raise MalformedHooksError(
                    "{}[{}]: 'matcher' must be a string when present".format(
                        event_name, idx
                    ),
                    path=path,
                )
            hooks_list = group.get("hooks", [])
            if not isinstance(hooks_list, list):
                raise MalformedHooksError(
                    "{}[{}].hooks must be a list".format(event_name, idx),
                    path=path,
                )
            coerced: List[Dict[str, Any]] = []
            for jdx, item in enumerate(hooks_list):
                where = "{}[{}].hooks[{}]".format(event_name, idx, jdx)
                entry = _coerce_hook_entry(item, path=path, where=where)
                coerced.append(entry)
                command_paths.append(entry["command"])
            normalized_groups.append({"matcher": matcher, "hooks": coerced})
        events[event_name] = normalized_groups

    extras = {k: v for k, v in obj.items() if k not in ("description", "hooks")}
    return {
        "description": description,
        "events": events,
        "command_paths": command_paths,
        "extras": extras,
    }
