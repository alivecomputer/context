"""Filename-timestamp sweep for prior pre-upgrade tarballs (T5 of fn-18).

Purpose: evict aged-out ``pre-upgrade-<iso-ts>.tar.gz`` files from
``<world>/.alive/upgrades/`` based on the ISO-8601 timestamp parsed
from the filename. ``mtime`` is **never** consulted -- per R18 (and
the Syncthing-corruption pitfall: rsync-style sync tools regularly
overwrite mtime to the current second, which would make a fresh tar
look like a 0-day-old backup even when the filename says it's a year
out of date).

The sweep is deliberately conservative:

* Only files matching the canonical pattern
  ``pre-upgrade-(\\d{4}-\\d{2}-\\d{2}T\\d{2}-\\d{2}-\\d{2})\\.tar\\.gz``
  are candidates. A file like ``pre-upgrade-2026-01-01.bad.tar.gz``
  (anomalous suffix) is left alone.
* Default eviction threshold: age > 30 days. Override via
  ``keep_tarballs`` (integer days). Setting it to 0 evicts everything
  parseable.
* The current run's freshly-written tarball is never swept (the
  caller passes its filename via ``protect`` so even
  ``keep_tarballs=0`` leaves it intact).

Stdlib-only (R10).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Set, Tuple


__all__ = (
    "SweepReport",
    "sweep_tarballs",
    "DEFAULT_KEEP_DAYS",
    "PRE_UPGRADE_FILENAME_RE",
)


#: Default age threshold for eviction (in days). Filenames older than
#: this -- as parsed from the timestamp portion -- are removed.
DEFAULT_KEEP_DAYS: int = 30


#: Strict regex matching the canonical pre-upgrade tarball filename.
#: Anchors at start AND end so anomalous suffixes (``.bad.tar.gz``
#: etc.) DO NOT match and are therefore never swept.
PRE_UPGRADE_FILENAME_RE = re.compile(
    r"^pre-upgrade-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})\.tar\.gz$"
)


#: Torn-write partial tarball name (``backup.py`` writes to
#: ``.pre-upgrade-<iso-ts>.tar.gz.tmp`` then ``os.replace``s to the
#: final name). A crash between create-and-replace leaves this
#: partial behind. Sweep's atomic-backup contract requires it to be
#: cleaned -- per the epic spec § Atomic backup, "only the .tmp may
#: exist (and is cleaned by sweep)". The same age threshold applies
#: as for final tarballs.
PRE_UPGRADE_TMP_FILENAME_RE = re.compile(
    r"^\.pre-upgrade-"
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})"
    r"\.tar\.gz\.tmp$"
)


#: Filenames the sweep recognises as legitimate ``.alive/upgrades/``
#: artifacts owned by other phases (per the standardized-paths
#: scheme). These are NEITHER swept NOR reported under
#: ``unrecognised[]`` -- the sweep targets pre-upgrade tarballs only;
#: upgrade-record + resume-marker + runstate + retroactive YAMLs are
#: maintained by their own phases. ``MANIFEST`` is the in-tarball
#: manifest copied alongside fixture extracts; treated as benign.
#:
#: Patterns are anchored regexes evaluated against the basename.
_RECOGNISED_NON_TARBALL_PATTERNS = tuple(
    re.compile(p) for p in (
        # Final upgrade record: <iso-ts>.yaml (no suffix)
        r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.yaml$",
        # Resume marker: <iso-ts>-resume.yaml
        r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-resume\.yaml$",
        # Retroactive synthesized record: <iso-ts>-retroactive.yaml
        r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-retroactive\.yaml$",
        # Run-state log: <iso-ts>-runstate.yaml
        r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-runstate\.yaml$",
        # Tarball MANIFEST copy (extracted fixtures may end up next to
        # the tarball; treated as a benign sibling).
        r"^MANIFEST$",
    )
)


def _is_recognised_non_tarball(name: str) -> bool:
    """Return True if *name* is a legitimate non-tarball upgrade artifact."""
    for pat in _RECOGNISED_NON_TARBALL_PATTERNS:
        if pat.match(name):
            return True
    return False


@dataclass
class SweepReport:
    """Outcome of :func:`sweep_tarballs`.

    Attributes
    ----------
    deleted : list[str]
        Absolute paths swept successfully.
    retained : list[str]
        Absolute paths considered but kept (within age window OR
        ``protect``-listed).
    unrecognised : list[str]
        Absolute paths in the upgrades dir that did NOT match the
        canonical filename pattern. Surfaced for forensics so an
        operator notices accidental files (e.g. a stray
        ``pre-upgrade-2026-01-01.bad.tar.gz``) before they accumulate.
    submodule_skipped : list[str]
        Per-walnut tarballs declined because the walnut is a submodule
        and the caller asked for in-walnut sweeping. Currently unused
        by the world-level sweep (kept for symmetry with the cleanup
        report so future per-walnut sweep variants share the shape).
    errors : list[tuple[str, str]]
        ``(path, reason)`` pairs for individual file failures. The
        sweep continues past errors -- one bad tarball never blocks
        the rest.
    """

    deleted: List[str] = field(default_factory=list)
    retained: List[str] = field(default_factory=list)
    unrecognised: List[str] = field(default_factory=list)
    submodule_skipped: List[str] = field(default_factory=list)
    errors: List[Tuple[str, str]] = field(default_factory=list)


def _parse_filename_timestamp(name: str) -> Optional[datetime]:
    """Parse the canonical tarball timestamp; return ``None`` on miss.

    Matches only ``pre-upgrade-<iso-ts>.tar.gz``. ``.tmp`` partials
    are parsed via :func:`_parse_tmp_filename_timestamp` so callers
    can distinguish the two and apply the right deletion policy.
    """
    m = PRE_UPGRADE_FILENAME_RE.match(name)
    if m is None:
        return None
    yr, mo, dy, hr, mn, sc = (int(g) for g in m.groups())
    try:
        return datetime(
            year=yr, month=mo, day=dy, hour=hr, minute=mn, second=sc,
            tzinfo=timezone.utc,
        )
    except ValueError:
        # Out-of-range component (e.g. month 13). Treat as
        # unparseable; the caller surfaces it under ``unrecognised``.
        return None


def _parse_tmp_filename_timestamp(name: str) -> Optional[datetime]:
    """Parse the timestamp of a torn-write ``.tmp`` partial, or ``None``."""
    m = PRE_UPGRADE_TMP_FILENAME_RE.match(name)
    if m is None:
        return None
    yr, mo, dy, hr, mn, sc = (int(g) for g in m.groups())
    try:
        return datetime(
            year=yr, month=mo, day=dy, hour=hr, minute=mn, second=sc,
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def sweep_tarballs(
    world_root_resolved: str,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    protect: Optional[Iterable[str]] = None,
    now: Optional[datetime] = None,
) -> SweepReport:
    """Evict aged-out pre-upgrade tarballs.

    Parameters
    ----------
    world_root_resolved : str
        Realpath'd world root. The sweep operates only on
        ``<world>/.alive/upgrades/``; callers should not pass per-
        walnut paths -- there is one canonical tarball location.
    keep_days : int
        Age threshold in days. Files whose **filename** timestamp is
        more than *keep_days* old are deleted. Setting *keep_days* to
        ``0`` evicts every parseable tarball EXCEPT those listed in
        *protect*.
    protect : iterable[str], optional
        Filenames (basenames OR absolute paths) that MUST NOT be
        swept regardless of age. The orchestrator passes the current
        run's freshly-written tarball here so the canary
        ``keep_tarballs=0`` test still leaves the working tarball
        alone.
    now : datetime, optional
        Override "current time" -- used by tests to drive
        deterministic eviction without monkey-patching ``datetime``.
        Must be timezone-aware UTC.

    Returns
    -------
    SweepReport
        Bucketed sweep outcome.
    """
    if keep_days < 0:
        raise ValueError(
            "keep_days must be >= 0 (got {!r})".format(keep_days)
        )
    upgrades_dir = os.path.join(
        world_root_resolved, ".alive", "upgrades",
    )
    report = SweepReport()
    if not os.path.isdir(upgrades_dir):
        return report

    if now is None:
        now = datetime.now(timezone.utc)

    protect_basenames: Set[str] = set()
    if protect:
        for p in protect:
            if not p:
                continue
            protect_basenames.add(os.path.basename(p))

    try:
        entries = sorted(os.listdir(upgrades_dir))
    except OSError as exc:
        report.errors.append((upgrades_dir, "listdir-error:{}".format(exc)))
        return report

    for name in entries:
        full = os.path.join(upgrades_dir, name)
        # Only operate on regular files; skip the staging dir, the
        # ``.tmp`` partial (if any), the ``MANIFEST``, and -- crucially
        # -- never recurse into subdirectories.
        try:
            if not os.path.isfile(full):
                continue
        except OSError:
            continue
        ts = _parse_filename_timestamp(name)
        is_tmp_partial = False
        if ts is None:
            # ``.tmp`` torn-write partials get the same age policy as
            # final tarballs. Per the atomic-backup contract, sweep
            # is responsible for cleaning them up after a crash.
            ts = _parse_tmp_filename_timestamp(name)
            is_tmp_partial = ts is not None
        if ts is None:
            # Distinguish legitimate sibling artifacts (upgrade
            # records, resume markers, runstate, MANIFEST) from
            # unexpected files. The recognised set is owned by other
            # phases and the sweep neither evicts nor flags them.
            if _is_recognised_non_tarball(name):
                continue
            report.unrecognised.append(full)
            continue
        if name in protect_basenames or full in protect_basenames:
            report.retained.append(full)
            continue
        # Torn-write partials are crash residue and never legitimate
        # in a healthy world; evict regardless of age (a partial
        # < 1 day old still indicates the prior run aborted between
        # tar-create and ``os.replace`` and the partial is not
        # consumable by anyone).
        if is_tmp_partial:
            try:
                os.unlink(full)
            except FileNotFoundError:
                continue
            except OSError as exc:
                report.errors.append((full, "unlink-error:{}".format(exc)))
                continue
            report.deleted.append(full)
            continue
        # Age computation uses filename timestamp -- NEVER mtime.
        age = now - ts
        # Convert to days as a float so a 30.5-day tarball with
        # ``keep_days=30`` is correctly evicted.
        age_days = age.total_seconds() / 86400.0
        if age_days <= keep_days:
            report.retained.append(full)
            continue
        try:
            os.unlink(full)
        except FileNotFoundError:
            # Lost the race; treat as a clean no-op.
            continue
        except OSError as exc:
            report.errors.append((full, "unlink-error:{}".format(exc)))
            continue
        report.deleted.append(full)

    return report
