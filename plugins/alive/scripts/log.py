#!/usr/bin/env python3
"""``alive log prepend`` -- log-entry prepend (T5 core + T6 robustness).

Thin, deterministic contract. ``--entry-file`` is **body-only** prose; the
CLI generates the heading, hash-marker, signed line, and separator around
it. Summary is either ``--summary <str>`` or ``--summary-file <path>``
(mutex -- never two concurrent stdin streams). After the atomic log write
the CLI shells out to ``project.py`` and (unless ``--no-index``)
``generate-index.py``, capturing their stdout and truncating it at 2000
chars. Stdout is **pure JSON** -- subprocess output never leaks through.

T6 entry layout (line 2 hash-comment marker added on top of the T5 block):

    \\n
    ## <ISO> -- squirrel:<8hex>\\n
    <!-- entry-hash: <8hex> -->\\n
    \\n
    <body>\\n
    \\n
    signed: squirrel:<8hex>\\n
    \\n
    ---\\n
    \\n

Lock & hook coexistence
-----------------------
The advisory ``fcntl.flock`` lock is acquired on a *separate* lockfile at
``_kernel/.log.md.lock`` and wraps ONLY the read-log/validate/compute/
atomic-write sequence. ``project.py`` and ``generate-index.py`` run
OUTSIDE the lock so same-walnut concurrent prepends never collide on the
world-scale index regeneration. Only ``alive log prepend`` writers take
this lock; existing hooks either read ``_kernel/log.md`` or write
non-overlapping paths, so the narrow scope is sufficient. Cross-walnut
index consistency is best-effort: a later winner may re-regenerate the
index after a loser finishes, but each projection + index call is
idempotent on its own inputs, so eventual consistency holds.

Frontmatter writer is a narrow 3-key hand-rolled editor:

    entry-count: <int>           (plain integer, unquoted)
    last-entry: <ISO>            (unquoted -- matches rules/world.md)
    summary: "<escaped>"         (double-quoted single-line)

Other keys inside the envelope are preserved byte-for-byte. Missing
required keys are appended to the end of the envelope.

Unsupported envelope shapes (raise ``frontmatter_unsupported`` on read):

* CRLF line endings on the delimiter or anywhere in the envelope.
* Missing closing ``---`` delimiter.
* Block scalars (``|``, ``>``, and every chomping / indent variant) on
  any of the three touched keys (``entry-count``, ``last-entry``,
  ``summary``). Block scalars on untouched keys pass through opaque
  -- the close-delimiter check matches ``^---[ \\t]*$`` at column 0
  only, and a valid YAML block-scalar continuation line is always
  indented, so no misdetection is possible for well-formed input.

Exit codes:
    0  success
    1  malformed / general failure
    2  usage error (incl. summary mutex)
    3  walnut path does not exist
    4  permission failure
    5  lock acquisition timed out (``_kernel/.log.md.lock``)
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import stat as _stat_mod
import subprocess
import sys
import time

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _common import (  # noqa: E402
    atomic_write_text,
    find_world_root,
    iso_now,
    resolve_plugin_root,
    resolve_session_id,
    squirrel_short_id,
)


# ---------------------------------------------------------------------------
# Schema metadata (consumed by ``alive schema``)
# ---------------------------------------------------------------------------

SCHEMA_METADATA = {
    "description": (
        "Prepend a signed entry to the walnut's _kernel/log.md, bump the "
        "frontmatter (entry-count / last-entry / summary), and re-run the "
        "projection + world index. Body prose comes from --entry-file; the "
        "CLI generates the heading, signed line, and separator."
    ),
    "stdout_shape": {
        "success": "bool",
        "walnut": "str",
        "entry_id": "str -- ISO-8601 timestamp of the new heading",
        "entry_count": "int",
        "last_entry": "str -- ISO",
        "bytes_written": "int",
        "squirrel_id": "str -- 8 hex",
        "session_id": "str -- full session id",
        "entry_hash": "str -- 8 hex (entry-hash marker on line 2)",
        "idempotency_hit": "bool -- true when head entry-hash matched",
        "log_write_skipped": "bool -- true on idempotency hit or dry-run",
        "existing_entry_id": (
            "str -- ISO timestamp of the head entry; present only when "
            "idempotency_hit is true"
        ),
        "projection_updated": "bool",
        "projection_path": "str",
        "projection_stdout": "str -- truncated at 2000 chars",
        "projection_stdout_bytes": "int -- full length before truncation",
        "projection_stdout_truncated": "bool",
        "index_updated": "bool",
        "index_skipped": "bool -- true when --no-index is passed",
        "index_path": "str",
        "index_stdout": "str -- truncated at 2000 chars",
        "index_stdout_bytes": "int",
        "index_stdout_truncated": "bool",
        "dry_run": "bool -- only present when --dry-run is passed",
        "planned_frontmatter": "dict -- dry-run only",
        "planned_prepend_bytes": "int -- dry-run only",
        "would_write_path": "str -- dry-run only",
        "lock_would_acquire": "bool -- dry-run only",
        "projection_would_run": "bool -- dry-run only",
        "index_would_run": "bool -- dry-run only",
    },
    "exit_codes": {
        "0": "entry prepended; projection + index ran to completion",
        "1": "malformed log / subprocess failure / general error",
        "2": "usage error (including --summary + --summary-file mutex)",
        "3": "--walnut path does not exist",
        "4": "permission failure on log.md or its parent",
        "5": "lock acquisition timed out on _kernel/.log.md.lock",
    },
    "examples": [
        {
            "input": (
                "echo 'body prose' | alive log prepend --walnut /tmp/w "
                "--entry-file - --summary 'updated summary'"
            ),
            "output_excerpt": (
                '{"success": true, "entry_count": 2, ...}'
            ),
        },
    ],
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUBPROCESS_STDOUT_LIMIT = 2000

#: Advisory ``fcntl.flock`` total wait budget (seconds) and per-retry sleep.
#: 5s / 100ms => 50 non-blocking attempts before giving up.
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_RETRY_INTERVAL = 0.1

#: Lockfile name under the walnut's ``_kernel/`` directory. A *separate*
#: file (not ``log.md`` itself) so we never lock the file being rewritten
#: -- ``atomic_write_text`` renames a temp over ``log.md`` and flock on a
#: replaced inode is meaningless on POSIX.
_LOCK_FILE_NAME = ".log.md.lock"

#: Default subprocess timeout in seconds. ``generate-index.py`` is
#: world-scale (sprint spec calls out it "can exceed 5s"); pick a value
#: that's comfortably above the observed p99 for a mid-size world and
#: expose an override for larger setups.
_DEFAULT_SUBPROCESS_TIMEOUT = float(
    os.environ.get("ALIVE_LOG_SUBPROCESS_TIMEOUT", "120")
)

# Error codes surfaced inside the JSON envelope on exit 1.
ERROR_FRONTMATTER_UNSUPPORTED = "frontmatter_unsupported"
ERROR_LOG_MALFORMED = "log_malformed"
ERROR_ENTRY_FILE = "entry_file_error"
ERROR_SUMMARY_FILE = "summary_file_error"
ERROR_PROJECTION_FAILED = "projection_failed"
ERROR_INDEX_FAILED = "index_failed"
ERROR_PLUGIN_ROOT = "plugin_root_error"
ERROR_WORLD_ROOT = "world_root_error"
ERROR_LOCK_TIMEOUT = "lock_timeout"


# ---------------------------------------------------------------------------
# Frontmatter handling (narrow 3-key writer)
# ---------------------------------------------------------------------------

_FRONTMATTER_KEYS = ("entry-count", "last-entry", "summary")


def _escape_summary(value):
    """Escape *value* for the ``summary: "..."`` double-quoted form.

    Narrow escape set: backslash, double-quote, LF, CR. Keeping the writer
    narrow means we can invert it in the test harness without pulling a
    YAML library.
    """
    return (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _format_key_line(key, value):
    """Render one frontmatter line for the three touched keys."""
    if key == "entry-count":
        return "entry-count: {}".format(int(value))
    if key == "last-entry":
        return "last-entry: {}".format(value)
    if key == "summary":
        return 'summary: "{}"'.format(_escape_summary(value))
    raise KeyError(key)


_CLOSE_DELIM_RE = re.compile(r"^---[ \t]*$")


def _split_frontmatter(body):
    """Return ``(envelope_lines, rest)`` or raise ``ValueError``.

    Line-based scan: require ``---`` on line 1, then walk forward to the
    *next* ``---`` line (trailing spaces/tabs allowed). A ``## `` entry
    heading encountered before the close means the envelope never
    terminated -- rather than silently treating an entry separator as the
    close delimiter (which would truncate the envelope and corrupt the
    rewrite), we raise ``ValueError`` so the caller surfaces
    ``frontmatter_unsupported``.

    Empty envelopes (``---\\n---\\n...``) are supported. LF-only line
    endings are enforced: a CRLF opener or any CR in the envelope raises.

    ``rest`` is everything after the closing ``---`` line's newline.
    """
    if body.startswith("---\r\n") or body.startswith("---\r"):
        raise ValueError(
            "frontmatter uses CRLF line endings; only LF is supported"
        )
    # Accept trailing whitespace on the opener (same shape as the close
    # delimiter regex) so the two ends stay symmetric.
    first_nl = body.find("\n")
    if first_nl < 0 or not _CLOSE_DELIM_RE.match(body[:first_nl]):
        raise ValueError("log.md missing opening '---' frontmatter delimiter")

    # splitlines(keepends=True) preserves the terminators so we can
    # reconstruct positions; but we don't need that here -- we just need
    # to find which line index closes the envelope and reassemble rest.
    lines = body.split("\n")
    # ``split("\n")`` on a string with a trailing LF produces an empty
    # tail element; we handle that naturally below.

    # lines[0] is the opening "---".
    # The close-delimiter regex anchors at column 0 (no leading
    # whitespace), so indented block-scalar continuations -- whose
    # content is always indented in valid YAML -- can never false-
    # positive as the envelope close. That lets us walk forward
    # unconditionally until we hit the first column-0 ``---``.
    close_idx = None
    for i in range(1, len(lines)):
        if _CLOSE_DELIM_RE.match(lines[i]):
            close_idx = i
            break

    if close_idx is None:
        raise ValueError(
            "log.md missing closing '---' frontmatter delimiter"
        )

    envelope_lines = lines[1:close_idx]
    # Guard against CRLF inside the envelope -- we ONLY handle LF.
    for line in envelope_lines:
        if "\r" in line:
            raise ValueError(
                "frontmatter uses CRLF line endings; only LF is supported"
            )

    # Rest = everything after the closing delimiter's newline. Reassemble
    # from the lines list instead of tracking byte offsets: join the
    # post-close slice with "\n".
    rest_lines = lines[close_idx + 1:]
    rest = "\n".join(rest_lines)
    return envelope_lines, rest


_KEY_LINE_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")

#: YAML block-scalar header regex. Matches the style-indicator (``|`` or
#: ``>``) followed by an optional indentation indicator (``1``-``9``) and
#: an optional chomping indicator (``-`` or ``+``), in either order.
#: Covers plain ``|`` / ``>``, ``|-`` / ``>-``, ``|+`` / ``>+``,
#: ``|2`` / ``>2``, ``|2-`` / ``>2+``, ``|-2`` / ``>+3`` etc. Anchored so
#: an ordinary value like ``|value`` (missing the space) never matches,
#: though the callers further guard by slicing on the first token.
_BLOCK_SCALAR_HEADER_RE = re.compile(
    r"^[|>](?:[1-9][+\-]?|[+\-][1-9]?)?$"
)


def _is_key_line(line, key):
    m = _KEY_LINE_RE.match(line)
    return bool(m and m.group(1) == key)


def _first_value_token(raw):
    """Return the first whitespace-delimited token from *raw*.

    Used to identify YAML block-scalar markers while tolerating inline
    comments (``summary: | # comment``). We deliberately don't try to
    be a general YAML tokenizer -- quoted strings never contain the
    block-scalar marker characters as their leading character, so a
    simple split is sufficient for the narrow 3-key contract.
    """
    token = raw.strip().split(None, 1)[0] if raw.strip() else ""
    return token


def _value_is_block_scalar(line):
    """True if the value portion of *line* is a YAML block scalar.

    Tolerates inline comments (``summary: | # foo``) and all block-scalar
    header variants -- indent indicator (``|2``), chomping indicator
    (``|-``, ``|+``), and their combinations (``|2-``, ``>+3``). Catches
    forms that a narrow token-set check would miss and silently
    corrupt on rewrite.
    """
    m = _KEY_LINE_RE.match(line)
    if not m:
        return False
    token = _first_value_token(m.group(2))
    return bool(_BLOCK_SCALAR_HEADER_RE.match(token))


def _rewrite_frontmatter(envelope_lines, entry_count, last_entry, summary):
    """Return new envelope-lines with the three touched keys normalized.

    All other lines (comments, blank lines, other scalars, block scalars
    on untouched keys) are preserved byte-for-byte. Missing touched keys
    are appended at the end of the envelope. Block scalars on any of
    the three touched keys raise ``ValueError`` -- we can't safely
    rewrite them without a YAML tokenizer (we don't know where the
    multi-line continuation ends). The close-delimiter check in
    ``_split_frontmatter`` matches only ``^---[ \\t]*$`` at column 0,
    which valid YAML block-scalar continuations never produce
    (continuations must be indented), so untouched block scalars can
    pass through opaque.
    """
    updates = {
        "entry-count": entry_count,
        "last-entry": last_entry,
        "summary": summary,
    }
    seen = {k: False for k in _FRONTMATTER_KEYS}
    new_lines = []
    for line in envelope_lines:
        m = _KEY_LINE_RE.match(line)
        if m and m.group(1) in _FRONTMATTER_KEYS:
            key = m.group(1)
            if _value_is_block_scalar(line):
                raise ValueError(
                    "frontmatter key {!r} uses an unsupported block "
                    "scalar (|/>); only plain scalars are handled on "
                    "the three touched keys".format(key)
                )
            if seen[key]:
                # Duplicate key -- drop the later occurrences so the writer
                # ends up with exactly one normalized line per touched key.
                continue
            seen[key] = True
            new_lines.append(_format_key_line(key, updates[key]))
        else:
            new_lines.append(line)

    for key in _FRONTMATTER_KEYS:
        if not seen[key]:
            new_lines.append(_format_key_line(key, updates[key]))
    return new_lines


def _strip_inline_comment(raw):
    """Strip a narrow YAML-style trailing ``# comment`` from *raw*.

    Splits on the first ``#`` that's preceded by whitespace (so
    ``3 # note`` -> ``3`` but ``foo#bar`` stays intact). Plain scalars
    never contain ``#`` in the middle of the value for the three keys we
    touch, so this narrow rule is safe without a full YAML tokenizer.
    Does NOT honor quotes (use ``_strip_trailing_yaml_comment`` for the
    summary-quoted-form path).
    """
    m = re.search(r"\s#", raw)
    if m is None:
        return raw.strip()
    return raw[: m.start()].strip()


def _strip_trailing_yaml_comment(raw):
    """Quote-aware variant for strings that may start with ``"`` or ``'``.

    Scans left-to-right, tracks quote state, and strips the first
    whitespace-preceded ``#`` seen OUTSIDE a quoted span. Handles the
    writer's double-quoted summary form (``summary: "a \\"b\\"" # note``)
    without eating a ``#`` character that appears inside the quoted
    value. Single-quoted spans treat every character literally except
    ``''`` (escaped single quote); double-quoted spans honor
    ``\\"`` / ``\\\\`` escapes (our writer emits only those).
    """
    out_end = None
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == '"':
            # Walk to the matching unescaped closing quote.
            j = i + 1
            while j < n:
                if raw[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if raw[j] == '"':
                    break
                j += 1
            i = j + 1 if j < n else n
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if raw[j] == "'":
                    if j + 1 < n and raw[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1 if j < n else n
            continue
        if ch == "#" and (i == 0 or raw[i - 1].isspace()):
            out_end = i
            break
        i += 1
    if out_end is None:
        return raw
    return raw[:out_end].rstrip()


def _find_entry_count(envelope_lines):
    """Return the existing entry-count as an int, or 0 if absent/malformed.

    Malformed values (non-integer) raise ``ValueError`` so the caller can
    surface the error rather than silently resetting the counter. Accepts
    the common ``entry-count: 3 # comment`` form by stripping a trailing
    inline comment before parsing.
    """
    for line in envelope_lines:
        m = _KEY_LINE_RE.match(line)
        if m and m.group(1) == "entry-count":
            raw = _strip_inline_comment(m.group(2))
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(
                    "frontmatter entry-count is not an integer: {!r}".format(
                        raw
                    )
                ) from exc
    return 0


def _find_existing_summary(envelope_lines):
    """Return the existing ``summary:`` value (decoded), or ``None``.

    Used to preserve the frontmatter summary when neither ``--summary``
    nor ``--summary-file`` is supplied -- overwriting with ``""`` on a
    bare ``alive log prepend`` would be surprising and the spec doesn't
    require it. Decodes the same narrow escape set the writer emits
    (``\\\\`` / ``\\"`` / ``\\n`` / ``\\r``). Block scalars on the
    ``summary`` key return ``None`` here; the downstream rewrite raises
    ``frontmatter_unsupported`` so the caller still aborts cleanly.
    """
    for line in envelope_lines:
        m = _KEY_LINE_RE.match(line)
        if not m or m.group(1) != "summary":
            continue
        # Block scalars -> bail out; the downstream ``_rewrite_frontmatter``
        # will re-check on its own and raise ``frontmatter_unsupported``.
        if _value_is_block_scalar(line):
            return None
        raw = _strip_trailing_yaml_comment(m.group(2)).strip()
        if (
            len(raw) >= 2
            and raw[0] == raw[-1]
            and raw[0] == '"'
        ):
            inner = raw[1:-1]
            # Invert the writer's narrow escape set.
            return (
                inner
                .replace("\\\\", "\x00")
                .replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace("\x00", "\\")
            )
        if (
            len(raw) >= 2
            and raw[0] == raw[-1]
            and raw[0] == "'"
        ):
            # Single-quoted: literal content (YAML convention would
            # double up ``''``; the writer never emits this form but
            # fixtures may, so be lenient).
            return raw[1:-1].replace("''", "'")
        return raw
    return None


# ---------------------------------------------------------------------------
# Entry block construction
# ---------------------------------------------------------------------------

def _normalize_body(body):
    """Strip trailing whitespace at the body/file boundary only.

    ``body.rstrip()`` handles the boundary case (trailing spaces, tabs,
    and newlines at EOF) without altering interior line semantics --
    notably, Markdown's "two trailing spaces = hard line break" stays
    intact. The deterministic entry block already owns the blank line
    that follows the body, so an extra trailing blank from the author
    would otherwise produce a double-blank run between body and
    ``signed: ...``.
    """
    return body.rstrip()


def _normalize_for_hash(value):
    """Canonicalize text for entry-hash computation.

    Strip trailing whitespace (rstrip) and normalize CRLF / bare CR to
    LF. ``str(value or "")`` guards against ``None`` summaries so the
    hash canonicalization never raises on a missing input.
    """
    s = str(value or "")
    return s.rstrip().replace("\r\n", "\n").replace("\r", "\n")


def _compute_entry_hash(entry_body, summary, session_id):
    """Compute the 8-hex entry hash used as the marker comment.

    Canonical pre-image (bytes):
        normalize(entry_body) || b"\\n---\\n" || normalize(summary) ||
        session_id

    The ``session_id`` is appended unnormalized -- two different sessions
    emitting identical body+summary are legitimately non-idempotent (the
    squirrel signature is part of identity), and the session id has no
    whitespace / line-ending ambiguity to normalize.
    """
    canon = (
        _normalize_for_hash(entry_body).encode("utf-8")
        + b"\n---\n"
        + _normalize_for_hash(summary).encode("utf-8")
        + (session_id or "").encode("utf-8")
    )
    return hashlib.sha256(canon).hexdigest()[:8]


_ENTRY_HASH_MARKER_RE = re.compile(
    r"<!--\s*entry-hash:\s*([0-9a-f]{8})\s*-->"
)


def _build_entry_block(iso_ts, squirrel_id, body, entry_hash):
    """Return the T6 entry block (with ``<!-- entry-hash: ... -->`` line 2)."""
    body_clean = _normalize_body(body)
    return (
        "\n"
        "## {ts} -- squirrel:{sq}\n"
        "<!-- entry-hash: {eh} -->\n"
        "\n"
        "{body}\n"
        "\n"
        "signed: squirrel:{sq}\n"
        "\n"
        "---\n"
        "\n"
    ).format(ts=iso_ts, sq=squirrel_id, eh=entry_hash, body=body_clean)


#: Matches the column-0 ``---`` frontmatter close delimiter with optional
#: trailing whitespace (same shape as :data:`_CLOSE_DELIM_RE` but with an
#: anchoring newline on each side so ``re.search`` can locate it inside
#: a larger buffer). Kept distinct from ``_CLOSE_DELIM_RE`` which is used
#: on already-split single-line slices.
_CLOSE_DELIM_SEARCH_RE = re.compile(r"\n---[ \t]*\n", re.MULTILINE)


def _head_entry_window(log_body):
    """Return the text slice starting at the head ``## `` entry, or ``""``.

    Skips the frontmatter envelope (``---\\n...\\n---\\n``, with optional
    trailing spaces/tabs on either delimiter line -- matching the tolerance
    of :func:`_split_frontmatter`), then returns the slice from the first
    ``## `` heading onward. Empty string on miss so callers can regex on
    the result without None-checking.
    """
    # Accept ``---\n`` or ``---   \n`` on the opener (same tolerance as
    # ``_split_frontmatter``; otherwise a fixture that's valid for the
    # write path would be invisible to the N=1 idempotency scanner).
    first_nl = log_body.find("\n")
    if first_nl < 0 or not _CLOSE_DELIM_RE.match(log_body[:first_nl]):
        return ""
    close_m = _CLOSE_DELIM_SEARCH_RE.search(log_body, first_nl)
    if close_m is None:
        return ""
    tail = log_body[close_m.end():]
    heading_match = re.search(r"^## ", tail, re.MULTILINE)
    if heading_match is None:
        return ""
    return tail[heading_match.start():]


#: Anchored marker: heading line then hash comment on line 2, per the
#: T6 layout contract. Tighter than a free ``re.search`` so a user-
#: authored whole-line ``<!-- entry-hash: ... -->`` further down in the
#: entry body can't false-positive the idempotency short-circuit on a
#: legacy entry that has no real marker.
_HEAD_HASH_LAYOUT_RE = re.compile(
    r"^## [^\n]*\n<!--\s*entry-hash:\s*([0-9a-f]{8})\s*-->"
)


def _extract_head_entry_hash(log_body):
    """Return the ``<!-- entry-hash: ... -->`` marker from the head entry.

    Anchored to the T6 byte layout: the marker MUST sit on line 2 of the
    head entry (immediately after the ``## ...`` heading). A marker that
    only appears later in the body is ignored -- it's either user-authored
    prose or a legacy entry without a real hash, and treating it as the
    canonical marker would let an attacker / fixture author silently
    short-circuit the idempotency guard.
    """
    window = _head_entry_window(log_body)
    if not window:
        return None
    m = _HEAD_HASH_LAYOUT_RE.match(window)
    return m.group(1) if m else None


def _extract_head_entry_id(log_body):
    """Return the ISO timestamp of the head entry's heading, or ``None``.

    The heading is ``## <ISO> -- squirrel:<8hex>``; we pull the first
    whitespace-delimited token after ``## ``. Shared with the
    idempotency-hit path so the surfaced ``existing_entry_id`` matches
    whatever the original writer emitted.
    """
    window = _head_entry_window(log_body)
    if not window:
        return None
    m = re.match(r"^## (\S+)", window)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------

def _read_entry_file(path):
    """Read the body from ``--entry-file``. ``-`` means stdin."""
    if path == "-":
        # Read bytes explicitly so a locale mismatch (e.g. ``LC_ALL=C``)
        # never raises ``UnicodeDecodeError`` out of text-mode stdin.
        # Decode with ``errors='replace'`` so invalid bytes land as
        # U+FFFD rather than crashing the command -- the pure-JSON
        # stdout contract is stronger than "prefer strict decoding".
        try:
            raw = sys.stdin.buffer.read()
        except OSError as exc:
            raise _LogError(
                "failed to read entry body from stdin: {}".format(exc),
                code=ERROR_ENTRY_FILE,
                exit_code=1,
            ) from exc
        return raw.decode("utf-8", errors="replace")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError as exc:
        raise _LogError(
            "--entry-file not found: {}".format(path),
            code=ERROR_ENTRY_FILE,
            exit_code=1,
        ) from exc
    except PermissionError as exc:
        raise _LogError(
            "permission denied reading --entry-file {!r}: {}".format(
                path, exc
            ),
            code="permission",
            exit_code=4,
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise _LogError(
            "failed to read --entry-file {!r}: {}".format(path, exc),
            code=ERROR_ENTRY_FILE,
            exit_code=1,
        ) from exc


def _read_summary(summary, summary_file):
    """Resolve the summary string from the two CLI flags.

    Returns ``None`` when neither flag was provided -- the handler then
    falls back to the existing frontmatter value so a bare
    ``alive log prepend`` never silently wipes a populated summary.
    ``summary`` (string flag) wins over ``summary_file``.
    """
    if summary is not None:
        return summary
    if summary_file is None:
        return None
    try:
        with open(summary_file, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError as exc:
        raise _LogError(
            "--summary-file not found: {}".format(summary_file),
            code=ERROR_SUMMARY_FILE,
            exit_code=1,
        ) from exc
    except PermissionError as exc:
        raise _LogError(
            "permission denied reading --summary-file {!r}: {}".format(
                summary_file, exc
            ),
            code="permission",
            exit_code=4,
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise _LogError(
            "failed to read --summary-file {!r}: {}".format(
                summary_file, exc
            ),
            code=ERROR_SUMMARY_FILE,
            exit_code=1,
        ) from exc


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _truncate(text, raw_bytes_len, limit=SUBPROCESS_STDOUT_LIMIT):
    """Return ``(truncated_text, full_byte_length, was_truncated)``.

    ``raw_bytes_len`` is the length of the *original* captured bytes
    (before UTF-8 decoding with ``errors='replace'``). This matches the
    spec's intent for ``*_stdout_bytes`` -- the agent sees how many
    bytes the child actually emitted, not how many bytes our decoded
    string would take to re-encode (those can differ once U+FFFD
    replacements enter the picture). Truncation happens on the decoded
    string boundary so we never cut a codepoint mid-sequence.
    """
    if text is None:
        return "", int(raw_bytes_len or 0), False
    if len(text) <= limit:
        return text, int(raw_bytes_len or 0), False
    return text[:limit], int(raw_bytes_len or 0), True


def _run_subprocess(cmd, error_code, timeout=None):
    """Invoke *cmd* and return ``(returncode, stdout, stderr, stdout_raw_len)``.

    Output is captured as bytes and decoded as UTF-8 with
    ``errors='replace'`` so a subprocess emitting non-UTF-8 output under
    a locale like ``C`` never propagates a ``UnicodeDecodeError`` out of
    this module -- that would break the pure-JSON-stdout contract. The
    U+FFFD replacement stays inside the stdout string we return, but
    the raw-byte length is reported separately so ``*_stdout_bytes`` in
    the JSON envelope reflects what the child actually emitted rather
    than the re-encoded replacement-char length.

    OSError (executable not found) and timeout are re-raised as
    :class:`_LogError` carrying *error_code* so the caller's single error
    path drives the JSON envelope with the right code.
    """
    effective_timeout = (
        timeout if timeout is not None else _DEFAULT_SUBPROCESS_TIMEOUT
    )
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=effective_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _LogError(
            "executable not found for subprocess: {} ({})".format(cmd[0], exc),
            code=error_code,
            exit_code=1,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _LogError(
            "subprocess timed out: {} ({}s limit; override via "
            "$ALIVE_LOG_SUBPROCESS_TIMEOUT)".format(
                " ".join(cmd), effective_timeout
            ),
            code=error_code,
            exit_code=1,
        ) from exc
    stdout_bytes = proc.stdout or b""
    stderr_bytes = proc.stderr or b""
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return proc.returncode, stdout, stderr, len(stdout_bytes)


# ---------------------------------------------------------------------------
# Lock acquisition
# ---------------------------------------------------------------------------

class _FlockGuard(object):
    """Context manager wrapping ``fcntl.flock`` on a lockfile fd.

    Acquires ``LOCK_EX | LOCK_NB`` with a bounded retry loop so a blocked
    writer never stalls longer than ``_LOCK_TIMEOUT_SECONDS``. Releases
    the lock and closes the fd in ``__exit__``. On acquisition timeout
    raises :class:`_LogError` with ``code: lock_timeout`` + ``exit 5``.

    The lockfile itself is a zero-byte sentinel; the lock protects *log.md*
    (which is written via rename and therefore cannot be flock'd directly).
    """

    def __init__(self, lock_path):
        self._lock_path = lock_path
        self._fd = None

    def __enter__(self):
        # O_CLOEXEC so a forked subprocess can't accidentally inherit the
        # lock holder. O_CREAT so concurrent first-writers both succeed in
        # creating-or-opening without a TOCTOU race. ``getattr`` guards
        # against exotic builds that omit O_CLOEXEC (it's POSIX.1-2008
        # but not universally re-exported through the Python os module).
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self._lock_path, flags, 0o644)
        except PermissionError as exc:
            raise _LogError(
                "permission denied opening lockfile {}: {}".format(
                    self._lock_path, exc
                ),
                code="permission",
                exit_code=4,
            ) from exc
        except OSError as exc:
            raise _LogError(
                "failed to open lockfile {}: {}".format(
                    self._lock_path, exc
                ),
                code=ERROR_LOG_MALFORMED,
                exit_code=1,
            ) from exc

        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return self
            except BlockingIOError:
                pass
            except OSError as exc:
                # EWOULDBLOCK on some kernels is distinct from EAGAIN.
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    os.close(fd)
                    raise _LogError(
                        "flock failed on {}: {}".format(
                            self._lock_path, exc
                        ),
                        code=ERROR_LOG_MALFORMED,
                        exit_code=1,
                    ) from exc
            if time.monotonic() >= deadline:
                os.close(fd)
                raise _LogError(
                    "lock acquisition timed out after {}s".format(
                        _LOCK_TIMEOUT_SECONDS
                    ),
                    code=ERROR_LOCK_TIMEOUT,
                    exit_code=5,
                    extra={
                        "path": self._lock_path,
                        "hint": (
                            "another writer held the lock for "
                            "{}s; retry or investigate".format(
                                int(_LOCK_TIMEOUT_SECONDS)
                            )
                        ),
                    },
                )
            time.sleep(_LOCK_RETRY_INTERVAL)

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                # Best-effort: if unlock failed, closing the fd implicitly
                # releases the flock on POSIX. Swallow so we don't mask the
                # original exception path.
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        return False


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------

class _LogError(Exception):
    """Raised inside ``handle`` to short-circuit into a JSON error envelope.

    ``code`` is the machine-readable token agents match on
    (``frontmatter_unsupported`` etc.). ``exit_code`` is the POSIX exit code
    to surface. ``detail`` may carry extra subprocess output; merged into
    the emitted JSON under ``error.detail``. ``extra`` carries additional
    top-level siblings of ``code`` / ``message`` on the ``error`` object
    (used for ``code: lock_timeout`` which promotes ``path`` + ``hint``).
    """

    def __init__(self, message, code, exit_code=1, detail=None, extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.detail = detail
        self.extra = extra


def _emit_error(exc):
    """Print the JSON error envelope for *exc* to stdout.

    The envelope carries ``success: false`` + an ``error`` object with
    ``code`` / ``message`` / optional ``detail`` / optional top-level
    ``extra`` siblings. Stdout-only; stderr stays empty so agents never
    need to parse two streams.
    """
    error_obj = {
        "code": exc.code,
        "message": exc.message,
    }
    if exc.detail is not None:
        error_obj["detail"] = exc.detail
    if exc.extra:
        for k, v in exc.extra.items():
            error_obj[k] = v
    payload = {
        "success": False,
        "error": error_obj,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Argparse registration
# ---------------------------------------------------------------------------

def register(subparsers):
    """Register the ``log`` group and its ``prepend`` subcommand."""
    parser = subparsers.add_parser(
        "log",
        help="Log-manipulation subcommands (prepend, ...).",
        description="Log-manipulation subcommands.",
    )
    log_subparsers = parser.add_subparsers(dest="log_command")
    log_subparsers.required = False

    prepend = log_subparsers.add_parser(
        "prepend",
        help=SCHEMA_METADATA["description"],
        description=SCHEMA_METADATA["description"],
    )
    # ``default=SUPPRESS`` means the subparser does NOT overwrite
    # ``args.plugin_root`` with ``None`` when the user passed the flag at
    # the TOP level (``alive --plugin-root /p log prepend ...``). A normal
    # ``default=None`` here silently masks the top-level value.
    prepend.add_argument(
        "--plugin-root",
        default=argparse.SUPPRESS,
        help=(
            "Override the ALIVE plugin root directory "
            "(defaults: $ALIVE_PLUGIN_ROOT, then auto-discovery)."
        ),
    )
    prepend.add_argument(
        "--walnut",
        required=True,
        help="Path to the walnut directory.",
    )
    prepend.add_argument(
        "--entry-file",
        required=True,
        help=(
            "Path to a file containing the entry BODY (no heading / signed "
            "line / separator). Pass '-' to read from stdin."
        ),
    )
    prepend.add_argument(
        "--summary",
        default=None,
        help="Frontmatter summary (one-line). Mutex with --summary-file.",
    )
    prepend.add_argument(
        "--summary-file",
        default=None,
        help=(
            "Path to a file whose content becomes the summary. "
            "Mutex with --summary."
        ),
    )
    prepend.add_argument(
        "--session-id",
        default=None,
        help=(
            "Override session id (default: $ALIVE_SESSION_ID, then "
            "$CLAUDE_SESSION_ID, then synthesized)."
        ),
    )
    prepend.add_argument(
        "--no-index",
        action="store_true",
        default=False,
        help="Skip the generate-index.py subprocess after the log write.",
    )
    prepend.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Plan the prepend (frontmatter + entry hash + byte counts) "
            "without acquiring the lock, mutating log.md, or running "
            "project.py / generate-index.py. Emits a JSON envelope with "
            "``dry_run: true`` describing what would happen."
        ),
    )
    prepend.error = _json_error_handler(prepend)  # type: ignore[assignment]

    # Also wire the top-level ``log`` parser's error handler -- missing
    # subcommand (``alive log`` alone) should land on stdout as JSON.
    parser.error = _json_error_handler(parser)  # type: ignore[assignment]

    # Stash metadata via set_defaults so ``alive schema`` finds it without
    # having to know about the nested ``log prepend`` path.
    from schema import SCHEMA_METADATA_DEFAULT_KEY  # noqa: E402
    prepend.set_defaults(
        _handler=handle,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )

    # Give the ``log`` group a handler that prints usage-as-JSON when
    # invoked with no sub-sub-command (so agents never see argparse's
    # default text-to-stderr output).
    parser.set_defaults(_handler=_log_group_missing_subcommand)

    return parser


def _json_error_handler(parser):
    """Return an argparse error replacement that emits a JSON envelope."""

    def _error(message):
        payload = {
            "success": False,
            "error": {
                "code": "usage",
                "message": "usage: {}".format(message),
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        sys.exit(2)

    return _error


def _log_group_missing_subcommand(args):
    """Handler for bare ``alive log`` (no sub-sub-command)."""
    payload = {
        "success": False,
        "error": {
            "code": "usage",
            "message": "missing log subcommand; try `alive log prepend --help`",
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handle(args):
    """Execute ``alive log prepend`` based on parsed args; return exit code.

    Every exception path MUST land on stdout as a JSON envelope -- the
    "stdout is pure JSON" contract is absolute for agent-facing CLIs. An
    uncaught exception would emit a traceback on stderr and leave stdout
    empty; agents treat that as "the transport broke", not "the
    operation failed with a known error code". We catch ``_LogError``
    separately so its structured envelope shape flows through, and fall
    back to a broad ``Exception`` handler that reports
    ``code: internal_error`` with the exception message (never the full
    traceback -- that would leak local filesystem details to the agent).
    """
    try:
        return _handle_inner(args)
    except _LogError as exc:
        _emit_error(exc)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 -- pure-JSON-stdout contract
        _emit_error(_LogError(
            "internal error: {}: {}".format(type(exc).__name__, exc),
            code="internal_error",
            exit_code=1,
        ))
        return 1


def _read_log_body(log_path):
    """Read ``log.md`` with the canonical error-code mapping."""
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError as exc:
        raise _LogError(
            "_kernel/log.md not found at {}".format(log_path),
            code=ERROR_LOG_MALFORMED,
            exit_code=1,
        ) from exc
    except PermissionError as exc:
        raise _LogError(
            "permission denied reading {}: {}".format(log_path, exc),
            code="permission",
            exit_code=4,
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise _LogError(
            "failed to read {}: {}".format(log_path, exc),
            code=ERROR_LOG_MALFORMED,
            exit_code=1,
        ) from exc


def _validate_frontmatter(log_body):
    """Split + parse frontmatter; raise frontmatter_unsupported on malformed."""
    try:
        envelope_lines, rest = _split_frontmatter(log_body)
    except ValueError as exc:
        raise _LogError(
            str(exc),
            code=ERROR_FRONTMATTER_UNSUPPORTED,
            exit_code=1,
        ) from exc
    try:
        old_entry_count = _find_entry_count(envelope_lines)
    except ValueError as exc:
        raise _LogError(
            str(exc),
            code=ERROR_FRONTMATTER_UNSUPPORTED,
            exit_code=1,
        ) from exc
    return envelope_lines, rest, old_entry_count


def _compute_new_log(envelope_lines, rest, new_entry_count, now_iso,
                     summary_raw, squirrel, body_raw, entry_hash):
    """Return the rewritten log.md text.

    Wraps frontmatter rewrite + entry-block assembly. Separated so the
    dry-run path can reuse it to compute planned byte counts without
    writing the file.
    """
    try:
        new_envelope_lines = _rewrite_frontmatter(
            envelope_lines,
            entry_count=new_entry_count,
            last_entry=now_iso,
            summary=summary_raw,
        )
    except ValueError as exc:
        raise _LogError(
            str(exc),
            code=ERROR_FRONTMATTER_UNSUPPORTED,
            exit_code=1,
        ) from exc

    entry_block = _build_entry_block(now_iso, squirrel, body_raw, entry_hash)

    # Rebuild the file: opening ``---\n`` + each envelope line followed by
    # ``\n`` + closing ``---\n`` + entry block + original rest.
    # Per-line append preserves trailing-blank-line envelope shapes that a
    # ``"\n".join`` would silently collapse.
    envelope_out = "".join(line + "\n" for line in new_envelope_lines)
    new_log = (
        "---\n"
        + envelope_out
        + "---\n"
        + entry_block
        + rest
    )
    return new_log


def _run_projection_and_index(args, walnut_abs, plugin_root, world_root):
    """Run project.py + (optionally) generate-index.py; return result dict.

    Called OUTSIDE the lock so same-walnut concurrent writers never
    contend on the world-scale index regeneration. Each subprocess is
    idempotent on its own inputs, so re-running after an idempotency hit
    (to recover from a partial-failure retry) produces the same result.
    """
    project_script = os.path.join(plugin_root, "scripts", "project.py")
    python_exe = os.environ.get("ALIVE_PYTHON") or sys.executable or "python3"
    proj_rc, proj_out, proj_err, proj_raw_len = _run_subprocess(
        [python_exe, project_script, "--walnut", walnut_abs],
        error_code=ERROR_PROJECTION_FAILED,
    )
    proj_trunc, proj_bytes, proj_was_trunc = _truncate(proj_out, proj_raw_len)
    projection_updated = proj_rc == 0
    projection_path = os.path.join(walnut_abs, "_kernel", "now.json")

    if not projection_updated:
        raise _LogError(
            "project.py exited {} for walnut {}".format(proj_rc, walnut_abs),
            code=ERROR_PROJECTION_FAILED,
            exit_code=1,
            detail={
                "stderr": proj_err,
                "stdout": proj_trunc,
                "stdout_bytes": proj_bytes,
                "stdout_truncated": proj_was_trunc,
                "returncode": proj_rc,
            },
        )

    index_updated = False
    index_skipped = bool(args.no_index)
    index_stdout = ""
    index_stdout_bytes = 0
    index_stdout_truncated = False
    index_path = (
        os.path.join(world_root, ".alive", "_index.json")
        if world_root
        else ""
    )

    if not index_skipped:
        index_script = os.path.join(
            plugin_root, "scripts", "generate-index.py"
        )
        idx_rc, idx_out, idx_err, idx_raw_len = _run_subprocess(
            [python_exe, index_script, str(world_root)],
            error_code=ERROR_INDEX_FAILED,
        )
        idx_trunc, idx_bytes, idx_was_trunc = _truncate(idx_out, idx_raw_len)
        index_stdout = idx_trunc
        index_stdout_bytes = idx_bytes
        index_stdout_truncated = idx_was_trunc
        if idx_rc != 0:
            raise _LogError(
                "generate-index.py exited {} for world {}".format(
                    idx_rc, world_root
                ),
                code=ERROR_INDEX_FAILED,
                exit_code=1,
                detail={
                    "stderr": idx_err,
                    "stdout": idx_trunc,
                    "stdout_bytes": idx_bytes,
                    "stdout_truncated": idx_was_trunc,
                    "returncode": idx_rc,
                },
            )
        index_updated = True

    return {
        "projection_updated": projection_updated,
        "projection_path": projection_path,
        "projection_stdout": proj_trunc,
        "projection_stdout_bytes": proj_bytes,
        "projection_stdout_truncated": proj_was_trunc,
        "index_updated": index_updated,
        "index_skipped": index_skipped,
        "index_path": index_path,
        "index_stdout": index_stdout,
        "index_stdout_bytes": index_stdout_bytes,
        "index_stdout_truncated": index_stdout_truncated,
    }


def _handle_inner(args):
    # -----------------------------------------------------------------
    # Usage-mutex: --summary AND --summary-file are mutually exclusive.
    # -----------------------------------------------------------------
    if args.summary is not None and args.summary_file is not None:
        raise _LogError(
            "--summary and --summary-file are mutually exclusive",
            code="usage",
            exit_code=2,
        )

    # -----------------------------------------------------------------
    # Walnut exists.
    # -----------------------------------------------------------------
    walnut_abs = os.path.abspath(os.path.expanduser(args.walnut))
    try:
        stat = os.stat(walnut_abs)
    except FileNotFoundError:
        raise _LogError(
            "walnut path does not exist: {}".format(walnut_abs),
            code="walnut_not_found",
            exit_code=3,
        )
    except PermissionError as exc:
        raise _LogError(
            "permission denied stat'ing walnut {}: {}".format(
                walnut_abs, exc
            ),
            code="permission",
            exit_code=4,
        ) from exc
    except OSError as exc:
        raise _LogError(
            "failed to stat walnut {}: {}".format(walnut_abs, exc),
            code=ERROR_LOG_MALFORMED,
            exit_code=1,
        ) from exc
    if not _stat_mod.S_ISDIR(stat.st_mode):
        raise _LogError(
            "walnut path is not a directory: {}".format(walnut_abs),
            code="walnut_not_found",
            exit_code=3,
        )

    kernel_dir = os.path.join(walnut_abs, "_kernel")
    log_path = os.path.join(kernel_dir, "log.md")
    lock_path = os.path.join(kernel_dir, _LOCK_FILE_NAME)

    # -----------------------------------------------------------------
    # Plugin root (consumed for subprocess script discovery).
    # -----------------------------------------------------------------
    try:
        plugin_root = resolve_plugin_root(getattr(args, "plugin_root", None))
    except FileNotFoundError as exc:
        raise _LogError(
            str(exc),
            code=ERROR_PLUGIN_ROOT,
            exit_code=1,
        ) from exc

    # -----------------------------------------------------------------
    # World root (unconditional so the JSON envelope shape stays stable
    # even with --no-index; a missing world-root only aborts when the
    # index subprocess would actually need it).
    # -----------------------------------------------------------------
    world_root = None
    try:
        world_root = find_world_root(walnut_abs)
    except FileNotFoundError as exc:
        if not args.no_index:
            raise _LogError(
                str(exc),
                code=ERROR_WORLD_ROOT,
                exit_code=1,
            ) from exc

    # -----------------------------------------------------------------
    # Resolve session / squirrel id (done BEFORE lock so a usage error
    # never blocks anyone else).
    # -----------------------------------------------------------------
    session_id = resolve_session_id(args.session_id)
    squirrel = squirrel_short_id(session_id)
    if not re.match(r"^[0-9a-f]{8}$", squirrel):
        raise _LogError(
            "squirrel id {!r} must be 8 lowercase hex characters; pass "
            "--session-id with a hex-prefixed value (e.g. "
            "``abcdef01-...``) or let the CLI synthesize one".format(
                squirrel
            ),
            code="usage",
            exit_code=2,
        )

    # -----------------------------------------------------------------
    # Read body + summary (stdin consumed here, before the lock, so we
    # never hold the lock while blocking on slow stdin).
    # -----------------------------------------------------------------
    body_raw = _read_entry_file(args.entry_file)
    summary_flag = _read_summary(args.summary, args.summary_file)

    # -----------------------------------------------------------------
    # Compute the entry-hash early so both dry-run and the lock-scoped
    # path can use it. Hash uses the FINAL summary, which depends on
    # the existing frontmatter -- so we read log.md once (unlocked, for
    # planning) to resolve the fallback summary. This unlocked read is
    # a hint; the lock-scoped re-read is the one that drives the write.
    # -----------------------------------------------------------------
    dry_run = bool(getattr(args, "dry_run", False))

    # ----- Dry-run path: no lock, no write, no subprocesses. ---------
    if dry_run:
        log_body = _read_log_body(log_path)
        envelope_lines, rest, old_entry_count = _validate_frontmatter(log_body)
        summary_raw = summary_flag
        if summary_raw is None:
            existing = _find_existing_summary(envelope_lines)
            summary_raw = existing if existing is not None else ""
        entry_hash = _compute_entry_hash(body_raw, summary_raw, session_id)
        head_hash = _extract_head_entry_hash(log_body)
        idempotency_hit = head_hash == entry_hash

        if idempotency_hit:
            # On a hit the real command acquires the lock (to re-check
            # safely) but skips the write AND the frontmatter bump.
            # Mirror that: zero planned bytes, unchanged touched-key
            # values, ``lock_would_acquire: true``. ``projection_would_run``
            # + ``index_would_run`` still reflect the real behavior (the
            # subprocesses run on a hit to recover from a prior partial
            # failure; only the log write is suppressed).
            planned_prepend_bytes = 0
            # Preserve existing last-entry from the envelope so the
            # planning payload reflects "nothing would change on disk".
            existing_last_entry = None
            for _line in envelope_lines:
                _m = _KEY_LINE_RE.match(_line)
                if _m and _m.group(1) == "last-entry":
                    existing_last_entry = _strip_inline_comment(
                        _m.group(2)
                    )
                    break
            planned_frontmatter = {
                "entry-count": old_entry_count,
                "last-entry": existing_last_entry,
                "summary": summary_raw,
            }
        else:
            now_iso = iso_now()
            new_entry_count = old_entry_count + 1
            new_log = _compute_new_log(
                envelope_lines, rest, new_entry_count, now_iso,
                summary_raw, squirrel, body_raw, entry_hash,
            )
            planned_prepend_bytes = len(new_log.encode("utf-8")) - len(
                log_body.encode("utf-8")
            )
            planned_frontmatter = {
                "entry-count": new_entry_count,
                "last-entry": now_iso,
                "summary": summary_raw,
            }

        payload = {
            "success": True,
            "dry_run": True,
            "walnut": walnut_abs,
            "squirrel_id": squirrel,
            "session_id": session_id,
            "entry_hash": entry_hash,
            "idempotency_hit": idempotency_hit,
            "log_write_skipped": True,
            "would_write_path": log_path,
            # The real command always acquires the lock to re-check
            # idempotency under lock; ``lock_would_acquire`` reports
            # "would the non-dry-run call take the flock". On a hit
            # we DO still take it (then skip the write). So this is
            # True whenever the planning path reaches this point.
            "lock_would_acquire": True,
            "projection_would_run": True,
            "index_would_run": not bool(args.no_index),
            "planned_frontmatter": planned_frontmatter,
            "planned_prepend_bytes": planned_prepend_bytes,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    # -----------------------------------------------------------------
    # Write path: acquire narrow lock for the read/validate/compute/write.
    # -----------------------------------------------------------------
    now_iso = None
    new_entry_count = None
    bytes_written = 0
    entry_hash = None
    idempotency_hit = False
    log_write_skipped = False
    existing_entry_id = None

    with _FlockGuard(lock_path):
        log_body = _read_log_body(log_path)
        envelope_lines, rest, old_entry_count = _validate_frontmatter(log_body)

        summary_raw = summary_flag
        if summary_raw is None:
            existing = _find_existing_summary(envelope_lines)
            summary_raw = existing if existing is not None else ""

        entry_hash = _compute_entry_hash(body_raw, summary_raw, session_id)
        head_hash = _extract_head_entry_hash(log_body)

        if head_hash == entry_hash:
            # Idempotency hit: skip the log write, BUT still run the
            # projection + index outside the lock. project.py + index
            # are idempotent, so re-running handles "retry after a
            # partial failure" without introducing duplicate entries.
            idempotency_hit = True
            log_write_skipped = True
            new_entry_count = old_entry_count  # unchanged
            existing_entry_id = _extract_head_entry_id(log_body)
            # ``now_iso`` stays None; the envelope assembler surfaces
            # the old entry's timestamp as ``existing_entry_id`` /
            # ``entry_id`` / ``last_entry``.
        else:
            now_iso = iso_now()
            new_entry_count = old_entry_count + 1
            new_log = _compute_new_log(
                envelope_lines, rest, new_entry_count, now_iso,
                summary_raw, squirrel, body_raw, entry_hash,
            )
            try:
                atomic_write_text(log_path, new_log)
            except PermissionError as exc:
                raise _LogError(
                    "permission denied writing {}: {}".format(
                        log_path, exc
                    ),
                    code="permission",
                    exit_code=4,
                ) from exc
            except OSError as exc:
                raise _LogError(
                    "failed to write {}: {}".format(log_path, exc),
                    code=ERROR_LOG_MALFORMED,
                    exit_code=1,
                ) from exc
            bytes_written = len(new_log.encode("utf-8"))

    # -----------------------------------------------------------------
    # Subprocesses OUTSIDE the lock. On idempotency hit we still run
    # them -- they're idempotent and this lets a caller recover from a
    # prior run that wrote log.md but crashed before projection/index.
    # -----------------------------------------------------------------
    sub = _run_projection_and_index(args, walnut_abs, plugin_root, world_root)

    # -----------------------------------------------------------------
    # Assemble JSON envelope.
    # -----------------------------------------------------------------
    payload = {
        "success": True,
        "walnut": walnut_abs,
        "entry_count": new_entry_count,
        "bytes_written": bytes_written,
        "squirrel_id": squirrel,
        "session_id": session_id,
        "entry_hash": entry_hash,
        "idempotency_hit": idempotency_hit,
        "log_write_skipped": log_write_skipped,
        "projection_updated": sub["projection_updated"],
        "projection_path": sub["projection_path"],
        "projection_stdout": sub["projection_stdout"],
        "projection_stdout_bytes": sub["projection_stdout_bytes"],
        "projection_stdout_truncated": sub["projection_stdout_truncated"],
        "index_updated": sub["index_updated"],
        "index_skipped": sub["index_skipped"],
        "index_path": sub["index_path"],
        "index_stdout": sub["index_stdout"],
        "index_stdout_bytes": sub["index_stdout_bytes"],
        "index_stdout_truncated": sub["index_stdout_truncated"],
    }
    if idempotency_hit:
        # On hit, surface the pre-existing entry id (ISO timestamp of the
        # head heading) instead of a fresh one.
        payload["entry_id"] = existing_entry_id
        payload["last_entry"] = existing_entry_id
        payload["existing_entry_id"] = existing_entry_id
    else:
        payload["entry_id"] = now_iso
        payload["last_entry"] = now_iso
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# Direct-invocation support (python3 scripts/log.py ...)
# ---------------------------------------------------------------------------

def _standalone_main(argv=None):
    parser = argparse.ArgumentParser(prog="alive-log")
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)
    # Force the ``log`` group so argparse resolves cleanly.
    args = parser.parse_args(["log"] + (list(argv) if argv else []))
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(_standalone_main(sys.argv[1:]))
