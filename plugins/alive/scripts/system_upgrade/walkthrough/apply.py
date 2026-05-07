"""Phase-9 walkthrough apply -- writes happen here.

Consumes :class:`WalkthroughDecisions` produced by phase 7 and rewrites
the targeted user-extension files. Per the epic §
Approach: ``decisions.accepted`` carries ``(path, pattern_id,
match_span)`` tuples, NOT pre-computed rewrite bytes. Phase 9 derives
the rewrite payload from the catalog at apply time so all rewrite
logic stays in :mod:`retired_patterns` (single source of truth).

Per-file semantics:

Decisions are grouped by ``path`` so each file is read, backed up, and
written **once**, regardless of how many accepted matches it carries.
Within a file, accepted spans are applied **right-to-left** so byte
offsets captured in phase 7 stay valid as later spans are mutated.
This honours the per-match contract: an operator who skipped one
occurrence of the regex never sees that occurrence rewritten, even if
another occurrence of the same pattern in the same file was accepted.

For ``regex_substitute`` patterns the template is expanded against
the ``matched_bytes`` captured at decide time -- NOT against a global
``re.sub(pattern, ..., file_content)`` -- so each span carries the
exact backref-expanded replacement the operator saw in the prompt.

Atomic-write order (per the epic spec):

1. Backup first: ``<basename>.bak.<UTC-iso-ts>`` sibling, fsync.
2. In-place rewrite, fsync.
3. (Resume marker write happens after both fsyncs; owned by the
   orchestrator, not this module.)

``--ext-migration=backup-only`` writes only the backup; the original
is left untouched.

Read seam: phase 9 reads original bytes via ``read_provider`` so the
apply path is testable through the same overlay-vs-disk seam as verify
. Default ``read_provider`` is
``Path.read_bytes`` -- production callers override it from the
orchestrator's ``--dry-run`` overlay if dry-run apply ever lands
(currently ``--dry-run`` skips phase 9 entirely; see epic § Approach).

Mode preservation:

The catalog targets ``.alive/skills/`` (markdown), ``.alive/rules/``
(markdown), and ``.alive/hooks/`` (shell scripts). Hooks may be
executable. Apply preserves the ORIGINAL file's mode bits when writing
both the ``.bak.<ts>`` sibling and the in-place rewrite, so an
executable hook survives the round-trip. The mode is read via
``os.stat`` on the original path; if the stat fails (overlay-only
read), mode falls back to ``0o644`` (skill / rule default).

Stdlib-only (R10).
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple


__all__ = (
    "AppliedRewrite",
    "SkippedApply",
    "WalkthroughApplyReport",
    "apply",
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppliedRewrite:
    """One successfully applied rewrite.

    Apply now operates on a per-FILE batch
    so a file with multiple accepted matches produces ONE
    :class:`AppliedRewrite` -- not one per match -- with the
    ``pattern_ids`` tuple listing every catalog entry that contributed
    a span to the rewrite.

    Attributes
    ----------
    path:
        Absolute path of the rewritten file.
    backup_path:
        Absolute path of the ``.bak.<ts>`` sibling created before the
        in-place rewrite.
    pattern_ids:
        Tuple of catalog indices whose accepted decisions contributed
        spans to this rewrite. Multiple entries iff the operator
        accepted hits from more than one pattern in this file.
    rewrite_kinds:
        Tuple of ``CATALOG[pid].rewrite_kind`` values, parallel to
        ``pattern_ids``. Echoed so the post-apply summary can render
        the action set in human terms.
    pattern_id:
        Convenience alias -- the FIRST entry of ``pattern_ids``.
        Backward-compatible single-pattern callers (the bulk of the
        catalog at T8) keep reading well; multi-pattern callers should
        consult ``pattern_ids``.
    rewrite_kind:
        Convenience alias -- the FIRST entry of ``rewrite_kinds``.
    backup_only:
        True iff the apply ran with ``decisions.backup_only=True`` and
        the in-place rewrite was deliberately skipped.
    spans_applied:
        Number of accepted spans applied to this file. Equals
        ``len(decisions.accepted)`` filtered by ``path``.
    """

    path: str
    backup_path: str
    pattern_ids: Tuple[int, ...]
    rewrite_kinds: Tuple[str, ...]
    pattern_id: int
    rewrite_kind: str
    spans_applied: int = 1
    backup_only: bool = False


@dataclass(frozen=True)
class SkippedApply:
    """One match that apply could not rewrite.

    ``reason`` is one of:

    * ``"read_failed"``         -- ``read_provider`` raised; the
                                    backup was NOT created.
    * ``"content_drifted"``     -- the bytes at the match span no
                                    longer equal ``decision.matched_bytes``.
                                    apply refuses to rewrite a file
                                    whose content drifted between
                                    decide and apply.
    * ``"catalog_misconfigured"`` -- the catalog entry is
                                    ``walkthrough_eligible`` but lacks
                                    a rewrite payload. Surfaced as a
                                    skip in addition to logging the
                                    raised ``ValueError`` (phase 9
                                    re-raises so the orchestrator can
                                    return non-zero).
    * ``"backup_write_failed"`` -- backup write raised.
    * ``"rewrite_write_failed"`` -- rewrite write raised after the
                                    backup succeeded; the backup is
                                    left in place for forensic recovery.
    """

    path: str
    pattern_id: int
    reason: str
    detail: str = ""


@dataclass
class WalkthroughApplyReport:
    """Output of phase 9. Consumed by the orchestrator's post-walkthrough
    summary (which itself is composed with the phase-7 skipped list)."""

    applied: List[AppliedRewrite] = field(default_factory=list)
    skipped: List[SkippedApply] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_iso_ts(now: Optional[datetime] = None) -> str:
    """Filename-safe UTC timestamp matching the canonical record shape.

    Format: ``YYYY-MM-DDTHH-MM-SS`` (matches the canonical upgrade
    record filenames in ``.alive/upgrades/``). The timezone is always
    UTC; the format omits any timezone suffix so the basename is
    stable across operator locales.

    Codex completion-review fix: the previous ``YYYYMMDD-HHMMSS`` form
    was inconsistent with the rest of the system-upgrade codebase
    (record filenames, resume markers, pre-upgrade tarballs all use
    ``YYYY-MM-DDTHH-MM-SS``). The canary contract
    (``test_canary_retired_pattern_files_backup_or_rewrite``) pins
    the canonical shape on ``.bak.<ts>`` siblings.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%S")


def _generate_span_replacement(
    pattern: Any, matched_bytes: bytes,
) -> bytes:
    """Catalog-driven span-replacement generation.

    Returns the bytes that replace the matched span. Apply uses this
    per-decision so a regex_substitute pattern with multiple hits in
    the same file produces ONE replacement per accepted span -- not
    one global rewrite that ignores the operator's per-occurrence
    skips.

    Dispatch table -- mirrors the rewrite-payload invariant in
    :mod:`retired_patterns` (M9). The catalog is the single source of
    truth; apply NEVER hand-rolls rewrite logic.

    Raises ``ValueError`` when the entry is ``walkthrough_eligible``
    but does not declare a usable rewrite payload (catches
    mis-populated catalog entries -- companion to the catalog
    completeness test in T4).
    """
    if pattern.rewrite_kind == "regex_substitute":
        # Expand the template against the SPAN ONLY -- not the whole
        # file. ``re.sub(pattern, template, matched_bytes)`` operates
        # on a string that, by construction, satisfies the regex (the
        # T3 pre-scan captured the span via ``regex.finditer``), so
        # the substitution produces exactly the backref-expanded
        # replacement for THIS span. Other occurrences in the same
        # file are left to their own decisions.
        try:
            text = matched_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "regex_substitute requires UTF-8-decodable content "
                "(target={}): {}".format(pattern.target_path_glob, exc)
            ) from exc
        rewritten = re.sub(
            pattern.pattern_signature,
            pattern.replacement_template,
            text,
            count=1,  # one expansion -- the span IS the match.
        )
        return rewritten.encode("utf-8")

    if pattern.rewrite_kind == "static_replace":
        return (pattern.replacement_template or "").encode("utf-8")

    if pattern.rewrite_kind == "delete_only":
        return b""

    if pattern.rewrite_fn_id is not None:
        from ..retired_patterns import REWRITE_FN_REGISTRY  # noqa: PLC0415
        fn = REWRITE_FN_REGISTRY.get(pattern.rewrite_fn_id)
        if fn is None:
            raise ValueError(
                "rewrite_fn_id {!r} not registered in REWRITE_FN_REGISTRY "
                "(target={})".format(
                    pattern.rewrite_fn_id, pattern.target_path_glob,
                )
            )
        # The registry callable's contract is unchanged: it takes
        # ``(matched_bytes, full_content)`` and returns the new
        # content; we adapt to the per-span shape by passing
        # ``matched_bytes`` as both arguments. Future fn_id callers
        # that need full-file context should extend the catalog
        # invariant rather than threading file content through this
        # span-shaped helper.
        return fn(matched_bytes, matched_bytes)

    raise ValueError(
        "pattern {} has walkthrough_eligible=True but no rewrite "
        "payload (rewrite_kind={!r}, replacement_template={!r}, "
        "rewrite_fn_id={!r})".format(
            pattern.target_path_glob,
            pattern.rewrite_kind,
            pattern.replacement_template,
            pattern.rewrite_fn_id,
        )
    )


def _generate_rewrite_bytes(
    original_bytes: bytes, pattern: Any, match: Any,
) -> bytes:
    """Whole-file rewrite generation -- used by tests of the catalog
    dispatch contract.

    Per the production apply path uses
    :func:`_generate_span_replacement` (per-span) instead. This helper
    is retained for the catalog dispatch test (the M9 invariant
    assertion is per-pattern, not per-span) and as the shape that
    matches the spec's ``_generate_rewrite_bytes(original, pattern,
    match) -> bytes`` signature.
    """
    if pattern.rewrite_kind == "regex_substitute":
        try:
            text = original_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "regex_substitute requires UTF-8-decodable content "
                "(target={}): {}".format(pattern.target_path_glob, exc)
            ) from exc
        rewritten = re.sub(
            pattern.pattern_signature,
            pattern.replacement_template,
            text,
        )
        return rewritten.encode("utf-8")

    if pattern.rewrite_kind == "static_replace":
        replacement = (pattern.replacement_template or "").encode("utf-8")
        return (
            original_bytes[: match.span_start]
            + replacement
            + original_bytes[match.span_end:]
        )

    if pattern.rewrite_kind == "delete_only":
        return (
            original_bytes[: match.span_start]
            + original_bytes[match.span_end:]
        )

    if pattern.rewrite_fn_id is not None:
        from ..retired_patterns import REWRITE_FN_REGISTRY  # noqa: PLC0415
        fn = REWRITE_FN_REGISTRY.get(pattern.rewrite_fn_id)
        if fn is None:
            raise ValueError(
                "rewrite_fn_id {!r} not registered in REWRITE_FN_REGISTRY "
                "(target={})".format(
                    pattern.rewrite_fn_id, pattern.target_path_glob,
                )
            )
        return fn(original_bytes, match)

    raise ValueError(
        "pattern {} has walkthrough_eligible=True but no rewrite "
        "payload (rewrite_kind={!r}, replacement_template={!r}, "
        "rewrite_fn_id={!r})".format(
            pattern.target_path_glob,
            pattern.rewrite_kind,
            pattern.replacement_template,
            pattern.rewrite_fn_id,
        )
    )


def _backup_path_for(path: str, ts: str) -> str:
    """Return the ``<basename>.bak.<ts>`` sibling path.

    Format matches ``gws.bak.20260407-163602`` -- basename + ``.bak.``
    + UTC timestamp, no extension preserved.
    """
    return path + ".bak." + ts


def _default_read_provider(path: str) -> bytes:
    """Default read seam: read raw bytes from disk.

    Production callers may override with an overlay-aware reader so
    apply remains testable through the same seam as verify.
    """
    with open(path, "rb") as f:
        return f.read()


def _stat_mode(path: str) -> int:
    """Return the original file's mode bits, or 0o644 on stat failure.

    Used to preserve the executable bit on hooks. ``os.stat`` may fail
    when ``read_provider`` is overlay-only (no file on disk); the
    fallback to 0o644 matches the skill / rule default.
    """
    try:
        return os.stat(path).st_mode & 0o7777
    except OSError:
        return 0o644


def _make_default_atomic_write(file_mode: int):
    """Return an atomic-write helper bound to ``file_mode``.

    The helper accepts ``(path, content)`` so callers don't have to
    thread the mode through every test override; we close over it
    here and the apply driver picks the right closure per-file.
    """

    def _write(path: str, content: bytes) -> None:
        from _atomic_io import atomic_write_text  # noqa: PLC0415  (top-level scripts/)
        text = content.decode("utf-8", errors="surrogateescape")
        atomic_write_text(path, text, mode=file_mode)

    return _write


def _default_atomic_write(path: str, content: bytes) -> None:
    """Backward-compatible default writer (mode=0o644).

    Production callers go through :func:`_make_default_atomic_write`
    so the original file's mode is preserved; this name is preserved
    so existing test suites that import the helper directly keep
    working.
    """
    _make_default_atomic_write(0o644)(path, content)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def apply(
    world_root: str,
    decisions: Any,
    *,
    catalog: Optional[List[Any]] = None,
    read_provider: Optional[Callable[[str], bytes]] = None,
    write_fn: Optional[Callable[[str, bytes], None]] = None,
    timestamp: Optional[str] = None,
    now: Optional[datetime] = None,
    mode: str = "real",
) -> WalkthroughApplyReport:
    """Phase-9 entry. Applies decisions; produces ``.bak.<ts>`` siblings.

    Parameters
    ----------
    world_root:
        Resolved world root. Reserved for future containment checks
        (apply currently rewrites whatever paths the decisions list
        names; the orchestrator is responsible for refusing decisions
        whose ``path`` escaped the world root).
    decisions:
        The :class:`WalkthroughDecisions` returned by phase 7. When
        ``decisions.backup_only`` is True, apply writes only the
        ``.bak.<ts>`` sibling and leaves the original untouched.
    catalog:
        Catalog list to resolve ``pattern_id`` -> ``RetiredPattern``.
        Defaults to :data:`retired_patterns.CATALOG`.
    read_provider:
        Overlay-aware byte reader. Defaults to ``open(path, "rb").read()``.
        Tests inject an overlay-aware reader so they can exercise the
        same seam verify uses.
    write_fn:
        Optional override for the atomic byte-write helper. Defaults to
        the package-level ``atomic_write_text`` primitive (which fsyncs
        the temp file, the rename, and the parent directory). Tests
        inject an in-memory writer to assert the order without disk I/O.
        When ``write_fn`` is supplied directly, mode preservation is
        the override's responsibility -- apply does NOT wrap the
        helper. The default writer (used when ``write_fn=None``)
        preserves the original file's mode via ``os.stat``.

        Signature: ``write_fn(path: str, content: bytes) -> None``.
    timestamp:
        Override for the ``.bak.<ts>`` suffix (tests pin the timestamp
        for deterministic assertions). Defaults to a UTC stamp computed
        at call time.
    now:
        Override for the timestamp clock when ``timestamp`` is None;
        ignored otherwise.
    mode:
        ``"real"`` (default) -- writes happen.
        ``"dry_run"`` -- the report is built without writes; tests use
        this to drive the dispatch table without touching disk.

    Returns
    -------
    :class:`WalkthroughApplyReport`. ``apply`` does NOT raise on
    per-decision errors -- those are recorded under ``skipped`` so the
    orchestrator can surface them in the post-walkthrough summary.
    Catalog mis-configuration is the one exception: a ``ValueError``
    from :func:`_generate_span_replacement` is re-raised after
    populating the report so the orchestrator can return non-zero on a
    misconfigured catalog.
    """
    if catalog is None:
        from ..retired_patterns import CATALOG  # noqa: PLC0415
        catalog = CATALOG
    if read_provider is None:
        read_provider = _default_read_provider

    if timestamp is None:
        timestamp = _utc_iso_ts(now)

    report = WalkthroughApplyReport()

    backup_only = bool(getattr(decisions, "backup_only", False))

    raised_value_error: Optional[ValueError] = None

    # ----- Group decisions by file path (preserve insertion order) -----
    grouped: "OrderedDict[str, List[Any]]" = OrderedDict()
    for d in getattr(decisions, "accepted", []):
        grouped.setdefault(d.path, []).append(d)

    # ----- Process each file in one batched read/write -----
    for path, file_decisions in grouped.items():
        # Read the original through the seam, ONCE per file.
        try:
            original = read_provider(path)
        except (FileNotFoundError, KeyError, OSError) as exc:
            for d in file_decisions:
                report.skipped.append(
                    SkippedApply(
                        path=d.path,
                        pattern_id=d.pattern_id,
                        reason="read_failed",
                        detail=str(exc),
                    )
                )
            continue

        # Drift check + span-replacement generation, accumulated as
        # ``(span_start, span_end, replacement_bytes, decision)``
        # tuples. Decisions that fail drift / catalog-misconfig land
        # in report.skipped here and are EXCLUDED from the apply set.
        applicable: List[Tuple[int, int, bytes, Any]] = []
        for d in file_decisions:
            pattern = catalog[d.pattern_id]
            observed = original[d.span_start: d.span_end]
            if d.matched_bytes and observed != d.matched_bytes:
                report.skipped.append(
                    SkippedApply(
                        path=d.path,
                        pattern_id=d.pattern_id,
                        reason="content_drifted",
                        detail=(
                            "expected {!r} at offset {}-{}, observed "
                            "{!r}".format(
                                d.matched_bytes,
                                d.span_start,
                                d.span_end,
                                observed,
                            )
                        ),
                    )
                )
                continue
            try:
                replacement = _generate_span_replacement(
                    pattern, d.matched_bytes,
                )
            except ValueError as exc:
                report.skipped.append(
                    SkippedApply(
                        path=d.path,
                        pattern_id=d.pattern_id,
                        reason="catalog_misconfigured",
                        detail=str(exc),
                    )
                )
                raised_value_error = exc
                continue
            applicable.append(
                (d.span_start, d.span_end, replacement, d),
            )

        if not applicable:
            # Every decision for this file was filtered out (drift /
            # misconfig / etc.). Don't touch the file at all.
            continue

        # Apply spans right-to-left so earlier offsets stay valid as
        # later spans are mutated. Sort by span_start descending; ties
        # broken by span_end descending (equal-start ranges are
        # exotic but deterministic ordering is cheap insurance).
        applicable.sort(
            key=lambda t: (t[0], t[1]),
            reverse=True,
        )

        rewritten_bytes = bytearray(original)
        for span_start, span_end, replacement, _d in applicable:
            rewritten_bytes[span_start: span_end] = replacement

        rewrite_bytes = bytes(rewritten_bytes)

        # Build the AppliedRewrite report for this file.
        pattern_ids = tuple(d.pattern_id for _, _, _, d in applicable)
        rewrite_kinds = tuple(
            str(catalog[d.pattern_id].rewrite_kind) for _, _, _, d in applicable
        )
        backup_path = _backup_path_for(path, timestamp)
        applied_record = AppliedRewrite(
            path=path,
            backup_path=backup_path,
            pattern_ids=pattern_ids,
            rewrite_kinds=rewrite_kinds,
            pattern_id=pattern_ids[0],
            rewrite_kind=rewrite_kinds[0],
            spans_applied=len(applicable),
            backup_only=backup_only,
        )

        # ----- dry-run: skip writes, just record -----
        if mode == "dry_run":
            report.applied.append(applied_record)
            continue

        # Resolve the writer: caller-supplied write_fn (if any) wins;
        # otherwise build a mode-preserving default writer.
        if write_fn is not None:
            file_writer = write_fn
        else:
            file_mode = _stat_mode(path)
            file_writer = _make_default_atomic_write(file_mode)

        # ----- write order: backup first, then in-place rewrite -----
        try:
            file_writer(backup_path, original)
        except OSError as exc:
            for _, _, _, d in applicable:
                report.skipped.append(
                    SkippedApply(
                        path=d.path,
                        pattern_id=d.pattern_id,
                        reason="backup_write_failed",
                        detail=str(exc),
                    )
                )
            continue

        if backup_only:
            # Backup-only mode: do NOT rewrite the original.
            report.applied.append(applied_record)
            continue

        try:
            file_writer(path, rewrite_bytes)
        except OSError as exc:
            # Backup is left on disk for forensic recovery.
            for _, _, _, d in applicable:
                report.skipped.append(
                    SkippedApply(
                        path=d.path,
                        pattern_id=d.pattern_id,
                        reason="rewrite_write_failed",
                        detail=str(exc),
                    )
                )
            continue

        report.applied.append(applied_record)

    if raised_value_error is not None:
        # Report is fully populated above; re-raise so the
        # orchestrator can return non-zero on a misconfigured catalog.
        raise raised_value_error

    return report
