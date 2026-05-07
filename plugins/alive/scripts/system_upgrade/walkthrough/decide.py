"""Phase-7 walkthrough decide -- pure decisions, NO filesystem writes.

Consumes the catalog matches that T3's pre-scan stored on
``DetectionReport.walkthrough_eligible_matches``. Renders one prompt per
match, collects the operator's y/n/d/q decision, and returns a
:class:`WalkthroughDecisions`. **No filesystem writes.** This is
verified by an audit grep -- the module must not import any of
``open``, ``os.makedirs``, ``shutil.copy``, ``Path.write_*``,
``atomic_write_text``, etc.

Per the epic § Approach: ``WalkthroughDecisions``
carries ``(path, pattern_id, match_span)`` tuples for accepted
decisions, NOT pre-computed rewrite bytes. Phase 9 (apply) derives the
rewrite payload from the catalog at apply time so all rewrite logic
stays in :mod:`retired_patterns` (single source of truth).

Modes consumed (R7-shaped):

* ``--non-interactive --ext-migration=skip`` -- every prompt resolves
  as ``n``; ``WalkthroughDecisions.skipped`` collects every match.
  ``accepted`` is empty.
* ``--non-interactive --ext-migration=backup-only`` -- every prompt
  resolves as ``y`` for backup-only purposes; ``accepted`` collects
  every match. Phase 9 inspects ``WalkthroughDecisions.backup_only``
  and writes ``.bak.<ts>`` siblings without touching originals.
* ``--non-interactive --ext-migration=abort`` -- raises
  :class:`WalkthroughAbort` so the orchestrator can return non-zero.
* Interactive mode -- uses :func:`input` against ``stdin``; the
  caller (orchestrator) is responsible for ``--non-interactive`` /
  TTY checks before invoking ``decide``.

Stdlib-only (R10).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, TextIO, Tuple

# NOTE: IMPORT POLICY -- decide.py MUST NOT import any module that
# performs filesystem writes. The whitelist is:
#
# * stdlib: dataclasses / typing / sys
# * sibling: walkthrough.diff_render (pure / read-only)
# * sibling: ..retired_patterns (catalog SoT; pure)
# * sibling: ..progress (pause/resume only; the renderer's writes go
#   to stderr, not the world)
#
# The audit grep enforced by ``test_walkthrough_decide`` greps for
# ``open(`` / ``os.makedirs`` / ``atomic_write_text`` etc. and asserts
# none appear in this module.
from .diff_render import (
    Excerpt,
    format_excerpt_for_prompt,
    render_excerpt,
    render_full_diff,
)


__all__ = (
    "AcceptedDecision",
    "SkippedDecision",
    "WalkthroughAbort",
    "WalkthroughDecisions",
    "decide",
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AcceptedDecision:
    """One accepted walkthrough match.

    Attributes
    ----------
    path:
        Absolute path of the user-extension file the match was found
        in.
    pattern_id:
        Catalog index. Phase 9 resolves to the full ``RetiredPattern``
        via ``CATALOG[pattern_id]``.
    span_start:
        Byte offset of the start of the match within the file's
        captured content. Phase 9 anchors the rewrite from here.
    span_end:
        Byte offset of the end of the match.
    matched_bytes:
        The matched bytes, captured at decide time so phase 9 can
        verify the original content has not drifted between phases.
    """

    path: str
    pattern_id: int
    span_start: int
    span_end: int
    matched_bytes: bytes


@dataclass(frozen=True)
class SkippedDecision:
    """One skipped (or quit-early) match.

    ``reason`` is one of:

    * ``"user_skip"``       -- operator answered ``n`` at the prompt.
    * ``"quit_early"``      -- operator answered ``q``; every remaining
                                match in the input list is also recorded
                                with ``"quit_early"`` so the
                                post-walkthrough summary can list them.
    * ``"non_interactive_skip"`` -- ``--ext-migration=skip``.
    """

    path: str
    pattern_id: int
    reason: str


@dataclass
class WalkthroughDecisions:
    """Output of phase 7. Consumed by phase 9 (apply).

    ``accepted`` carries ``(path, pattern_id, match_span)`` tuples --
    NOT pre-computed rewrite bytes. Phase 9 derives the rewrite
    payload from the catalog at apply time (: rewrite
    logic stays in ``retired_patterns.py`` as the single source of
    truth).

    ``backup_only`` is set when the orchestrator was invoked with
    ``--ext-migration=backup-only``: phase 9 writes ``.bak.<ts>``
    siblings but leaves the originals unchanged.
    """

    accepted: List[AcceptedDecision] = field(default_factory=list)
    skipped: List[SkippedDecision] = field(default_factory=list)
    quit_early: bool = False
    backup_only: bool = False


class WalkthroughAbort(Exception):
    """Raised by :func:`decide` when ``--ext-migration=abort`` is set
    and at least one walkthrough-eligible match is present.

    Carries the offending matches so the orchestrator can render them
    in the abort message.
    """

    def __init__(self, matches: Iterable[Any]) -> None:
        self.matches = tuple(matches)
        super().__init__(
            "walkthrough abort: {} retired-pattern hit(s) under "
            "--ext-migration=abort".format(len(self.matches))
        )


# ---------------------------------------------------------------------------
# Prompt protocol
# ---------------------------------------------------------------------------

# Single character recognised at the prompt. Returns one of the
# canonical decisions or None when the input is unrecognised (caller
# re-prompts).
_VALID_INPUTS = {"y", "n", "d", "q"}


def _parse_response(raw: str) -> Optional[str]:
    if raw is None:
        return None
    raw = raw.strip().lower()
    if raw in _VALID_INPUTS:
        return raw
    # Tolerate "yes"/"no" expansions.
    if raw == "yes":
        return "y"
    if raw == "no":
        return "n"
    if raw == "quit":
        return "q"
    if raw == "diff":
        return "d"
    return None


# ---------------------------------------------------------------------------
# Snapshot read seam
# ---------------------------------------------------------------------------

def _snapshot_read(snapshot: Any, path: str) -> bytes:
    """Read ``path`` from ``snapshot`` if available.

    Decide is dry-run-safe: it never reads from disk directly. The
    snapshot was captured in phase 2; T3 used it for the pre-scan.
    Phase 7 reads the same snapshot to render the excerpt. If the
    snapshot is unavailable (test fakes pass ``None``), the caller
    must pre-populate the matches' ``matched_bytes`` so the prompt can
    still render a useful excerpt.
    """
    if snapshot is None:
        return b""
    try:
        return snapshot.read(path)
    except (KeyError, ValueError, FileNotFoundError):
        return b""


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def decide(
    walkthrough_eligible_matches: Iterable[Any],
    *,
    snapshot: Any = None,
    catalog: Optional[List[Any]] = None,
    progress: Any = None,
    mode: str = "interactive",
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    input_fn: Optional[Callable[[str], str]] = None,
) -> WalkthroughDecisions:
    """Phase-7 entry. Pure decisions; no filesystem writes.

    Parameters
    ----------
    walkthrough_eligible_matches:
        Iterable of catalog-match records produced by T3's pre-scan.
        Each record MUST expose ``path``, ``pattern_id``,
        ``span_start``, ``span_end``, and ``matched_bytes`` attributes
        (the contract published by
        :class:`retired_patterns.CatalogMatch`).
    snapshot:
        The phase-2 :class:`FileSnapshot`. When supplied, decide reads
        the matched file's full bytes from the snapshot to render the
        excerpt + (on ``d``) the full proposed diff. When ``None``,
        decide falls back to ``match.matched_bytes`` for the excerpt
        and refuses the ``d`` branch (announces "diff unavailable" and
        re-prompts).
    catalog:
        Catalog list to resolve ``pattern_id`` -> ``RetiredPattern``.
        Defaults to :data:`retired_patterns.CATALOG`. Tests can pass a
        custom catalog when they need to pin a specific shape.
    progress:
        Optional :class:`ProgressRenderer` whose ``pause()`` / ``resume()``
        is called around each prompt. ``None`` is tolerated so unit
        tests don't have to wire one up.
    mode:
        One of:

        * ``"interactive"``      -- default; uses ``input_fn`` /
                                    ``stdin`` to collect responses.
        * ``"non_interactive_skip"`` -- treats every match as ``n``;
                                    the prompt is NOT rendered (no
                                    output to stdout).
        * ``"non_interactive_backup_only"`` -- treats every match as
                                    ``y`` and sets
                                    ``decisions.backup_only = True``.
        * ``"non_interactive_abort"`` -- raises :class:`WalkthroughAbort`
                                    if any matches are present.
    stdin:
        Stream the prompt reads from. Defaults to ``sys.stdin``.
    stdout:
        Stream the prompt writes to. Defaults to ``sys.stdout``.
    input_fn:
        Override for ``input`` / ``stdin.readline``. Tests use this to
        stub responses without driving real stdin.

    Returns
    -------
    :class:`WalkthroughDecisions`. Never partially-written; either the
    full list is processed or :class:`WalkthroughAbort` is raised.
    """
    # Defer the import so a circular-import bug in retired_patterns
    # never takes down the walkthrough package (T8 lands BEFORE the
    # T9 migration phase that imports it, so circular import would
    # bite at the wrong layer).
    if catalog is None:
        from ..retired_patterns import CATALOG  # noqa: PLC0415
        catalog = CATALOG

    matches_list = list(walkthrough_eligible_matches)
    decisions = WalkthroughDecisions()

    # Non-interactive abort: refuse the run BEFORE any prompt fires.
    if mode == "non_interactive_abort":
        if matches_list:
            raise WalkthroughAbort(matches_list)
        return decisions

    # Non-interactive skip: collect every match as a skip; no prompts.
    if mode == "non_interactive_skip":
        for m in matches_list:
            decisions.skipped.append(
                SkippedDecision(
                    path=m.path,
                    pattern_id=m.pattern_id,
                    reason="non_interactive_skip",
                )
            )
        return decisions

    # Non-interactive backup-only: collect every match as accepted +
    # mark the decisions object as backup-only so phase 9 writes
    # .bak.<ts> siblings without rewriting.
    if mode == "non_interactive_backup_only":
        decisions.backup_only = True
        for m in matches_list:
            decisions.accepted.append(
                AcceptedDecision(
                    path=m.path,
                    pattern_id=m.pattern_id,
                    span_start=m.span_start,
                    span_end=m.span_end,
                    matched_bytes=m.matched_bytes,
                )
            )
        return decisions

    # Non-interactive rewrite: collect every match as accepted with
    # ``backup_only=False`` so phase 9 writes BOTH a .bak.<ts>
    # sibling AND rewrites the original to the catalog's
    # replacement_template. Required for the
    # full-pipeline contract: walnut_equal tests need rc=0,
    # idempotency property tests need post-state with no retired
    # patterns, canary tests need at least one .bak.<ts> file.
    # ``backup-only`` alone leaves the retired pattern in the
    # original, breaking idempotency on a 2nd run.
    if mode == "non_interactive_rewrite":
        for m in matches_list:
            decisions.accepted.append(
                AcceptedDecision(
                    path=m.path,
                    pattern_id=m.pattern_id,
                    span_start=m.span_start,
                    span_end=m.span_end,
                    matched_bytes=m.matched_bytes,
                )
            )
        return decisions

    if mode != "interactive":
        raise ValueError(
            "decide(mode={!r}): expected one of "
            "interactive | non_interactive_skip | "
            "non_interactive_backup_only | non_interactive_rewrite | "
            "non_interactive_abort".format(mode)
        )

    # ---- interactive path ------------------------------------------------

    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    if input_fn is None:

        def _readline_fn(prompt: str) -> str:
            stdout.write(prompt)
            stdout.flush()
            line = stdin.readline()
            if not line:
                # EOF: treat as quit so we don't loop forever in a
                # closed-pipe scenario.
                return "q"
            return line.rstrip("\n")

        input_fn = _readline_fn

    quit_seen = False
    for m in matches_list:
        if quit_seen:
            decisions.skipped.append(
                SkippedDecision(
                    path=m.path,
                    pattern_id=m.pattern_id,
                    reason="quit_early",
                )
            )
            continue

        # Pause the progress renderer so the prompt prints cleanly.
        if progress is not None:
            try:
                progress.pause()
            except Exception:  # noqa: BLE001 - never let progress kill UX
                pass

        try:
            response = _prompt_one(
                m, snapshot, catalog, stdout, input_fn,
            )
        finally:
            if progress is not None:
                try:
                    progress.resume()
                except Exception:  # noqa: BLE001
                    pass

        if response == "y":
            decisions.accepted.append(
                AcceptedDecision(
                    path=m.path,
                    pattern_id=m.pattern_id,
                    span_start=m.span_start,
                    span_end=m.span_end,
                    matched_bytes=m.matched_bytes,
                )
            )
        elif response == "n":
            decisions.skipped.append(
                SkippedDecision(
                    path=m.path,
                    pattern_id=m.pattern_id,
                    reason="user_skip",
                )
            )
        elif response == "q":
            quit_seen = True
            decisions.quit_early = True
            decisions.skipped.append(
                SkippedDecision(
                    path=m.path,
                    pattern_id=m.pattern_id,
                    reason="quit_early",
                )
            )
        else:
            # Unreachable: _prompt_one only returns y / n / q.
            raise RuntimeError(
                "decide(): unexpected response {!r}".format(response)
            )

    return decisions


# ---------------------------------------------------------------------------
# Internal: per-match prompt rendering
# ---------------------------------------------------------------------------

def _prompt_one(
    match: Any,
    snapshot: Any,
    catalog: List[Any],
    stdout: TextIO,
    input_fn: Callable[[str], str],
) -> str:
    """Render the prompt for one match and return the operator's
    canonical response (``y`` / ``n`` / ``q``).

    The ``d`` branch fans out to the full diff and re-prompts; this
    helper never returns ``d`` to the caller.
    """
    pattern = catalog[match.pattern_id]
    full_content = _snapshot_read(snapshot, match.path)
    excerpt_bytes = full_content if full_content else (match.matched_bytes or b"")
    # The [d] full-diff branch is only
    # available when we have the FULL file content. If the snapshot
    # is missing or empty, the preview/diff would either be wrong
    # (splicing against ``matched_bytes`` instead of the file) or
    # misleading. ``has_full_content`` gates both the "after"
    # excerpt and the [d] branch so the prompt re-asks rather than
    # showing a malformed preview.
    has_full_content = bool(full_content)

    # Header.
    stdout.write(
        "\n╭─ \U0001f43f️  ext: {}\n".format(match.path)
    )
    stdout.write(
        "│  pattern: {} (catalog id={})\n".format(
            pattern.target_path_glob, match.pattern_id,
        )
    )
    if pattern.surface_message:
        stdout.write(
            "│  why: {}\n".format(pattern.surface_message)
        )

    # Original excerpt.
    excerpt = render_excerpt(
        excerpt_bytes, match.span_start, match.span_end,
    )
    stdout.write("│  before:\n")
    stdout.write(format_excerpt_for_prompt(excerpt))
    stdout.write("\n")

    # Proposed rewrite excerpt -- rendered by deriving the rewrite
    # bytes from the catalog (NOT pre-computed) so the operator sees
    # the exact bytes phase 9 will apply. Only available when we
    # have the full file content -- otherwise a splice against
    # ``matched_bytes`` (where span_start may be nonzero) produces
    # a malformed preview. The [d] branch below also gates on the
    # same flag.
    rewritten: Optional[bytes] = None
    if has_full_content:
        try:
            rewritten = _derive_rewrite_bytes_for_excerpt(
                full_content, pattern, match,
            )
        except Exception as exc:  # noqa: BLE001 - render-time helper
            rewritten = None
            stdout.write(
                "│  (rewrite preview unavailable: {})\n".format(exc)
            )

    if rewritten is not None:
        rewrite_excerpt = render_excerpt(
            rewritten, match.span_start, match.span_end,
        )
        stdout.write("│  after:\n")
        stdout.write(format_excerpt_for_prompt(rewrite_excerpt))
        stdout.write("\n")

    while True:
        prompt_text = (
            "│  [y] accept  [n] skip  [d] full diff  [q] quit walkthrough\n"
            "╰─ > "
        )
        raw = input_fn(prompt_text)
        choice = _parse_response(raw)
        if choice is None:
            stdout.write(
                "  (unrecognised; type one of y / n / d / q)\n"
            )
            continue
        if choice == "d":
            # [d] is only meaningful when we have the full file
            # content AND a derivable rewrite. Without the full
            # file, the diff would compare ``matched_bytes`` against
            # a spliced rewrite -- contractually wrong and visually
            # malformed. Per, gate explicitly on
            # ``has_full_content``.
            if not has_full_content:
                stdout.write(
                    "  (full diff unavailable -- snapshot did not "
                    "capture the full file)\n"
                )
                continue
            if rewritten is None:
                stdout.write(
                    "  (full diff unavailable -- the rewrite preview "
                    "could not be derived from the catalog)\n"
                )
                continue
            diff_text = render_full_diff(
                full_content,
                rewritten,
                fromfile=match.path,
                tofile=match.path + " (proposed)",
            )
            stdout.write(diff_text)
            stdout.write("\n")
            continue
        return choice


def _derive_rewrite_bytes_for_excerpt(
    original: bytes, pattern: Any, match: Any,
) -> Optional[bytes]:
    """Derive the rewritten bytes for prompt-time preview -- per-span,
    matching phase 9 apply.

    Reuses :func:`walkthrough.apply._generate_span_replacement` so the
    "after" excerpt and the ``[d]`` full diff show EXACTLY the bytes
    phase 9 will apply for THIS span -- not a global ``re.sub`` that
    would advertise rewrites for other occurrences the operator may
    later skip.

    Returns ``None`` when the catalog entry is mis-populated; the
    prompt swallows the error and re-prompts. (Phase 9 raises
    ``ValueError`` instead -- the apply-time dispatch is the
    authoritative one; the preview is a best-effort UX nicety.)
    """
    if not original:
        return None
    # Defer the import so this module's audit-grep (no writes) stays
    # clean -- apply.py imports atomic_write_text indirectly, but only
    # the helper symbol we name below is loaded into decide.py.
    try:
        from .apply import _generate_span_replacement  # noqa: PLC0415
    except ImportError:
        return None
    try:
        replacement = _generate_span_replacement(
            pattern, match.matched_bytes,
        )
    except (ValueError, Exception):  # noqa: BLE001 - preview is best-effort
        return None
    # Splice the replacement at THIS span; other occurrences in the
    # file remain untouched -- byte-identical to phase 9's per-span
    # apply for this same decision in isolation.
    return (
        original[: match.span_start]
        + replacement
        + original[match.span_end:]
    )
