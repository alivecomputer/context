"""Diff-rendering helpers for the walkthrough phase 7 UX.

Pure / read-only. The decide-phase prompt prints a 3-5 line excerpt
around each retired-pattern hit; the ``[d] show full diff`` branch
fans out to ``render_full_diff``. Both helpers operate on bytes -- the
caller is responsible for decoding to UTF-8 once for display.

Stdlib-only (R10).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import List, Tuple


__all__ = (
    "Excerpt",
    "render_excerpt",
    "render_full_diff",
)


# Number of context lines around the matched line in the short excerpt.
# 2 lines on each side + the match itself = up to 5 lines, matching the
# "3-5 line excerpt" contract from the epic spec.
_EXCERPT_CONTEXT_LINES = 2


@dataclass(frozen=True)
class Excerpt:
    """One 3-5 line excerpt around a catalog-match hit.

    Attributes
    ----------
    start_line:
        1-based line number of the first excerpt line.
    match_line:
        1-based line number containing the matched bytes.
    lines:
        ``(line_number, line_text)`` tuples. ``line_text`` excludes the
        trailing newline so the renderer can pick its own join.
    """

    start_line: int
    match_line: int
    lines: Tuple[Tuple[int, str], ...]


def _byte_span_to_line(content: bytes, span_start: int) -> int:
    """Return the 1-based line number containing byte offset ``span_start``."""
    if span_start < 0:
        span_start = 0
    # Count newlines BEFORE span_start; the line number is that count + 1.
    head = content[:span_start]
    return head.count(b"\n") + 1


def render_excerpt(
    content: bytes,
    span_start: int,
    span_end: int,
    *,
    context_lines: int = _EXCERPT_CONTEXT_LINES,
) -> Excerpt:
    """Render a short excerpt around the matched bytes.

    Decodes ``content`` as UTF-8 with replacement (so a stray bad byte
    does not abort the prompt) and slices ``context_lines`` lines on
    each side of the line containing ``span_start``. The match
    end-offset is tolerated when it crosses line boundaries -- the
    excerpt window expands to include every line touched by the match.

    Pure: no I/O, no caching.
    """
    if span_end < span_start:
        span_end = span_start

    text = content.decode("utf-8", errors="replace")
    # Splitlines without keeping trailing newlines so the excerpt
    # printer can join with a renderer-chosen separator.
    file_lines = text.split("\n")
    total = len(file_lines)
    if total == 0:
        return Excerpt(start_line=1, match_line=1, lines=tuple())

    match_line = _byte_span_to_line(content, span_start)
    last_match_line = _byte_span_to_line(content, max(span_end - 1, span_start))

    start_line = max(1, match_line - context_lines)
    end_line = min(total, last_match_line + context_lines)

    rendered: List[Tuple[int, str]] = []
    # ``file_lines`` is 0-indexed; line numbers are 1-based.
    for ln in range(start_line, end_line + 1):
        rendered.append((ln, file_lines[ln - 1]))

    return Excerpt(
        start_line=start_line,
        match_line=match_line,
        lines=tuple(rendered),
    )


def render_full_diff(
    original: bytes,
    rewritten: bytes,
    *,
    fromfile: str = "before",
    tofile: str = "after",
) -> str:
    """Render a unified diff between ``original`` and ``rewritten``.

    Used by the ``[d] show full diff`` branch of the prompt. Decodes
    both inputs as UTF-8 with replacement so a binary-ish file still
    produces a readable diff (the catalog should never target binary
    files, but the renderer is defensive).

    Returns the diff as a single string ready to print. Empty string
    when the two byte strings are identical.
    """
    if original == rewritten:
        return ""

    a = original.decode("utf-8", errors="replace").splitlines(keepends=True)
    b = rewritten.decode("utf-8", errors="replace").splitlines(keepends=True)
    diff = difflib.unified_diff(a, b, fromfile=fromfile, tofile=tofile)
    return "".join(diff)


def format_excerpt_for_prompt(
    excerpt: Excerpt,
    *,
    indent: str = "    ",
    pointer: str = ">",
) -> str:
    """Format an :class:`Excerpt` as printable text for the prompt.

    Each line is prefixed with its 1-based line number. The line
    containing the match is prefixed with ``pointer`` so the operator
    can spot the hit at a glance. Returns a string without a trailing
    newline -- the caller appends its own newline.
    """
    if not excerpt.lines:
        return ""
    width = len(str(excerpt.lines[-1][0]))
    rendered = []
    for ln, text in excerpt.lines:
        marker = pointer if ln == excerpt.match_line else " "
        rendered.append(
            "{indent}{marker} {ln:>{width}} | {text}".format(
                indent=indent,
                marker=marker,
                ln=ln,
                width=width,
                text=text,
            )
        )
    return "\n".join(rendered)
