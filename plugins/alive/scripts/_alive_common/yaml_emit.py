"""Hand-rolled stdlib YAML reader / writer for the v3 manifest schema (LD20).

Extracted verbatim from ``alive-p2p.py`` (T2 of fn-18). Public surface:

- ``write_manifest_yaml(manifest_dict, output_path)``
- ``read_manifest_yaml(path)``
- ``_validate_safe_string(value, field_name)``  (used by callers in
  ``alive-p2p.py:generate_manifest`` to reject strings that would
  corrupt the emitter; preserved at module scope and re-exported via a
  shim from ``alive-p2p.py``).

Scope: bundle / manifest YAML as the v3 P2P emitter handles today --
top-level scalars (always double-quoted), string lists, single-level
nested dicts (``source``, ``signature``), and lists of dicts
(``files``, ``substitutions_applied``). NO multi-line block style,
NO anchors, NO flow style.

This module is NOT used for system-upgrade record I/O. The upgrade
record family (resume markers, runstate entries, retroactive records,
final upgrade records, no-op records) carries nested
``surfaces[<name>].needs_retry[]``, ``planned_ops``,
``all_signals_raw`` and structured errors that this emitter cannot
round-trip. See ``system_upgrade/_record_codec.py`` for that family
(JSON-text-as-YAML via the YAML 1.2 superset trick).

Stdlib-only.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any, Dict, List, Optional


# Field order for the on-disk YAML manifest. The canonical (JSON) form
# re-sorts keys alphabetically; this order is for human readability of the
# generated YAML only.
_MANIFEST_FIELD_ORDER = (
    "format_version",
    "source_layout",
    "min_plugin_version",
    "created",
    "scope",
    "source",
    "sender",
    "description",
    "note",
    "exclusions_applied",
    "substitutions_applied",
    "bundles",
    "payload_sha256",
    "files",
    "encryption",
    "signature",
)


def _validate_safe_string(value, field_name):
    # type: (Any, str) -> None
    """Reject free-form strings that would corrupt the hand-rolled YAML emitter.

    The manifest YAML writer below emits single-line scalars only. Newlines,
    carriage returns, or unescaped double quotes inside ``description``,
    ``note``, ``sender``, and similar fields would either break the parser on
    the receive side or, worse, smuggle additional YAML keys into the manifest
    via injection. Reject them up front with a specific error.

    Backslashes are tolerated; the writer escapes them. Single quotes are
    tolerated since the writer always uses double quotes for scalars.
    """
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(
            "Field '{0}' must be a string, got {1}".format(
                field_name, type(value).__name__
            )
        )
    if "\n" in value or "\r" in value:
        raise ValueError(
            "Field '{0}' must be single-line (no newlines): {1!r}".format(
                field_name, value
            )
        )
    if '"' in value:
        raise ValueError(
            "Field '{0}' must not contain unescaped double quotes: {1!r}. "
            "Use single quotes or strip the value before passing it in.".format(
                field_name, value
            )
        )


# ---------------------------------------------------------------------------
# Stdlib-only manifest YAML reader / writer (LD20)
# ---------------------------------------------------------------------------
#
# The hand-rolled writer emits the exact subset of YAML the manifest schema
# uses: string scalars (always double-quoted for safety), string lists, one
# level of nested dicts (``source``, ``signature``), and a list of dicts
# (``files``, ``substitutions_applied``). No multi-line block style, no
# anchors, no flow style. Keys are emitted in ``_MANIFEST_FIELD_ORDER``; any
# unknown keys are appended in alphabetical order so forward-compat fields
# survive a round-trip.
#
# The reader is a regex-driven line scanner that handles the same subset and
# tolerates unknown top-level scalar fields (preserved in the dict for
# forward compat). Anything outside the subset raises ``ValueError`` so the
# parser cannot silently mis-parse a malformed file.

def _yaml_quote(value):
    # type: (str) -> str
    """Quote a string for the YAML writer (always double quotes).

    Backslashes and double quotes are escaped. Newlines are forbidden by
    ``_validate_safe_string`` upstream, but the escape is included for
    defence in depth.
    """
    if value is None:
        return '""'
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return '"{0}"'.format(s)


def _emit_scalar(key, value, indent):
    # type: (str, Any, int) -> str
    """Emit a single ``key: value`` line for the YAML writer."""
    pad = " " * indent
    if isinstance(value, bool):
        return "{0}{1}: {2}\n".format(pad, key, "true" if value else "false")
    if isinstance(value, (int, float)):
        return "{0}{1}: {2}\n".format(pad, key, value)
    if value is None:
        return "{0}{1}: \"\"\n".format(pad, key)
    return "{0}{1}: {2}\n".format(pad, key, _yaml_quote(value))


def _emit_string_list(key, items, indent):
    # type: (str, List[Any], int) -> str
    """Emit ``key:`` followed by ``- item`` lines for a list of scalars.

    Empty lists serialize as ``key: []`` so the field is preserved across a
    round trip without ambiguity.
    """
    pad = " " * indent
    if not items:
        return "{0}{1}: []\n".format(pad, key)
    out = "{0}{1}:\n".format(pad, key)
    for item in items:
        out += "{0}  - {1}\n".format(pad, _yaml_quote(item))
    return out


def _emit_dict_block(key, d, indent):
    # type: (str, Dict[str, Any], int) -> str
    """Emit a nested dict block (one level only).

    Used for ``source:``, ``signature:``, and any other future single-level
    nested dict. Keys are emitted in alphabetical order for stability.
    """
    pad = " " * indent
    out = "{0}{1}:\n".format(pad, key)
    for k in sorted(d.keys()):
        out += _emit_scalar(k, d[k], indent + 2)
    return out


def _emit_list_of_dicts(key, items, indent):
    # type: (str, List[Dict[str, Any]], int) -> str
    """Emit a list of dicts (e.g. ``files:`` and ``substitutions_applied:``).

    Each item is emitted as a ``- key: value`` block. Keys within an item are
    emitted in a fixed order: ``path`` first, then alphabetical for the rest
    so the path is the visual anchor for each entry.
    """
    pad = " " * indent
    if not items:
        return "{0}{1}: []\n".format(pad, key)
    out = "{0}{1}:\n".format(pad, key)
    for item in items:
        keys = list(item.keys())
        if "path" in keys:
            keys.remove("path")
            keys = ["path"] + sorted(keys)
        else:
            keys = sorted(keys)
        first = True
        for k in keys:
            if first:
                out += "{0}  - {1}: {2}\n".format(
                    pad, k, _yaml_quote(item[k]) if isinstance(item[k], str)
                    else item[k]
                )
                first = False
            else:
                out += "{0}    {1}: {2}\n".format(
                    pad, k, _yaml_quote(item[k]) if isinstance(item[k], str)
                    else item[k]
                )
    return out


def write_manifest_yaml(manifest_dict, output_path):
    # type: (Dict[str, Any], str) -> None
    """Serialize a manifest dict to YAML and write it atomically.

    Field order follows ``_MANIFEST_FIELD_ORDER`` for the known fields and
    appends any unknown top-level fields in alphabetical order so forward-
    compat additions survive a round trip.

    The writer dispatches per field type:
    - ``source`` and ``signature`` -> nested dict block
    - ``exclusions_applied`` and ``bundles`` -> string list
    - ``substitutions_applied`` and ``files`` -> list of dicts
    - everything else -> scalar
    """
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    out = ""
    written = set()
    known_order = list(_MANIFEST_FIELD_ORDER)
    extra_fields = sorted(
        k for k in manifest_dict.keys() if k not in known_order
    )
    for key in known_order + extra_fields:
        if key not in manifest_dict:
            continue
        val = manifest_dict[key]
        if key in ("source", "signature"):
            if isinstance(val, dict):
                out += _emit_dict_block(key, val, 0)
            else:
                out += _emit_scalar(key, val, 0)
        elif key in ("exclusions_applied", "bundles"):
            if isinstance(val, list):
                out += _emit_string_list(key, val, 0)
            else:
                out += _emit_scalar(key, val, 0)
        elif key in ("substitutions_applied", "files"):
            if isinstance(val, list):
                out += _emit_list_of_dicts(key, val, 0)
            else:
                out += _emit_scalar(key, val, 0)
        else:
            out += _emit_scalar(key, val, 0)
        written.add(key)

    # Atomic write so a crash mid-write does not leave a half-manifest behind.
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(output_path), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(out)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, output_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _yaml_unquote_strict(val):
    # type: (str) -> Any
    """Decode a single YAML scalar produced by the writer.

    Handles double-quoted strings (with backslash escapes), single-quoted
    strings, integer literals, float literals, ``true``/``false``, and bare
    strings. Used by ``read_manifest_yaml`` only.
    """
    if val == "" or val == "[]":
        return val
    if val.startswith('"') and val.endswith('"') and len(val) >= 2:
        s = val[1:-1]
        # Decode escapes in reverse order of how they were applied.
        s = s.replace("\\r", "\r").replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return s
    if val.startswith("'") and val.endswith("'") and len(val) >= 2:
        return val[1:-1]
    lower = val.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "~"):
        return None
    # Try int, then float, then bare string.
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def read_manifest_yaml(path):
    # type: (str) -> Dict[str, Any]
    """Parse a manifest YAML file (written by ``write_manifest_yaml``).

    The parser handles the exact subset the writer emits:
    - Top-level scalar lines (``key: value``)
    - Top-level ``key:`` followed by indented ``- item`` lines (string list)
    - Top-level ``key:`` followed by indented ``key: value`` pairs (nested
      dict, one level only)
    - Top-level ``key:`` followed by indented ``- key: value`` blocks (list
      of dicts)
    - ``key: []`` for empty lists

    Unknown top-level scalar fields are preserved in the result dict so
    forward-compat additions survive a round trip. Anything that does not
    match the subset raises ``ValueError`` -- silent mis-parsing would be
    worse than failing fast.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError("manifest not found: {0}".format(path))

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Strip trailing newline so the line counter is exact.
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    result = {}  # type: Dict[str, Any]
    i = 0
    n = len(lines)

    top_kv_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
    indented_dash_kv_re = re.compile(r"^(\s+)-\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
    indented_dash_scalar_re = re.compile(r"^(\s+)-\s+(.*)$")
    indented_kv_re = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")

    while i < n:
        line = lines[i]
        if line.strip() == "" or line.lstrip().startswith("#"):
            i += 1
            continue

        m = top_kv_re.match(line)
        if not m:
            raise ValueError(
                "Malformed manifest line {0}: {1!r}".format(i + 1, line)
            )

        key = m.group(1)
        raw_val = m.group(2).strip()

        if raw_val == "[]":
            result[key] = []
            i += 1
            continue

        if raw_val == "":
            # Block follows: nested dict OR list (string list / list of dicts).
            j = i + 1
            block_lines = []  # type: List[str]
            while j < n:
                nxt = lines[j]
                if nxt.strip() == "":
                    block_lines.append(nxt)
                    j += 1
                    continue
                # Indented? Then it belongs to this block.
                if nxt[:1] in (" ", "\t"):
                    block_lines.append(nxt)
                    j += 1
                    continue
                break

            if not block_lines or all(b.strip() == "" for b in block_lines):
                # Empty block -> empty value (treat as empty string).
                result[key] = ""
                i = j
                continue

            # Decide block type from the first non-blank child.
            first_nonblank = next(b for b in block_lines if b.strip() != "")
            stripped = first_nonblank.lstrip()
            if stripped.startswith("- "):
                # Either a string list or a list of dicts.
                # Inspect: is the first dash followed by ``word:`` or by a
                # bare scalar?
                dash_kv = indented_dash_kv_re.match(first_nonblank)
                if dash_kv:
                    # List of dicts.
                    items = _parse_list_of_dicts_block(block_lines, key, i)
                    result[key] = items
                else:
                    items = []  # type: List[Any]
                    for b in block_lines:
                        if b.strip() == "":
                            continue
                        dm = indented_dash_scalar_re.match(b)
                        if not dm:
                            raise ValueError(
                                "Malformed list item in '{0}' block: {1!r}".format(
                                    key, b
                                )
                            )
                        items.append(_yaml_unquote_strict(dm.group(2).strip()))
                    result[key] = items
            else:
                # Nested dict block.
                nested = {}  # type: Dict[str, Any]
                for b in block_lines:
                    if b.strip() == "":
                        continue
                    km = indented_kv_re.match(b)
                    if not km:
                        raise ValueError(
                            "Malformed nested dict line in '{0}' block: {1!r}".format(
                                key, b
                            )
                        )
                    nested[km.group(2)] = _yaml_unquote_strict(km.group(3).strip())
                result[key] = nested

            i = j
            continue

        # Inline scalar.
        result[key] = _yaml_unquote_strict(raw_val)
        i += 1

    return result


def _parse_list_of_dicts_block(block_lines, parent_key, start_index):
    # type: (List[str], str, int) -> List[Dict[str, Any]]
    """Parse a list-of-dicts block produced by ``_emit_list_of_dicts``.

    Each entry begins with ``  - key: value`` and is followed by zero or more
    ``    key: value`` continuation lines (deeper indent). The function
    builds a list of dicts and raises ``ValueError`` on any line that does
    not match the expected pattern.
    """
    items = []  # type: List[Dict[str, Any]]
    current = None  # type: Optional[Dict[str, Any]]
    dash_re = re.compile(r"^(\s+)-\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
    cont_re = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")

    dash_indent = None  # type: Optional[int]

    for raw in block_lines:
        if raw.strip() == "":
            continue
        dash = dash_re.match(raw)
        if dash:
            if current is not None:
                items.append(current)
            indent_len = len(dash.group(1))
            if dash_indent is None:
                dash_indent = indent_len
            elif indent_len != dash_indent:
                raise ValueError(
                    "Inconsistent dash indent in '{0}' list (line {1})".format(
                        parent_key, start_index + 1
                    )
                )
            current = {dash.group(2): _yaml_unquote_strict(dash.group(3).strip())}
            continue
        cont = cont_re.match(raw)
        if cont and current is not None:
            indent_len = len(cont.group(1))
            if dash_indent is None or indent_len <= dash_indent:
                raise ValueError(
                    "Continuation line not deeper than dash in '{0}' list "
                    "(line {1}): {2!r}".format(
                        parent_key, start_index + 1, raw
                    )
                )
            current[cont.group(2)] = _yaml_unquote_strict(cont.group(3).strip())
            continue
        raise ValueError(
            "Malformed entry in '{0}' list (line {1}): {2!r}".format(
                parent_key, start_index + 1, raw
            )
        )

    if current is not None:
        items.append(current)
    return items


__all__ = (
    "write_manifest_yaml",
    "read_manifest_yaml",
)
