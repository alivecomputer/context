"""Phase-6 atomic pre-upgrade backup (T5 of fn-18).

Produces ``<world>/.alive/upgrades/pre-upgrade-<iso-ts>.tar.gz`` via a
selection-staging-then-tar pipeline:

1. Build ``selected_paths`` from the cleanup + migrate plans -- every
   path either of those phases will mutate, plus per-walnut ``_kernel/``
   paths and world-root walnut content. Dropped from the selection:

       * ``<world>/.alive/upgrades/`` (would recurse into prior
         tarballs, the new ``.tmp`` partial tarball, the staging dir);
       * ``<world>/.alive/.system-upgrade.lock`` and its meta sidecar;
       * ``<world>/.alive/.rollback-*/`` directories (operator
         rollback artifacts);
       * the staging directory itself (``.alive/upgrades/.staging-<ts>/``);
       * the partial ``.tmp`` tarball being written.

   Drops are recorded in the report's ``skipped_self_inclusion`` list
   so the operator can audit.

2. Hardlink-stage the selection into
   ``<world>/.alive/upgrades/.staging-<iso-ts>/``. Hardlinks where the
   source filesystem permits; copy fallback otherwise.

3. Disk-full canary: write a same-size scratch file BEFORE invoking
   ``safe_tar_create``; ENOSPC there fails fast with a clean abort
   (no partial tarball, no staging leftover).

4. Invoke ``_alive_common.tarball.safe_tar_create(staging_dir,
   tmp_path)``. ``safe_tar_create`` is NOT atomic on its own (per the
   pre-existing ``alive-p2p.py`` implementation it writes directly to
   the path passed in). T5 wraps it with the atomic protocol:
   ``fsync(fd)``, ``fsync(parent_dir)``, ``os.replace(tmp,
   final)``, ``fsync(parent_dir)`` again. Result: on a crash between
   tar-create and rename, no ``pre-upgrade-<ts>.tar.gz`` exists at the
   final path; only the ``.tmp`` may exist (and is cleaned by sweep).

5. After successful create-and-replace, the staging directory is
   removed via ``shutil.rmtree`` (best-effort -- the tarball is
   already durable; staging cleanup failure is recorded but does not
   roll back the backup).

The tarball includes a top-level ``MANIFEST`` text file listing the
restoration roots (one line per ``selected_paths`` entry as world-
relative path). T11's rollback procedure reads this manifest to
generate exact restore commands.

Stdlib-only (R10).
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple


# Tarball helpers live under ``_alive_common``; resolve via the
# scripts/-on-sys.path import the rest of the package uses.
from _alive_common.tarball import safe_tar_create


__all__ = (
    "BackupReport",
    "build_backup_selection",
    "create_backup",
    "DEFAULT_CANARY_BYTES",
    "MIN_CANARY_BYTES",
    "estimate_uncompressed_selection_size",
)


#: Lower bound on the canary write. Picked so the probe always
#: forces real block allocation on every modern filesystem (1 MiB
#: comfortably exceeds typical block sizes of 4 KiB-128 KiB).
#: ``create_backup`` uses ``max(estimated_size, MIN_CANARY_BYTES)``
#: when the caller does not pass an explicit ``canary_bytes``; the
#: explicit override path still respects ``canary_bytes=0`` for
#: tests that don't care about disk-full coverage.
MIN_CANARY_BYTES: int = 1 * 1024 * 1024


#: Historic default. Retained as a public constant for backward
#: compat with any caller that imports it directly; new code should
#: rely on ``MIN_CANARY_BYTES`` + ``estimate_uncompressed_selection_size``
#: which together provide a size-aware probe.
DEFAULT_CANARY_BYTES: int = MIN_CANARY_BYTES


def estimate_uncompressed_selection_size(
    selected_paths: Iterable[str],
) -> int:
    """Return the sum of file sizes inside *selected_paths*.

    Walks each selected path. Files contribute ``stat.st_size``;
    directories recurse via ``os.walk`` (symlinks NOT followed --
    ``followlinks=False`` is the default, but we also check
    ``os.path.islink`` per-entry so a dangling symlink is silently
    skipped instead of inflating the estimate). Missing paths
    contribute zero.

    Pure read-only. Used by :func:`create_backup` to size the disk-
    full canary so a near-full filesystem cannot pass the probe and
    then ENOSPC inside ``safe_tar_create``.
    """
    total = 0
    for raw in selected_paths:
        if not raw:
            continue
        try:
            if os.path.islink(raw):
                # Don't follow symlinks for sizing -- the staging
                # pass will not chase them either (it hardlinks the
                # symlink itself).
                continue
            if os.path.isfile(raw):
                try:
                    total += os.path.getsize(raw)
                except OSError:
                    continue
                continue
            if not os.path.isdir(raw):
                continue
            for root, dirs, files in os.walk(raw, followlinks=False):
                for name in files:
                    full = os.path.join(root, name)
                    if os.path.islink(full):
                        continue
                    try:
                        total += os.path.getsize(full)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


@dataclass
class BackupReport:
    """Outcome of :func:`create_backup`.

    Attributes
    ----------
    final_path : str | None
        Absolute path of the durable
        ``pre-upgrade-<iso-ts>.tar.gz``; ``None`` when the backup did
        not complete (canary aborted, tar failed, etc.).
    manifest_entries : list[str]
        World-relative restoration roots written into the tarball's
        top-level ``MANIFEST`` text file. Returned for T11 / verify
        consumption.
    selected_paths : list[str]
        Absolute paths the backup pulled into staging.
    skipped_self_inclusion : list[str]
        Paths dropped from the selection because they fall under one
        of the hard-exclusion roots (own ``.alive/upgrades/``, the
        lock, ``.rollback-*/``, the staging dir, the ``.tmp``
        partial).
    canary_aborted : bool
        ``True`` when the disk-full canary refused before any
        destructive op. The orchestrator surfaces this as a clean
        pre-flight refusal.
    error : str | None
        Free-text error message; ``None`` on success.
    """

    final_path: Optional[str] = None
    manifest_entries: List[str] = field(default_factory=list)
    selected_paths: List[str] = field(default_factory=list)
    skipped_self_inclusion: List[str] = field(default_factory=list)
    canary_aborted: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Selection construction
# ---------------------------------------------------------------------------

def _hard_exclusion_roots(
    world_root_resolved: str, iso_ts: str,
) -> Tuple[str, ...]:
    """Realpath'd absolute roots that MUST NOT enter the backup tar."""
    upgrades_dir = os.path.join(world_root_resolved, ".alive", "upgrades")
    return (
        upgrades_dir,
        os.path.join(world_root_resolved, ".alive", ".system-upgrade.lock"),
        os.path.join(
            world_root_resolved, ".alive", ".system-upgrade.lock-meta.json",
        ),
    )


def _is_under(path: str, root: str) -> bool:
    """``True`` if *path* is *root* or strictly under *root*."""
    if path == root:
        return True
    return path.startswith(root.rstrip(os.sep) + os.sep)


def _is_excluded(
    abs_path: str,
    *,
    world_root_resolved: str,
    iso_ts: str,
) -> bool:
    """Return True if *abs_path* falls under any hard-exclusion root.

    Drives the selection-time filtering. ``.alive/upgrades/`` is the
    blanket exclusion (covers prior tarballs, the new staging dir, the
    new ``.tmp`` partial); the lock + lock-meta are individually
    enumerated; ``.alive/.rollback-*/`` directories are matched by
    prefix because the timestamp suffix is variable.
    """
    # Normalise to realpath so a symlinked selection cannot bypass the
    # exclusion check.
    try:
        resolved = os.path.realpath(abs_path)
    except OSError:
        return True  # un-resolvable path -- safer to drop than include
    for root in _hard_exclusion_roots(world_root_resolved, iso_ts):
        if _is_under(resolved, root):
            return True
    # Rollback dirs share the prefix ``<world>/.alive/.rollback-``.
    rollback_prefix = os.path.join(
        world_root_resolved, ".alive", ".rollback-",
    )
    if resolved.startswith(rollback_prefix):
        return True
    return False


def build_backup_selection(
    world_root_resolved: str,
    iso_ts: str,
    *,
    cleanup_targets: Iterable[str] = (),
    migrate_targets: Iterable[str] = (),
    extra_targets: Iterable[str] = (),
) -> Tuple[List[str], List[str]]:
    """Return ``(selected, skipped_self_inclusion)``.

    *cleanup_targets* and *migrate_targets* are absolute paths that
    phase 8 / phase 9 will mutate. *extra_targets* lets the
    orchestrator add per-walnut ``_kernel/`` paths and world-root
    walnut content explicitly.

    De-duplicated, sorted, and filtered against the hard-exclusion
    roots. Paths that don't currently exist on disk are dropped
    silently (we don't want a stale plan polluting the manifest).

    **Ancestor/descendant collapse**: when the
    caller passes both a directory AND a file inside it (e.g.
    ``<walnut>/_kernel/`` plus ``<walnut>/_kernel/now.md``), the
    descendant is dropped from the selection. Without this, staging
    walks the directory tree once (placing every child) and then
    re-stages the explicit child path, hitting ``EEXIST`` in
    ``_hardlink_or_copy`` and failing the entire backup. The
    collapse is segment-aware (``+ os.sep``) so siblings sharing a
    name prefix do not accidentally collapse.
    """
    # Normalise the world root once so realpath comparisons below
    # always operate on canonical paths. macOS-style aliases
    # (``/var`` -> ``/private/var``) and other symlinked roots resolve
    # consistently here so the eventual ``relpath`` computation in
    # the staging pass cannot produce ``..``-prefixed escapes.
    try:
        world_root_canonical = os.path.realpath(world_root_resolved)
    except OSError:
        world_root_canonical = world_root_resolved
    world_prefix = world_root_canonical.rstrip(os.sep) + os.sep

    seen: set = set()
    candidates: List[str] = []
    skipped: List[str] = []
    for group in (cleanup_targets, migrate_targets, extra_targets):
        for raw in group:
            if not raw:
                continue
            # ``realpath`` collapses ``/var`` <-> ``/private/var`` so
            # the path uses the same alias as ``world_root_canonical``.
            try:
                abs_path = os.path.realpath(raw)
            except OSError:
                abs_path = os.path.abspath(raw)
            if abs_path in seen:
                continue
            seen.add(abs_path)
            if not os.path.exists(abs_path) and not os.path.islink(abs_path):
                # Nothing to back up -- skip silently; this is normal
                # for migrate plans that name yet-unmaterialised
                # outputs.
                continue
            # Containment guard: any path that does not sit under the
            # canonical world root cannot be staged without escaping
            # the staging dir later. Drop it into skipped (this is
            # the same bucket as the hard-exclusion roots; the
            # operator can audit the report).
            if abs_path != world_root_canonical and not abs_path.startswith(
                world_prefix,
            ):
                skipped.append(abs_path)
                continue
            if _is_excluded(
                abs_path,
                world_root_resolved=world_root_canonical,
                iso_ts=iso_ts,
            ):
                skipped.append(abs_path)
                continue
            candidates.append(abs_path)
    # Ancestor/descendant collapse. Sort by path length (shortest
    # first) so a parent directory is processed before its children;
    # then for each candidate, check whether any already-kept entry
    # is a path-segment-aware ancestor.
    candidates.sort(key=lambda p: (p.count(os.sep), p))
    kept: List[str] = []
    for path in candidates:
        is_descendant = False
        for ancestor in kept:
            ancestor_prefix = ancestor.rstrip(os.sep) + os.sep
            if path == ancestor or path.startswith(ancestor_prefix):
                # Only collapse when the ancestor is a real directory
                # (or symlink-to-dir) -- a file ancestor is impossible
                # but the type-check is cheap insurance against
                # caller-supplied weirdness.
                if os.path.isdir(ancestor):
                    is_descendant = True
                    break
        if is_descendant:
            continue
        kept.append(path)
    kept.sort()
    skipped.sort()
    return kept, skipped


# ---------------------------------------------------------------------------
# Hardlink-stage helpers
# ---------------------------------------------------------------------------

def _hardlink_or_copy(src: str, dst: str) -> None:
    """Hardlink *src* to *dst*; fall back to copy on EXDEV / EPERM.

    Hardlinks are filesystem-local: cross-filesystem (``EXDEV``) and
    permission-restricted (``EPERM``) cases trip the copy fallback.
    Symlinks in *src* are turned into hardlinks of the symlink itself
    where possible (preserving the alias), and copied with
    ``follow_symlinks=False`` otherwise.
    """
    parent = os.path.dirname(dst)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    # Idempotency guard: a destination already at *dst* is fine. The
    # ancestor/descendant collapse in ``build_backup_selection`` should
    # prevent this, but defense-in-depth keeps the staging pass robust
    # against caller-supplied raw selections (e.g., direct callers that
    # bypass ``build_backup_selection``).
    if os.path.exists(dst) or os.path.islink(dst):
        return
    try:
        # ``follow_symlinks=False`` lets us hardlink the symlink itself.
        os.link(src, dst, follow_symlinks=False)
        return
    except (OSError, NotImplementedError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.EEXIST:
            # Race: another stage call placed the file between our
            # exists-check and the link call. Treat as success.
            return
        # EXDEV: cross-device. EPERM: filesystem disallows linking. EINVAL on
        # some platforms when linking symlinks. Fall back to copy.
        if isinstance(exc, OSError) and exc.errno not in (
            errno.EXDEV, errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP,
        ):
            # Some other error -- bubble up so the caller can decide.
            raise
    # Copy fallback. ``copy2`` preserves metadata; ``follow_symlinks=False``
    # preserves symlink character.
    if os.path.islink(src):
        link_target = os.readlink(src)
        try:
            os.symlink(link_target, dst)
        except OSError:
            shutil.copy2(src, dst, follow_symlinks=False)
    else:
        shutil.copy2(src, dst, follow_symlinks=True)


def _stage_path(src: str, staging_dir: str, world_root_resolved: str) -> None:
    """Hardlink-stage *src* into *staging_dir*, preserving relative layout.

    Defense-in-depth refusal: if ``relpath(src, world_root_resolved)``
    starts with ``..`` (or equals ``..``), staging would write OUTSIDE
    the staging dir. ``build_backup_selection`` already filters those
    cases via the realpath-canonical containment check, but a direct
    caller passing raw paths still needs the guard. Refusing here
    surfaces the bug as ``ValueError`` rather than silently writing
    to a location the backup tarball cannot describe correctly.
    """
    rel = os.path.relpath(src, world_root_resolved)
    if rel == ".." or rel.startswith(".." + os.sep):
        raise ValueError(
            "stage path escapes world root: src={!r} world_root={!r}".format(
                src, world_root_resolved,
            )
        )
    dst = os.path.join(staging_dir, rel)
    if os.path.isdir(src) and not os.path.islink(src):
        for root, dirs, files in os.walk(src):
            rel_root = os.path.relpath(root, world_root_resolved)
            staged_root = os.path.join(staging_dir, rel_root)
            os.makedirs(staged_root, exist_ok=True)
            # Drop directory symlinks: hardlink the link itself in-place.
            for d in list(dirs):
                d_src = os.path.join(root, d)
                if os.path.islink(d_src):
                    d_rel = os.path.relpath(d_src, world_root_resolved)
                    d_dst = os.path.join(staging_dir, d_rel)
                    parent_d = os.path.dirname(d_dst)
                    if parent_d and not os.path.isdir(parent_d):
                        os.makedirs(parent_d, exist_ok=True)
                    try:
                        os.symlink(os.readlink(d_src), d_dst)
                    except OSError:
                        # Skip -- safe_tar_create rejects symlink-escapes
                        # at create time; if the symlink stays inside
                        # source_dir it gets included as a regular dir.
                        pass
                    dirs.remove(d)
            for fname in files:
                f_src = os.path.join(root, fname)
                f_rel = os.path.relpath(f_src, world_root_resolved)
                f_dst = os.path.join(staging_dir, f_rel)
                _hardlink_or_copy(f_src, f_dst)
    else:
        _hardlink_or_copy(src, dst)


# ---------------------------------------------------------------------------
# Disk-full canary
# ---------------------------------------------------------------------------

#: Block size used by the canary's chunked-write loop. Picked to be
#: large enough to amortise per-call overhead and small enough that
#: an ENOSPC partway through the probe surfaces quickly.
_CANARY_CHUNK_BYTES: int = 1 * 1024 * 1024  # 1 MiB


def _disk_full_canary(staging_parent: str, size_bytes: int) -> bool:
    """Try to allocate *size_bytes* of real disk space; return True on success.

    The probe writes *real bytes* (NOT a sparse seek-and-poke) so the
    filesystem actually allocates the blocks. A naive
    ``f.seek(size - 1); f.write(b"\\x00")`` produces a sparse file on
    every modern filesystem (HFS+/APFS/ext4/btrfs/xfs/zfs), allocating
    a single block regardless of *size_bytes* -- which defeats the
    canary entirely on a near-full disk: the probe passes, then
    ``safe_tar_create`` runs out of space mid-write and corrupts the
    upgrade flow.

    Implementation: write zero-filled chunks of ``_CANARY_CHUNK_BYTES``
    until *size_bytes* are committed, fsync once at the end. ENOSPC /
    EDQUOT mid-write yield ``False``; other ``OSError``s propagate so
    misconfiguration (missing dir, EACCES) surfaces as a real
    exception.

    The probe file is removed in ``finally`` regardless of outcome.
    """
    if size_bytes <= 0:
        return True
    os.makedirs(staging_parent, exist_ok=True)
    probe_path = os.path.join(staging_parent, ".disk-full-probe.bin")
    chunk = b"\x00" * _CANARY_CHUNK_BYTES
    try:
        with open(probe_path, "wb") as f:
            written = 0
            while written < size_bytes:
                remaining = size_bytes - written
                buf = chunk if remaining >= len(chunk) else chunk[:remaining]
                try:
                    f.write(buf)
                except OSError as exc:
                    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                        return False
                    raise
                written += len(buf)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError as exc:
                if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    return False
                raise
    finally:
        if os.path.exists(probe_path):
            try:
                os.unlink(probe_path)
            except OSError:
                pass
    return True


# ---------------------------------------------------------------------------
# Atomic post-create rename
# ---------------------------------------------------------------------------

def _fsync_path(path: str) -> None:
    """Best-effort fsync of *path* (file or directory)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_replace_tarball(tmp_path: str, final_path: str) -> None:
    """Rename *tmp_path* to *final_path* with surrounding fsyncs.

    Preconditions: *tmp_path* exists and is a complete tar.gz; the
    parent directory exists. Postconditions: ``final_path`` exists; the
    parent directory entry is durable on stable storage (best-effort).
    """
    parent = os.path.dirname(final_path) or "."
    _fsync_path(tmp_path)  # fsync the tar contents themselves
    os.replace(tmp_path, final_path)
    _fsync_path(parent)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def create_backup(
    world_root_resolved: str,
    iso_ts: str,
    *,
    selected_paths: List[str],
    skipped_self_inclusion: Optional[List[str]] = None,
    canary_bytes: Optional[int] = None,
) -> BackupReport:
    """Stage *selected_paths* and write the atomic pre-upgrade tarball.

    Parameters
    ----------
    world_root_resolved : str
        Realpath'd world root. The tarball lands at
        ``<world_root_resolved>/.alive/upgrades/pre-upgrade-<iso_ts>.tar.gz``.
    iso_ts : str
        Filename-safe ISO timestamp (e.g. ``2026-05-04T12-34-56``).
        The orchestrator owns generation; this module never derives a
        timestamp from ``time.time()`` directly.
    selected_paths : list[str]
        Absolute paths to back up. Built via
        :func:`build_backup_selection` -- the caller is responsible for
        running the selection through that helper so hard-exclusion
        roots (and skipped_self_inclusion) are applied consistently.
    skipped_self_inclusion : list[str], optional
        For reporting only; mirrored into the returned ``BackupReport``.
    canary_bytes : int, optional
        Disk-full canary size in bytes. When ``None`` (the default),
        the size is computed from *selected_paths* via
        :func:`estimate_uncompressed_selection_size` and clamped to
        a minimum of :data:`MIN_CANARY_BYTES`. Pass an explicit
        integer to override (tests pass ``0`` to skip the probe
        entirely; large fixture jobs may pass a tighter bound).

    Returns
    -------
    BackupReport
        ``final_path`` populated on success; ``error`` populated on
        failure. The function NEVER raises -- failures are reported
        in-band so the orchestrator can choose to refuse cleanly.
    """
    # Canonicalise the world root once. ``build_backup_selection`` did
    # the same; if a direct caller bypassed that helper, normalising
    # here guarantees the staging-pass ``relpath`` calls share a prefix
    # with the realpath'd selection.
    try:
        world_root_resolved = os.path.realpath(world_root_resolved)
    except OSError:
        pass
    upgrades_dir = os.path.join(world_root_resolved, ".alive", "upgrades")
    os.makedirs(upgrades_dir, exist_ok=True)

    final_basename = "pre-upgrade-{}.tar.gz".format(iso_ts)
    final_path = os.path.join(upgrades_dir, final_basename)
    tmp_basename = ".pre-upgrade-{}.tar.gz.tmp".format(iso_ts)
    tmp_path = os.path.join(upgrades_dir, tmp_basename)
    staging_dir = os.path.join(upgrades_dir, ".staging-{}".format(iso_ts))

    report = BackupReport(
        selected_paths=list(selected_paths),
        skipped_self_inclusion=list(skipped_self_inclusion or []),
    )

    # Resolve canary size: caller override wins when set; otherwise
    # estimate from the live selection and clamp to MIN_CANARY_BYTES.
    if canary_bytes is None:
        estimated = estimate_uncompressed_selection_size(selected_paths)
        effective_canary_bytes = max(estimated, MIN_CANARY_BYTES)
    else:
        effective_canary_bytes = canary_bytes

    # Disk-full canary BEFORE any staging -- abort cleanly if the
    # filesystem is too full to even host the tarball later.
    if not _disk_full_canary(upgrades_dir, effective_canary_bytes):
        report.canary_aborted = True
        report.error = "disk-full canary refused"
        return report

    # Stage each selected path. Hardlink-first; copy fallback. The
    # staging dir itself is the tar source -- the tarball does NOT
    # encode the staging-dir's basename in member paths because we
    # walk the staging dir (members are relative to it).
    if os.path.exists(staging_dir):
        # Stale leftover from a prior aborted run -- safer to wipe than
        # incorporate.
        try:
            shutil.rmtree(staging_dir)
        except OSError:
            pass
    try:
        os.makedirs(staging_dir, exist_ok=True)
        # Track every path we successfully stage so MANIFEST reflects
        # what actually ended up in the tarball -- not what the caller
        # asked for. Without this, an excluded raw path stays in the
        # MANIFEST while its content is absent from the archive,
        # producing rollback instructions that reference missing
        # content.
        staged_paths: List[str] = []
        for src in selected_paths:
            if _is_excluded(
                src,
                world_root_resolved=world_root_resolved,
                iso_ts=iso_ts,
            ):
                # Defence-in-depth: build_backup_selection already
                # filtered, but a caller passing raw paths still gets
                # the right answer here.
                report.skipped_self_inclusion.append(src)
                continue
            try:
                _stage_path(src, staging_dir, world_root_resolved)
            except (OSError, ValueError) as exc:
                # ``ValueError`` covers the defense-in-depth refusal in
                # ``_stage_path`` when a raw caller passes a path that
                # escapes the world root. The contract for
                # ``create_backup`` is "NEVER raises -- failures are
                # reported in-band", so we catch both error classes
                # here, surface as ``report.error``, and clean up the
                # partial staging dir before returning.
                report.error = "stage failed for {}: {}".format(src, exc)
                shutil.rmtree(staging_dir, ignore_errors=True)
                return report
            staged_paths.append(src)

        # MANIFEST -- top-level text file inside the staged tree.
        # Sourced from staged_paths (NOT raw selected_paths) so the
        # rollback instructions reference only content that's actually
        # in the archive.
        manifest_entries: List[str] = []
        for path in staged_paths:
            try:
                rel = os.path.relpath(path, world_root_resolved)
            except ValueError:
                rel = path
            manifest_entries.append(rel.replace(os.sep, "/"))
        manifest_entries.sort()
        manifest_path = os.path.join(staging_dir, "MANIFEST")
        try:
            with open(manifest_path, "w", encoding="utf-8") as f:
                for entry in manifest_entries:
                    f.write(entry + "\n")
        except OSError as exc:
            report.error = "manifest write failed: {}".format(exc)
            shutil.rmtree(staging_dir, ignore_errors=True)
            return report
        report.manifest_entries = manifest_entries

        # Tar the staging dir into the .tmp path.
        try:
            safe_tar_create(staging_dir, tmp_path)
        except (OSError, ValueError) as exc:
            report.error = "tar create failed: {}".format(exc)
            shutil.rmtree(staging_dir, ignore_errors=True)
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            return report

        # Atomic rename + fsyncs.
        try:
            _atomic_replace_tarball(tmp_path, final_path)
        except OSError as exc:
            report.error = "atomic replace failed: {}".format(exc)
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            return report
    finally:
        # Staging cleanup is best-effort -- the tarball is durable by
        # this point, so a failed rmtree is recorded but doesn't mark
        # the backup as failed.
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)

    report.final_path = final_path
    return report
