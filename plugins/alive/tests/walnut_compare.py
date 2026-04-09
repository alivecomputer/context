#!/usr/bin/env python3
"""Canonical walnut comparator for round-trip tests (LD13 / fn-7-7cw.11).

Implements ``walnut_equal(a_path, b_path, **opts) -> Tuple[bool, List[str]]``
per the LD13 contract:

Default ignored paths::

    _kernel/now.json
    _kernel/_generated/*
    _kernel/imports.json

Default ignored frontmatter fields in ``log.md``::

    last-entry, entry-count, updated

Default ignored ``log.md`` body: first ``ignore_log_entries`` entries
(parameterisable, defaults to 0).

Default ignored YAML scalar fields in bundle manifests::

    created, received_at, updated, *_at timestamps

Normalisation applied to all text comparisons:
- CRLF -> LF line endings
- Trailing whitespace stripped per line
- Trailing blank lines stripped before final-newline normalisation

Strict (byte-exact after normalisation):
- ``key.md``, ``insights.md``, ``log.md`` body after ignored entries
- ``tasks.json``, ``completed.json`` (deep JSON compare)
- bundle manifests (after ignored fields)
- draft files, ``raw/`` trees, live context files

Encryption: callers compare DECRYPTED payloads. Signature: callers verify
separately.

Returns ``(match, differences)``. ``differences`` is empty when ``match`` is
True. Tests use ``assert_walnut_equal(a, b, **opts)`` which pretty-prints the
diff on failure.

Stdlib only. No external test framework.
"""

import json
import os
import re
import unittest
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Defaults (LD13)
# ---------------------------------------------------------------------------

DEFAULT_IGNORE_PATHS = (
    "_kernel/now.json",
    "_kernel/imports.json",
)

DEFAULT_IGNORE_PATH_PREFIXES = (
    "_kernel/_generated/",
)

DEFAULT_LOG_FRONTMATTER_IGNORE = (
    "last-entry",
    "entry-count",
    "updated",
)

DEFAULT_MANIFEST_IGNORE = (
    "created",
    "received_at",
    "updated",
)

# Manifest fields whose key suffix matches ``*_at`` are ignored as well.
_MANIFEST_TIMESTAMP_SUFFIX = "_at"


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _normalise_text(text):
    # type: (str) -> str
    """Apply LD13 normalisation: CRLF->LF, strip trailing whitespace per line,
    strip trailing blank lines, ensure exactly one trailing newline if any
    content remains.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _read_text(path):
    # type: (str) -> str
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _read_bytes(path):
    # type: (str) -> bytes
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Path collection / classification
# ---------------------------------------------------------------------------


def _collect_files(root):
    # type: (str) -> Dict[str, str]
    """Walk a tree and return {posix_relpath: abs_path} for all regular files."""
    result = {}  # type: Dict[str, str]
    root_abs = os.path.abspath(root)
    for dirpath, dirs, files in os.walk(root_abs):
        # Sort for stable iteration in tests, even though we're populating a dict.
        dirs.sort()
        files.sort()
        for fname in files:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root_abs).replace(os.sep, "/")
            result[rel] = full
    return result


def _is_ignored(rel_path, ignore_patterns):
    # type: (str, Set[str]) -> bool
    """Match LD13 default + caller-supplied ignore patterns. Patterns are
    POSIX-normalised; trailing ``/`` denotes a directory prefix.
    """
    if rel_path in DEFAULT_IGNORE_PATHS:
        return True
    for prefix in DEFAULT_IGNORE_PATH_PREFIXES:
        if rel_path.startswith(prefix):
            return True
    if not ignore_patterns:
        return False
    for pat in ignore_patterns:
        if pat == rel_path:
            return True
        if pat.endswith("/") and rel_path.startswith(pat):
            return True
    return False


# ---------------------------------------------------------------------------
# Domain-specific comparators
# ---------------------------------------------------------------------------


_LOG_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_LOG_ENTRY_RE = re.compile(r"^## .+?(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def _parse_log_md(content, drop_top_entries=0):
    # type: (str, int) -> Tuple[Dict[str, str], str]
    """Split a log.md into ({frontmatter_fields}, body_after_dropped_entries).

    Filters frontmatter fields per LD13 defaults. ``drop_top_entries`` is the
    number of leading ``## `` entries to drop from the body before
    normalisation. Used by callers comparing a sender-side walnut against a
    receiver-side walnut: pass the count of receiver-injected import entries
    so the comparator skips them when matching the rest of the log body.
    """
    fields = {}  # type: Dict[str, str]
    body = content
    m = _LOG_FRONTMATTER_RE.match(content)
    if m:
        block = m.group(1)
        body = content[m.end():]
        for raw_line in block.split("\n"):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key in DEFAULT_LOG_FRONTMATTER_IGNORE:
                continue
            fields[key] = value
    if drop_top_entries > 0:
        # Find the first ``drop_top_entries`` ``## `` entries and drop them.
        entries = list(_LOG_ENTRY_RE.finditer(body))
        if entries and drop_top_entries < len(entries):
            cut_at = entries[drop_top_entries].start()
            preamble = body[:entries[0].start()]
            kept = body[cut_at:]
            body = preamble + kept
        elif entries and drop_top_entries >= len(entries):
            body = body[:entries[0].start()]
    return fields, _normalise_text(body)


_MANIFEST_FIELD_RE = re.compile(r"^([a-zA-Z0-9_]+)\s*:\s*(.*)$")


def _parse_simple_yaml(content):
    # type: (str) -> Dict[str, str]
    """Parse top-level scalar key-value pairs from a YAML manifest. Lines
    starting with ``#`` and empty lines are ignored. Indented continuation
    blocks are skipped (we only care about scalar fields for comparison).
    """
    out = {}  # type: Dict[str, str]
    for raw_line in content.split("\n"):
        if not raw_line or raw_line.startswith("#"):
            continue
        if raw_line.startswith(" ") or raw_line.startswith("\t"):
            # Indented continuation; skip for scalar comparison purposes.
            continue
        m = _MANIFEST_FIELD_RE.match(raw_line)
        if not m:
            continue
        out[m.group(1)] = m.group(2).strip()
    return out


def _filter_manifest_fields(fields, strict_timestamps):
    # type: (Dict[str, str], bool) -> Dict[str, str]
    """Drop default-ignored timestamp fields unless strict_timestamps is True."""
    if strict_timestamps:
        return dict(fields)
    filtered = {}  # type: Dict[str, str]
    for key, value in fields.items():
        if key in DEFAULT_MANIFEST_IGNORE:
            continue
        if key.endswith(_MANIFEST_TIMESTAMP_SUFFIX):
            continue
        filtered[key] = value
    return filtered


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def walnut_equal(
    a_path,                       # type: str
    b_path,                       # type: str
    ignore_log_entries=0,         # type: int
    ignore_patterns=None,         # type: Optional[List[str]]
    strict_timestamps=False,      # type: bool
):
    # type: (...) -> Tuple[bool, List[str]]
    """Compare two walnut trees per LD13. Returns (match, differences).

    Default ignored paths:
        _kernel/now.json, _kernel/imports.json, _kernel/_generated/*

    Default ignored log.md frontmatter fields:
        last-entry, entry-count, updated

    Default ignored manifest YAML fields:
        created, received_at, updated, *_at suffixes

    Parameters:
        ignore_log_entries: drop the first N ``## `` entries from log.md
            comparison (used to skip the receiver-side import log line).
        ignore_patterns: extra path or path-prefix patterns to ignore.
            Trailing ``/`` marks a directory prefix.
        strict_timestamps: when True, manifest timestamp fields ARE compared.
    """
    a_path = os.path.abspath(a_path)
    b_path = os.path.abspath(b_path)
    if not os.path.isdir(a_path):
        return (False, ["a is not a directory: {0}".format(a_path)])
    if not os.path.isdir(b_path):
        return (False, ["b is not a directory: {0}".format(b_path)])

    extra_ignore = set(ignore_patterns or [])
    a_files = {
        rel: full for rel, full in _collect_files(a_path).items()
        if not _is_ignored(rel, extra_ignore)
    }
    b_files = {
        rel: full for rel, full in _collect_files(b_path).items()
        if not _is_ignored(rel, extra_ignore)
    }

    diffs = []  # type: List[str]

    only_in_a = sorted(set(a_files.keys()) - set(b_files.keys()))
    only_in_b = sorted(set(b_files.keys()) - set(a_files.keys()))
    for rel in only_in_a:
        diffs.append("only in a: {0}".format(rel))
    for rel in only_in_b:
        diffs.append("only in b: {0}".format(rel))

    common = sorted(set(a_files.keys()) & set(b_files.keys()))
    for rel in common:
        a_file = a_files[rel]
        b_file = b_files[rel]
        if not _files_match(rel, a_file, b_file, ignore_log_entries,
                            strict_timestamps, diffs):
            # diff message already appended
            pass

    return (not diffs, diffs)


def _files_match(rel, a_file, b_file, ignore_log_entries, strict_timestamps,
                 diffs):
    # type: (str, str, str, int, bool, List[str]) -> bool
    """Compare a single file pair using the right strategy for its kind.
    Appends to ``diffs`` on mismatch and returns False.
    """
    basename = rel.split("/")[-1]

    # log.md gets special treatment so the receiver-side import entry can
    # be ignored. The ``ignore_log_entries`` count is treated asymmetrically:
    # we drop that many top entries from the SECOND argument (assumed
    # receiver) only. Sender-side walnuts pass through untouched. This makes
    # round-trip comparisons trivial when the receiver injects N import
    # entries above an otherwise byte-equal log body.
    if rel == "_kernel/log.md" or rel.endswith("/_kernel/log.md"):
        a_text = _read_text(a_file)
        b_text = _read_text(b_file)
        a_fields, a_body = _parse_log_md(a_text, drop_top_entries=0)
        b_fields, b_body = _parse_log_md(b_text, drop_top_entries=ignore_log_entries)
        if a_fields != b_fields:
            diffs.append(
                "{0}: log frontmatter mismatch: a={1} b={2}".format(
                    rel, sorted(a_fields.items()), sorted(b_fields.items()),
                )
            )
            return False
        if a_body != b_body:
            diffs.append(
                "{0}: log body mismatch (after ignoring {1} entries)".format(
                    rel, ignore_log_entries,
                )
            )
            return False
        return True

    # Bundle manifests: tolerate timestamp drift unless strict.
    if basename == "context.manifest.yaml":
        a_text = _normalise_text(_read_text(a_file))
        b_text = _normalise_text(_read_text(b_file))
        a_fields = _filter_manifest_fields(_parse_simple_yaml(a_text),
                                           strict_timestamps)
        b_fields = _filter_manifest_fields(_parse_simple_yaml(b_text),
                                           strict_timestamps)
        if a_fields != b_fields:
            diffs.append(
                "{0}: manifest scalar fields mismatch: a={1} b={2}".format(
                    rel, sorted(a_fields.items()), sorted(b_fields.items()),
                )
            )
            return False
        return True

    # JSON files (tasks.json, completed.json): deep JSON compare.
    if basename in ("tasks.json", "completed.json"):
        try:
            with open(a_file, "r", encoding="utf-8") as f:
                a_data = json.load(f)
            with open(b_file, "r", encoding="utf-8") as f:
                b_data = json.load(f)
        except (IOError, OSError, ValueError, json.JSONDecodeError) as exc:
            diffs.append("{0}: cannot parse JSON: {1}".format(rel, exc))
            return False
        if a_data != b_data:
            diffs.append(
                "{0}: JSON content differs".format(rel)
            )
            return False
        return True

    # Default text comparison with normalisation. Binary files (raw/) fall
    # through to a byte compare if utf-8 decode fails.
    try:
        a_text = _normalise_text(_read_text(a_file))
        b_text = _normalise_text(_read_text(b_file))
    except UnicodeDecodeError:
        if _read_bytes(a_file) != _read_bytes(b_file):
            diffs.append("{0}: binary content differs".format(rel))
            return False
        return True

    if a_text != b_text:
        diffs.append("{0}: content differs".format(rel))
        return False
    return True


def assert_walnut_equal(test_case, a_path, b_path, **opts):
    # type: (unittest.TestCase, str, str, **Any) -> None
    """unittest helper that fails with a pretty-printed diff list.

    Usage::

        assert_walnut_equal(self, walnut_a, walnut_b,
                            ignore_log_entries=1)
    """
    match, diffs = walnut_equal(a_path, b_path, **opts)
    if not match:
        message = "walnut trees differ:\n  - " + "\n  - ".join(diffs)
        test_case.fail(message)


__all__ = ["walnut_equal", "assert_walnut_equal"]
