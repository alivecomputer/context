"""Phase-N ``--rollback`` flag: list + extract pre-upgrade tarballs.

Read-side only (T11 of fn-18). Two modes, dispatched by the CLI:

* **List mode** (``--rollback`` with no argument): scan
  ``<world>/.alive/upgrades/`` for files matching the canonical
  ``pre-upgrade-<iso-ts>.tar.gz`` pattern, return them sorted by
  timestamp descending with size + relative-age columns.
* **Extract mode** (``--rollback <iso-ts>``): locate
  ``<world>/.alive/upgrades/pre-upgrade-<ts>.tar.gz``; extract via
  ``_alive_common.tarball.safe_tar_extract`` (LD22 guard preserved)
  into ``<world>/.alive/.rollback-<ts>/``. The tarball's top-level
  ``MANIFEST`` text file (T5 writes one) lists exact restoration roots
  -- both ``.alive/`` and per-walnut ``_kernel/`` paths -- and drives
  the printed restore-procedure block.

Containment + path standardization:
    The extracted directory ALWAYS lives at
    ``<world>/.alive/.rollback-<ts>/`` (UNDER ``.alive/``, not at world
    root). T5's backup excludes ``.alive/.rollback-*/`` so a freshly
    extracted rollback is never reincluded in a subsequent backup.
    Never extract to ``/tmp``, never to the world-root variant
    ``<world>/.alive.rollback-<ts>/``, never anywhere else.

Full automated swap (current ``.alive/`` aside, payload moved into
place) is **deferred to v3.3** per R17. T11 ships only the extract +
print flow; the printed procedure documents the manual swap with
EXACT paths from the manifest (so restore is mechanical, not
guesswork).

Stdlib-only (R10).
"""

from __future__ import annotations

import os
import re
import shlex
import tarfile
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


__all__ = (
    "LATEST_SENTINEL",
    "TarballEntry",
    "ListReport",
    "ExtractReport",
    "RollbackError",
    "list_tarballs",
    "extract_tarball",
    "format_list_report",
    "format_restore_procedure",
    "build_post_upgrade_pointer",
)


#: Sentinel used by the CLI when the operator passed ``--rollback`` with
#: no argument. The CLI's argparse ``const`` sets the namespace value
#: to this string; the dispatcher in :func:`run_rollback` interprets it
#: as "list mode".
LATEST_SENTINEL: str = "LATEST"


#: Canonical filename pattern for pre-upgrade tarballs. T5 produces
#: filenames matching this pattern; T11's listing + extraction MUST
#: refuse anything else.
_PRE_UPGRADE_RE = re.compile(
    r"^pre-upgrade-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})\.tar\.gz$"
)


#: Canonical ISO-8601 timestamp pattern (filename-safe variant where
#: ``:`` separators are replaced by ``-``).
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Public dataclasses + exceptions
# ---------------------------------------------------------------------------


class RollbackError(Exception):
    """Recoverable rollback failure carrying an actionable error code.

    ``error_code`` matches the CLI's ``error_code`` envelope vocabulary:

    * ``rollback_target_not_found`` -- no tarball at the requested ts
      (exit 3).
    * ``rollback_no_upgrades_dir`` -- ``.alive/upgrades/`` is absent
      (e.g. world has never been upgraded; exit 3).
    * ``rollback_invalid_timestamp`` -- the timestamp argument did not
      match the canonical ``YYYY-MM-DDTHH-MM-SS`` shape (exit 1).
    * ``rollback_extract_failed`` -- ``safe_tar_extract`` refused (LD22
      guard) or the underlying tar was corrupt (exit 1).
    * ``rollback_target_already_extracted`` -- the destination
      ``.alive/.rollback-<ts>/`` already exists; the operator must
      remove it (or pick a different ts) before re-extracting
      (exit 1).
    * ``rollback_permission`` -- a filesystem permission error
      prevented reading or writing required state (e.g. unreadable
      ``.alive/upgrades/`` directory, unreadable tarball, or write
      failure into the rollback destination). Surfaces as exit 4 to
      match the CLI's documented permission-error contract; the
      operator's actionable next step is fixing perms, not retrying
      with a different timestamp.

    ``exit_code`` mirrors the CLI's exit-code convention (3 = not
    found, 4 = permission, 1 = generic refusal).
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.exit_code = exit_code


@dataclass(frozen=True)
class TarballEntry:
    """One pre-upgrade tarball discovered under ``.alive/upgrades/``.

    Attributes
    ----------
    timestamp : str
        Filename-safe ISO-8601 timestamp (``YYYY-MM-DDTHH-MM-SS``).
    absolute_path : str
        Full path to the ``.tar.gz`` on disk.
    size_bytes : int
        ``stat.st_size`` of the tarball.
    age_seconds : int
        Seconds elapsed between the timestamp and ``now`` at list time
        (clamped to >= 0). The CLI renders this via
        :func:`_format_relative_age`.
    """

    timestamp: str
    absolute_path: str
    size_bytes: int
    age_seconds: int


@dataclass
class ListReport:
    """Outcome of :func:`list_tarballs`.

    ``entries`` is sorted by timestamp DESCENDING (newest first).
    ``upgrades_dir_present`` distinguishes "no upgrades have ever
    happened" (False) from "upgrades happened but everything was
    swept" (True with empty entries).
    """

    upgrades_dir: str
    upgrades_dir_present: bool
    entries: List[TarballEntry] = field(default_factory=list)


@dataclass
class ExtractReport:
    """Outcome of :func:`extract_tarball`.

    Attributes
    ----------
    tarball_path : str
        Source archive that was extracted.
    extract_dir : str
        Destination dir, always ``<world>/.alive/.rollback-<ts>/``.
    manifest_entries : list[str]
        World-relative restoration roots read out of the tarball's
        top-level ``MANIFEST`` file. Sorted; empty when the tarball
        carried no manifest.
    """

    tarball_path: str
    extract_dir: str
    manifest_entries: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upgrades_dir(world_root_resolved: str) -> str:
    """Return ``<world>/.alive/upgrades/`` (no existence check)."""
    return os.path.join(world_root_resolved, ".alive", "upgrades")


def _rollback_dir(world_root_resolved: str, iso_ts: str) -> str:
    """Return ``<world>/.alive/.rollback-<iso_ts>/`` (canonical path)."""
    return os.path.join(
        world_root_resolved, ".alive", ".rollback-{}".format(iso_ts),
    )


def _parse_iso_ts(ts: str) -> Optional[float]:
    """Convert a filename-safe ISO timestamp to UNIX seconds.

    Returns ``None`` on parse failure. The input is assumed to be UTC
    (matching T5's emission convention via ``datetime.now(timezone.utc)``).
    Stdlib-only -- ``time.strptime`` + ``calendar.timegm``.
    """
    import calendar  # noqa: PLC0415
    try:
        struct = time.strptime(ts, "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None
    try:
        return float(calendar.timegm(struct))
    except (ValueError, OverflowError):
        return None


def _format_relative_age(age_seconds: int) -> str:
    """Format *age_seconds* as a human-readable relative string.

    Output examples: ``"3m ago"``, ``"42m ago"``, ``"2h ago"``,
    ``"3d ago"``. Negative values clamp to ``"just now"`` (clock skew
    tolerance).
    """
    if age_seconds < 60:
        return "just now"
    if age_seconds < 3600:
        return "{}m ago".format(age_seconds // 60)
    if age_seconds < 86400:
        return "{}h ago".format(age_seconds // 3600)
    return "{}d ago".format(age_seconds // 86400)


def _format_size(n: int) -> str:
    """Format byte count into K / M / G with one decimal place."""
    if n < 1024:
        return "{}B".format(n)
    if n < 1024 * 1024:
        return "{:.1f}K".format(n / 1024.0)
    if n < 1024 * 1024 * 1024:
        return "{:.1f}M".format(n / (1024.0 * 1024.0))
    return "{:.1f}G".format(n / (1024.0 * 1024.0 * 1024.0))


# ---------------------------------------------------------------------------
# List mode
# ---------------------------------------------------------------------------


def list_tarballs(
    world_root_resolved: str,
    *,
    now_seconds: Optional[float] = None,
) -> ListReport:
    """Discover pre-upgrade tarballs under ``<world>/.alive/upgrades/``.

    Pure read-only. Filenames not matching the canonical
    ``pre-upgrade-<iso-ts>.tar.gz`` pattern are silently skipped --
    final upgrade records (``<iso-ts>.yaml``), the resume marker
    (``-resume.yaml``), runstate, and retroactive records all live in
    the same directory.

    Parameters
    ----------
    world_root_resolved : str
        Realpath'd world root.
    now_seconds : float, optional
        Override the wall-clock for ``age_seconds`` calculation
        (tests pass a deterministic value).

    Returns
    -------
    ListReport
        ``entries`` sorted by timestamp descending.
    """
    upgrades_dir = _upgrades_dir(world_root_resolved)
    # ``os.path.isdir`` swallows PermissionError, which would let a
    # perm-blocked upgrades dir collapse to "no rollback points" --
    # silently misleading the operator. Probe via ``os.stat`` so the
    # FileNotFoundError (truly absent) and PermissionError (unreadable)
    # branches diverge cleanly.
    try:
        upgrades_st = os.stat(upgrades_dir)
    except FileNotFoundError:
        return ListReport(
            upgrades_dir=upgrades_dir,
            upgrades_dir_present=False,
            entries=[],
        )
    except PermissionError as exc:
        raise RollbackError(
            "permission denied probing upgrades directory at {}: "
            "{}".format(upgrades_dir, exc),
            error_code="rollback_permission",
            exit_code=4,
        )
    except OSError as exc:
        raise RollbackError(
            "filesystem error probing upgrades directory at {}: "
            "{}".format(upgrades_dir, exc),
            error_code="rollback_permission",
            exit_code=4,
        )
    import stat as _stat  # noqa: PLC0415
    if not _stat.S_ISDIR(upgrades_st.st_mode):
        return ListReport(
            upgrades_dir=upgrades_dir,
            upgrades_dir_present=False,
            entries=[],
        )
    if now_seconds is None:
        now_seconds = time.time()
    entries: List[TarballEntry] = []
    try:
        names = os.listdir(upgrades_dir)
    except PermissionError as exc:
        # Surface as exit-4 permission error rather than collapse
        # to "no rollback points" -- a false empty result on an
        # unreadable directory misleads the operator into thinking
        # nothing is recoverable when perms are merely wrong.
        raise RollbackError(
            "permission denied reading upgrades directory at {}: "
            "{}".format(upgrades_dir, exc),
            error_code="rollback_permission",
            exit_code=4,
        )
    except OSError as exc:
        # Other OS errors (e.g. ENOTDIR after a TOCTOU race) -- still
        # safer to surface than collapse silently. Treat as a
        # permission-class failure for envelope routing; operator's
        # actionable step is investigating the filesystem state, not
        # retrying with a different timestamp.
        raise RollbackError(
            "filesystem error listing upgrades directory at {}: "
            "{}".format(upgrades_dir, exc),
            error_code="rollback_permission",
            exit_code=4,
        )
    for name in names:
        m = _PRE_UPGRADE_RE.match(name)
        if not m:
            continue
        ts = m.group(1)
        path = os.path.join(upgrades_dir, name)
        try:
            size_bytes = os.path.getsize(path)
        except PermissionError as exc:
            # An unreadable individual tarball MUST surface -- a
            # silent skip would hide the operator's only chance to
            # recover from that snapshot. Same exit-4 routing.
            raise RollbackError(
                "permission denied reading tarball at {}: {}".format(
                    path, exc,
                ),
                error_code="rollback_permission",
                exit_code=4,
            )
        except OSError:
            # File disappeared between listdir and stat (TOCTOU);
            # legitimate to skip.
            continue
        ts_seconds = _parse_iso_ts(ts)
        if ts_seconds is None:
            # Filename matched the regex but the timestamp didn't
            # parse -- treat as zero age so the entry is still
            # surfaced (the operator might want to extract it).
            age = 0
        else:
            age = max(0, int(now_seconds - ts_seconds))
        entries.append(
            TarballEntry(
                timestamp=ts,
                absolute_path=path,
                size_bytes=size_bytes,
                age_seconds=age,
            )
        )
    # Descending by timestamp string (lexicographic == chronological
    # for the ISO-8601 format).
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return ListReport(
        upgrades_dir=upgrades_dir,
        upgrades_dir_present=True,
        entries=entries,
    )


def format_list_report(report: ListReport) -> str:
    """Render a ``ListReport`` as a bordered-block table.

    Empty report (no tarballs) renders a single-line "no rollback
    points" notice. Three columns: timestamp, size, age.
    """
    lines: List[str] = []
    lines.append("╭─ \U0001f43f️  rollback points")  # ╭─ 🐿️
    if not report.upgrades_dir_present:
        lines.append("│  no upgrades directory at {}".format(
            report.upgrades_dir,
        ))
        lines.append(
            "│  (no /alive:system-upgrade has run in this world)",
        )
        lines.append("╰─")  # ╰─
        return "\n".join(lines)
    if not report.entries:
        lines.append("│  no pre-upgrade tarballs at {}".format(
            report.upgrades_dir,
        ))
        lines.append("╰─")
        return "\n".join(lines)
    # Compute column widths from the entries.
    ts_w = max(len(e.timestamp) for e in report.entries)
    size_strs = [_format_size(e.size_bytes) for e in report.entries]
    size_w = max(len(s) for s in size_strs)
    age_strs = [_format_relative_age(e.age_seconds) for e in report.entries]
    age_w = max(len(s) for s in age_strs)
    header = "  {:<{tsw}}  {:>{sw}}  {:<{aw}}".format(
        "timestamp", "size", "age",
        tsw=ts_w, sw=size_w, aw=age_w,
    )
    lines.append("│" + header)
    lines.append(
        "│" + "  " + "-" * (ts_w + size_w + age_w + 4)
    )
    for entry, size_s, age_s in zip(report.entries, size_strs, age_strs):
        line = "  {:<{tsw}}  {:>{sw}}  {:<{aw}}".format(
            entry.timestamp, size_s, age_s,
            tsw=ts_w, sw=size_w, aw=age_w,
        )
        lines.append("│" + line)
    lines.append("│")
    lines.append(
        "│  Extract one with: /alive:system-upgrade --rollback <timestamp>",
    )
    lines.append("╰─")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extract mode
# ---------------------------------------------------------------------------


def _read_manifest_from_tarball(tarball_path: str) -> List[str]:
    """Read the top-level ``MANIFEST`` text file from *tarball_path*.

    Returns the world-relative restoration roots (one per line, sorted
    as written by T5). Empty list when the tarball carries no
    ``MANIFEST`` member or the member is unreadable (corrupt-tar /
    EOF) -- callers fall back to a placeholder rendering.

    Permission errors propagate as :class:`RollbackError` with exit 4
    rather than being swallowed -- a permission failure on the
    tarball itself is something the operator MUST see, not silently
    treated as "manifest absent" (which would render a misleading
    legacy-format fallback block).

    Pure read-only. Uses ``tarfile`` directly (NOT
    ``safe_tar_extract``) because we only want one named member, not a
    full extraction.
    """
    try:
        tar = tarfile.open(tarball_path, "r:*")
    except PermissionError as exc:
        raise RollbackError(
            "permission denied opening tarball at {}: {}".format(
                tarball_path, exc,
            ),
            error_code="rollback_permission",
            exit_code=4,
        )
    except (tarfile.TarError, OSError, EOFError):
        # Corrupt or unreadable archive (non-permission). Fall back
        # to empty manifest so the renderer prints the legacy-format
        # block; the subsequent ``safe_tar_extract`` call will trip
        # the same corruption and surface a proper diagnostic.
        return []
    try:
        with tar:
            try:
                member = tar.getmember("MANIFEST")
            except KeyError:
                return []
            try:
                f = tar.extractfile(member)
            except PermissionError as exc:
                raise RollbackError(
                    "permission denied reading MANIFEST member of "
                    "{}: {}".format(tarball_path, exc),
                    error_code="rollback_permission",
                    exit_code=4,
                )
            if f is None:
                return []
            try:
                raw = f.read()
            finally:
                f.close()
    except (tarfile.TarError, OSError, EOFError):
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    out: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s:
            out.append(s)
    return out


def extract_tarball(
    world_root_resolved: str,
    iso_ts: str,
) -> ExtractReport:
    """Extract the named pre-upgrade tarball into the canonical rollback dir.

    Parameters
    ----------
    world_root_resolved : str
        Realpath'd world root.
    iso_ts : str
        Filename-safe ISO-8601 timestamp identifying the tarball.

    Raises
    ------
    RollbackError
        On any of: invalid timestamp shape, no upgrades dir, no
        matching tarball, destination already exists, or extraction
        failure (LD22 refusal / corrupt tar).

    Returns
    -------
    ExtractReport
        ``manifest_entries`` populated when the tarball had a
        ``MANIFEST`` file; empty when absent.
    """
    if not _ISO_TS_RE.match(iso_ts):
        raise RollbackError(
            "rollback timestamp {!r} is not a canonical ISO-8601 "
            "filename-safe value (expected YYYY-MM-DDTHH-MM-SS)".format(
                iso_ts,
            ),
            error_code="rollback_invalid_timestamp",
            exit_code=1,
        )
    upgrades_dir = _upgrades_dir(world_root_resolved)
    # ``os.path.isdir`` swallows PermissionError, so a perm-blocked
    # upgrades dir would route here as "no upgrades dir" (exit 3) and
    # mask the real failure. Probe via ``os.stat`` to distinguish the
    # truly-absent case (FileNotFoundError) from the unreadable case
    # (PermissionError).
    try:
        upgrades_st = os.stat(upgrades_dir)
    except FileNotFoundError:
        raise RollbackError(
            "no upgrades directory at {} -- /alive:system-upgrade has "
            "never run in this world, so there is nothing to roll "
            "back".format(upgrades_dir),
            error_code="rollback_no_upgrades_dir",
            exit_code=3,
        )
    except PermissionError as exc:
        raise RollbackError(
            "permission denied probing upgrades directory at {}: "
            "{}".format(upgrades_dir, exc),
            error_code="rollback_permission",
            exit_code=4,
        )
    except OSError as exc:
        raise RollbackError(
            "filesystem error probing upgrades directory at {}: "
            "{}".format(upgrades_dir, exc),
            error_code="rollback_permission",
            exit_code=4,
        )
    import stat as _stat  # noqa: PLC0415
    if not _stat.S_ISDIR(upgrades_st.st_mode):
        raise RollbackError(
            "{} exists but is not a directory; expected the upgrades "
            "tarball collection".format(upgrades_dir),
            error_code="rollback_no_upgrades_dir",
            exit_code=3,
        )
    tarball_basename = "pre-upgrade-{}.tar.gz".format(iso_ts)
    tarball_path = os.path.join(upgrades_dir, tarball_basename)
    # ``os.path.isfile`` swallows PermissionError silently and returns
    # False -- which would route an unreadable upgrades dir into the
    # "missing tarball" branch and misdiagnose a permission failure
    # as ``rollback_target_not_found`` (exit 3). Use ``os.stat`` so
    # we can distinguish FileNotFoundError (truly missing) from
    # PermissionError (perms wrong on the dir or the tarball itself).
    tarball_exists: bool
    try:
        st = os.stat(tarball_path)
        tarball_exists = bool(st)
    except FileNotFoundError:
        tarball_exists = False
    except PermissionError as exc:
        raise RollbackError(
            "permission denied probing tarball at {}: {}".format(
                tarball_path, exc,
            ),
            error_code="rollback_permission",
            exit_code=4,
        )
    except OSError as exc:
        raise RollbackError(
            "filesystem error probing tarball at {}: {}".format(
                tarball_path, exc,
            ),
            error_code="rollback_permission",
            exit_code=4,
        )
    if not tarball_exists:
        # Render "available timestamps" hint by listing what's there.
        # If listing trips a permission failure, surface that via
        # exit 4 rather than mask it with a target-not-found refusal
        # -- a perm error on the upgrades dir is the operator's
        # actionable signal (chmod / chown), not a "wrong timestamp"
        # cue. ``list_tarballs`` distinguishes the two cleanly.
        report = list_tarballs(world_root_resolved)
        available = ", ".join(
            e.timestamp for e in report.entries[:5]
        )
        suffix = (
            " (available: {})".format(available)
            if available else
            " (no other tarballs available either)"
        )
        raise RollbackError(
            "no pre-upgrade tarball at {}{}".format(
                tarball_path, suffix,
            ),
            error_code="rollback_target_not_found",
            exit_code=3,
        )
    extract_dir = _rollback_dir(world_root_resolved, iso_ts)
    if os.path.exists(extract_dir):
        raise RollbackError(
            "rollback destination {} already exists -- remove it (or "
            "pick a different timestamp) before re-extracting".format(
                extract_dir,
            ),
            error_code="rollback_target_already_extracted",
            exit_code=1,
        )
    # Read the manifest BEFORE extraction so a corrupt tarball surfaces
    # an empty manifest (and the LD22 guard refusal below is the only
    # failure mode the caller has to handle).
    manifest_entries = _read_manifest_from_tarball(tarball_path)
    # Extract via the LD22-conformant helper. ``safe_tar_extract``
    # refuses path-traversal members at pre-validation (no writes on
    # rejection) so a malicious tarball cannot escape ``extract_dir``.
    try:
        from _alive_common.tarball import safe_tar_extract  # noqa: PLC0415
    except ImportError:  # pragma: no cover - defensive
        raise RollbackError(
            "cannot import _alive_common.tarball -- plugin install is "
            "incomplete",
            error_code="rollback_extract_failed",
            exit_code=1,
        )
    # ``safe_tar_extract`` creates the output dir if absent; we let it
    # do so. Any rejection (LD22 guard, OSError, corrupt tar) is
    # surfaced through ``RollbackError``.
    try:
        os.makedirs(extract_dir, exist_ok=False)
    except FileExistsError:
        # Race against the existence check above; surface as
        # already-extracted rather than letting safe_tar_extract trip
        # on a half-created dir.
        raise RollbackError(
            "rollback destination {} appeared between check and "
            "create -- treat as already-extracted".format(extract_dir),
            error_code="rollback_target_already_extracted",
            exit_code=1,
        )
    except PermissionError as exc:
        # Cannot create the destination -- exit-4 envelope so the
        # operator's actionable next step is fixing perms (chmod /
        # chown), not retrying with a different timestamp.
        raise RollbackError(
            "permission denied creating rollback destination {}: "
            "{}".format(extract_dir, exc),
            error_code="rollback_permission",
            exit_code=4,
        )
    except OSError as exc:
        raise RollbackError(
            "could not create rollback destination {}: {}".format(
                extract_dir, exc,
            ),
            error_code="rollback_extract_failed",
            exit_code=1,
        )
    try:
        safe_tar_extract(tarball_path, extract_dir)
    except PermissionError as exc:
        # Permission failure during extraction (e.g. read on the
        # tarball, write into staging or destination). Cleanup the
        # destination then surface as exit-4.
        import shutil  # noqa: PLC0415
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise RollbackError(
            "permission denied during extraction of {}: {}".format(
                tarball_path, exc,
            ),
            error_code="rollback_permission",
            exit_code=4,
        )
    except (ValueError, OSError, FileNotFoundError) as exc:
        # Best-effort cleanup of the (likely empty or partial) extract
        # dir so a subsequent retry doesn't trip the
        # already-extracted guard. ``safe_tar_extract`` itself stages
        # through an inner temp dir; on rejection the staging is
        # cleaned but ``extract_dir`` may still exist as the empty
        # destination.
        import shutil  # noqa: PLC0415
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise RollbackError(
            "extraction refused / failed for {}: {}".format(
                tarball_path, exc,
            ),
            error_code="rollback_extract_failed",
            exit_code=1,
        )
    return ExtractReport(
        tarball_path=tarball_path,
        extract_dir=extract_dir,
        manifest_entries=manifest_entries,
    )


# ---------------------------------------------------------------------------
# Restore-procedure renderer
# ---------------------------------------------------------------------------


def _split_manifest_entries(
    manifest_entries: List[str],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Split manifest entries into ``(world_state, walnut_kernels)``.

    World-state entries are paths under ``.alive/`` (NOT counting any
    per-walnut ``_kernel/`` paths). Walnut-kernel entries are
    ``(walnut_path, kernel_relpath)`` tuples for paths whose final
    segment chain ends in ``_kernel`` or contains ``_kernel/`` as a
    component.

    Robust to unsorted / mixed input. Output is sorted within each
    bucket for determinism.
    """
    world_state: List[str] = []
    walnut_kernels: List[Tuple[str, str]] = []
    for raw in manifest_entries:
        rel = raw.strip()
        if not rel:
            continue
        # Normalise separators -- manifest is written with forward
        # slashes by T5 but be defensive.
        rel = rel.replace(os.sep, "/").lstrip("./").rstrip("/")
        if rel.startswith(".alive") and "/_kernel" not in rel:
            world_state.append(rel)
            continue
        # Walnut kernel detection: look for ``/_kernel`` segment.
        if "/_kernel" in rel:
            # Find the ``_kernel`` segment.
            idx = rel.find("/_kernel")
            walnut = rel[:idx]
            kernel_rel = rel[idx + 1:]  # strip leading slash
            walnut_kernels.append((walnut, kernel_rel))
        elif rel.endswith("_kernel"):
            # Edge case: walnut path itself ends in ``_kernel``
            # (unusual but possible). Treat as walnut + kernel rel.
            walnut = rel[:-len("_kernel")].rstrip("/")
            if walnut:
                walnut_kernels.append((walnut, "_kernel"))
        else:
            # Other content (e.g. raw walnut content captured by T5).
            # Bucket as world-state for the simpler case so the
            # operator at least sees it; T11 is read-side only and
            # cannot "guess" intent for non-conforming entries.
            world_state.append(rel)
    world_state = sorted(set(world_state))
    walnut_kernels = sorted(set(walnut_kernels), key=lambda x: x[0])
    return world_state, walnut_kernels


def format_restore_procedure(
    report: ExtractReport,
    world_root: str,
    iso_ts: str,
) -> str:
    """Render the manual restore-procedure block for a successful extract.

    The procedure stages payload OUT of ``.alive/`` to a world-root
    sibling BEFORE moving current ``.alive/`` aside (otherwise step 2
    would lose the rollback payload because step 1's extraction lives
    UNDER ``.alive/``). EXACT paths from the tarball's MANIFEST are
    rendered -- both ``.alive/`` and per-walnut ``_kernel/`` --
    making the restore mechanical, not guesswork.

    Renderer also handles the empty-manifest fallback: when a tarball
    carries no MANIFEST (legacy backups or corrupt manifest), the
    block prints a placeholder ``<world>`` line so the operator at
    least sees the stage-out + swap pattern.

    Output is bordered-block formatted (matches the squirrel visual
    convention).
    """
    world_state, walnut_kernels = _split_manifest_entries(
        report.manifest_entries,
    )
    has_manifest = bool(report.manifest_entries)

    payload_sibling = os.path.join(
        world_root, ".alive-rollback-payload-{}".format(iso_ts),
    )
    extract_dir = report.extract_dir

    lines: List[str] = []
    lines.append("╭─ \U0001f43f️  rollback ready")  # ╭─ 🐿️
    lines.append("│  Extracted to: {}".format(extract_dir))
    lines.append("│")
    lines.append("│  Tarball contents (from manifest):")
    if not has_manifest:
        lines.append(
            "│    - <manifest absent; tarball is legacy-format>",
        )
        lines.append("│    - .alive/  (world-level state, presumed)")
    else:
        for entry in world_state:
            lines.append("│    - {}/  (world-level state)".format(entry))
        for walnut, kernel_rel in walnut_kernels:
            lines.append(
                "│    - {}/{}  (per-walnut state)".format(
                    walnut, kernel_rel,
                ),
            )
    lines.append("│")
    lines.append("│  To restore:")
    # Every shell-command path is shlex.quote'd so a world root or
    # walnut path containing spaces / shell metacharacters renders a
    # valid mv / rm invocation. Without this, ``/tmp/My World``-style
    # paths produce syntactically wrong commands that operate on the
    # wrong arguments -- breaking the mechanical-restore contract on
    # any path that isn't pristine [a-zA-Z0-9_/.-].
    qq = shlex.quote
    # Step 1: stage payload out of .alive/ (sibling at world root).
    lines.append(
        "│    1. Stage rollback payload OUT of current .alive/ "
        "(sibling at world root):",
    )
    lines.append(
        "│       mv {} {}".format(qq(extract_dir), qq(payload_sibling)),
    )
    lines.append(
        "│       (this preserves the payload before we touch "
        "current .alive/)",
    )
    # Step 2: move current state aside.
    lines.append("│    2. Move current state aside:")
    current_alive = os.path.join(world_root, ".alive")
    archived_alive = os.path.join(
        world_root, ".alive.post-upgrade-{}".format(iso_ts),
    )
    lines.append(
        "│       mv {} {}".format(qq(current_alive), qq(archived_alive)),
    )
    if walnut_kernels:
        for walnut, _ in walnut_kernels:
            walnut_kernel = os.path.join(world_root, walnut, "_kernel")
            archived_kernel = os.path.join(
                world_root, walnut,
                "_kernel.post-upgrade-{}".format(iso_ts),
            )
            lines.append(
                "│       mv {} {}".format(
                    qq(walnut_kernel), qq(archived_kernel),
                ),
            )
        lines.append(
            "│       (one per walnut listed above)",
        )
    else:
        lines.append(
            "│       (no per-walnut _kernel/ paths in manifest)",
        )
    # Step 3: move staged payload into place.
    lines.append("│    3. Move staged payload into place:")
    if has_manifest:
        # ``.alive/`` rehome -- pick the manifest's ``.alive/`` entry,
        # falling back to the literal ``.alive`` if the manifest
        # buckets it differently.
        alive_payload = os.path.join(payload_sibling, ".alive")
        lines.append(
            "│       mv {} {}".format(
                qq(alive_payload), qq(current_alive),
            ),
        )
        for walnut, kernel_rel in walnut_kernels:
            payload_kernel = os.path.join(
                payload_sibling, walnut, "_kernel",
            )
            target_kernel = os.path.join(world_root, walnut, "_kernel")
            lines.append(
                "│       mv {} {}".format(
                    qq(payload_kernel), qq(target_kernel),
                ),
            )
        if walnut_kernels:
            lines.append("│       (repeat per walnut)")
    else:
        # Legacy fallback: just rehome ``.alive/``.
        alive_payload = os.path.join(payload_sibling, ".alive")
        lines.append(
            "│       mv {} {}".format(
                qq(alive_payload), qq(current_alive),
            ),
        )
    # Step 4: verify world health.
    lines.append("│    4. Verify world health: alive doctor")
    # Step 5: clean up.
    lines.append(
        "│    5. Clean up: rm -rf {}".format(qq(payload_sibling)),
    )
    lines.append(
        "│       (the .alive.post-upgrade-{} dirs can be archived "
        "or removed at your discretion)".format(iso_ts),
    )
    lines.append("│")
    lines.append(
        "│  Note: full automated swap is planned for v3.3.",
    )
    lines.append("╰─")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Post-upgrade summary helper (consumed by orchestrator phase 12)
# ---------------------------------------------------------------------------


def build_post_upgrade_pointer(tarball_path: str) -> str:
    """One-line pointer printed in the post-upgrade summary.

    Per acceptance criterion 5 of T11: the skill output post-upgrade
    must include a one-line pointer to rollback availability. This
    helper renders that line; the orchestrator's phase 12 record /
    summary calls it with the path of the tarball T5 just wrote.

    The path is normalised to a world-relative form when it sits under
    ``.alive/upgrades/`` (the canonical T5 location); arbitrary other
    paths are surfaced as-is.
    """
    basename = os.path.basename(tarball_path)
    m = _PRE_UPGRADE_RE.match(basename)
    if m is None:
        # Caller passed a non-canonical path -- surface verbatim and
        # let the operator inspect.
        return (
            "Rollback tarball: {}; run "
            "`/alive:system-upgrade --rollback <ts>` to extract."
        ).format(tarball_path)
    ts = m.group(1)
    return (
        "Rollback tarball: .alive/upgrades/{}; run "
        "`/alive:system-upgrade --rollback {}` to extract."
    ).format(basename, ts)
