"""System-upgrade record I/O codec (T2 of fn-18).

The system-upgrade record family -- final upgrade records, resume
markers, runstate entries, retroactive records, and no-op records --
carries nested structure (``surfaces[<name>].needs_retry[]``,
``planned_ops``, ``all_signals_raw``, structured errors) that the
hand-rolled bundle/manifest YAML emitter at
``_alive_common/yaml_emit.py`` cannot round-trip. Instead of teaching
the manifest emitter a richer subset (and risking regressions on the
P2P side), upgrade records use the YAML 1.2 superset trick:
``json.dumps(obj, indent=2, sort_keys=True)`` produces text that is
both valid JSON and valid YAML 1.2, so any YAML-1.2 reader can parse
it -- and ``json.loads`` reads it back exactly.

Public API
----------

- ``read(path)``        -- parse a record file, returning the dict.
- ``write_atomic(path, obj)`` -- serialize the dict and write atomically.

Files written by this codec carry the ``.yaml`` extension (matching
the upgrade-record naming convention) but the on-disk content is JSON
text. Round-trip is deterministic across Python builds because:

- ``sort_keys=True`` enforces canonical key order;
- ``indent=2`` enforces canonical indent;
- ``ensure_ascii=False`` keeps non-ASCII strings readable.

Stdlib-only (R10 -- no PyYAML, no ruamel.yaml).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

# Atomic-write primitive lives in ``_atomic_io``; reach for it via the
# scripts/-on-sys.path import that the rest of the package uses. The
# callsite is single-purpose so we keep the import local-style minimal.
from _atomic_io import atomic_write_text


__all__ = ("read", "write_atomic")


def write_atomic(path, obj):
    # type: (str, Dict[str, Any]) -> None
    """Atomically write ``obj`` to ``path`` as JSON-text-as-YAML.

    Output format:
        ``json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)``
        followed by a trailing newline.

    The trailing newline matches the convention used by the rest of
    the upgrade-record toolchain (every other writer terminates with
    ``\\n``). Idempotent re-emit: ``read(write_atomic(read(path)))`` is
    byte-equal to the prior file.
    """
    text = (
        json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    atomic_write_text(os.fspath(path), text, mode=0o644)


def read(path):
    # type: (str) -> Dict[str, Any]
    """Parse a record file and return the dict.

    The file is expected to contain JSON-text-as-YAML written by
    ``write_atomic``. Parsing uses ``json.loads`` so a malformed file
    surfaces as ``json.JSONDecodeError`` with line/column info -- which
    is more actionable than the generic exception a YAML-only parser
    would emit. ``FileNotFoundError`` propagates unchanged.
    """
    with open(os.fspath(path), "r", encoding="utf-8") as f:
        content = f.read()
    return json.loads(content)
