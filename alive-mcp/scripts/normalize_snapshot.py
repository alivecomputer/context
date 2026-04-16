#!/usr/bin/env python3
"""Normalize a raw MCP Inspector CLI response for golden-fixture use.

Reads the Inspector's JSON stdout on stdin, sorts top-level list
elements by a stable identity key (``name`` for tools and prompts,
``uri`` for resources), sorts every dict key recursively, pretty-prints
with two-space indent, and writes to stdout with a trailing newline.

Usage
-----
    python3 normalize_snapshot.py <method>

    where <method> is one of:
        tools/list      — sorts tools[] by .name
        resources/list  — sorts resources[] by .uri
        prompts/list    — sorts prompts[] by .name

Exit codes
----------
    0  success
    1  missing/invalid method argument
    2  stdin was empty or not valid JSON

This helper is the single source of truth for snapshot normalization.
``scripts/run-inspector-snapshot.sh`` invokes it after running the
Inspector; ``tests/test_contracts.py`` invokes it when regenerating an
in-memory snapshot to diff against the committed golden. Keeping
normalization in one place eliminates drift between "how the golden was
built" and "how the test compares".
"""
from __future__ import annotations

import json
import sys
from typing import Any


# Identity keys for stable list ordering. Each MCP `*_list` response
# has exactly one list field; we sort that list by the item's natural
# primary key so the snapshot is byte-stable across runs.
_SORT_KEYS: dict[str, tuple[str, str]] = {
    "tools/list": ("tools", "name"),
    "resources/list": ("resources", "uri"),
    "prompts/list": ("prompts", "name"),
}


def normalize(raw_text: str, method: str) -> str:
    """Parse ``raw_text``, canonicalize, return pretty-printed JSON.

    Raises
    ------
    ValueError
        If ``method`` is unknown or ``raw_text`` is not valid JSON.
    """
    if method not in _SORT_KEYS:
        raise ValueError(f"unsupported method: {method!r}")
    if not raw_text.strip():
        raise ValueError("raw input is empty")
    try:
        data: Any = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"input is not valid JSON (server print contamination?): {exc}"
        ) from exc

    list_key, item_key = _SORT_KEYS[method]
    if isinstance(data, dict) and isinstance(data.get(list_key), list):
        data[list_key] = sorted(
            data[list_key],
            # Defensive get: some items may lack the identity key in a
            # future schema change; fall back to the item's repr so
            # ordering stays deterministic even then.
            key=lambda item: item.get(item_key, "") if isinstance(item, dict) else repr(item),
        )

    # ``sort_keys=True`` recurses through every nested dict, so we do
    # NOT need a separate recursive sort helper. ``ensure_ascii=False``
    # preserves Unicode identifiers verbatim (walnut paths with
    # accents, etc.) — the file is UTF-8 either way.
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write(
            "usage: normalize_snapshot.py <tools/list|resources/list|prompts/list>\n"
        )
        return 1
    method = argv[1]
    raw_text = sys.stdin.read()
    try:
        normalized = normalize(raw_text, method)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(normalized)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
