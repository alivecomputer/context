"""Tarball create / extract helpers (LD22-conformant).

Extracted verbatim from ``alive-p2p.py`` (T2 of fn-18). Public surface
(stable names preserved):

- ``safe_tar_create(source_dir, output_path, strip_prefix=None)``
- ``safe_tar_extract(archive_path, output_dir)``
- ``safe_extractall``  -- LD22 spec-name alias of ``safe_tar_extract``
- ``tar_list_entries(archive_path)``

Behaviour preserved exactly: the LD22 pre-validation guards, PAX/GNU
long-name tolerance, the inner-staging-dir + atomic-rename extract
flow, and the macOS resource-fork suppression all transfer here
verbatim. The redesign tasks (T5/T11) consume this module; the
``alive-p2p.py`` shim re-exports the same names for backward compat.

Stdlib-only.
"""

from __future__ import annotations

import inspect
import os
import shutil
import tarfile
import tempfile
from typing import List, Optional, Set


# Files and patterns to exclude from archives
_TAR_EXCLUDES = {".DS_Store", "Thumbs.db", "Icon\r", "__MACOSX"}


def _is_excluded(name):
    # type: (str) -> bool
    """Check whether a tar entry name should be excluded."""
    base = os.path.basename(name)
    if base in _TAR_EXCLUDES:
        return True
    # macOS resource fork files
    if base.startswith("._"):
        return True
    return False


def _resolve_path(base, name):
    # type: (str, str) -> Optional[str]
    """Resolve *name* relative to *base* and check it stays inside *base*.

    Returns the resolved absolute path, or None if the entry escapes.
    """
    # Reject absolute paths outright
    if os.path.isabs(name):
        return None
    target = os.path.normpath(os.path.join(base, name))
    # Must start with base (use trailing sep to avoid prefix tricks)
    if not (target == base or target.startswith(base + os.sep)):
        return None
    return target


def safe_tar_create(source_dir, output_path, strip_prefix=None):
    # type: (str, str, Optional[str]) -> None
    """Create a tar.gz archive from *source_dir*.

    - Sets ``COPYFILE_DISABLE=1`` to suppress macOS resource forks.
    - Excludes ``.DS_Store``, ``Thumbs.db``, ``._*`` files.
    - Rejects symlinks that resolve outside *source_dir*.
    - Optional *strip_prefix* removes a leading path component from entries.
    """
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        raise FileNotFoundError("Source directory not found: {0}".format(source_dir))

    # Suppress macOS resource forks (affects C-level tar inside python too)
    os.environ["COPYFILE_DISABLE"] = "1"

    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with tarfile.open(output_path, "w:gz") as tar:
        for root, dirs, files in os.walk(source_dir):
            # Skip excluded directories in-place
            dirs[:] = [
                d for d in dirs
                if d not in _TAR_EXCLUDES and not d.startswith("._")
            ]

            for name in sorted(files):
                if _is_excluded(name):
                    continue

                full_path = os.path.join(root, name)

                # Reject symlinks that escape source_dir
                if os.path.islink(full_path):
                    real = os.path.realpath(full_path)
                    if not (real == source_dir
                            or real.startswith(source_dir + os.sep)):
                        raise ValueError(
                            "Symlink escapes source: {0} -> {1}".format(full_path, real)
                        )

                arcname = os.path.relpath(full_path, source_dir)
                if strip_prefix:
                    if arcname.startswith(strip_prefix):
                        arcname = arcname[len(strip_prefix):]
                        arcname = arcname.lstrip(os.sep)

                tar.add(full_path, arcname=arcname)

            # Also add directories that are symlinks (check safety)
            for d in dirs:
                dir_path = os.path.join(root, d)
                if os.path.islink(dir_path):
                    real = os.path.realpath(dir_path)
                    if not (real == source_dir
                            or real.startswith(source_dir + os.sep)):
                        raise ValueError(
                            "Symlink escapes source: {0} -> {1}".format(dir_path, real)
                        )


# LD22 caps. Member count cap is high enough for the largest realistic walnut
# (~5000 files in our worst-case fixture) but low enough to bound memory use
# when validating a hostile tar.
_LD22_MAX_MEMBERS = 10000
_LD22_MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB

# Tar metadata member types that don't write filesystem entries: PAX headers
# and GNU longname/longlink. Tolerated and skipped during pre-validation.
_LD22_METADATA_TYPES = frozenset(
    t for t in (
        getattr(tarfile, "XHDTYPE", None),
        getattr(tarfile, "XGLTYPE", None),
        getattr(tarfile, "GNUTYPE_LONGNAME", None),
        getattr(tarfile, "GNUTYPE_LONGLINK", None),
    )
    if t is not None
)


def _ld22_validate_members(members, dest_abs):
    # type: (List[tarfile.TarInfo], str) -> None
    """Pre-validate every tar member per LD22. Raises ValueError on any
    rejection. Performs no filesystem writes.

    Rules (in order):
        - Member count cap (10000)
        - Skip PAX / GNU long-name metadata members
        - Reject symlinks and hardlinks outright (any target)
        - Reject device / fifo / block members
        - Allowlist regular files and directories only
        - Cap cumulative regular file size (500 MB)
        - Reject backslashes in member names
        - Normalize ``./`` prefix and reject empty / pure-slash names
        - Reject ``..`` segments and intermediate ``.`` segments
        - Reject absolute POSIX paths and Windows drive letters
        - Reject duplicate effective member paths
        - Reject post-normalisation paths that escape ``dest_abs``
    """
    if len(members) > _LD22_MAX_MEMBERS:
        raise ValueError(
            "Tar has {0} members; cap is {1}".format(
                len(members), _LD22_MAX_MEMBERS
            )
        )

    total = 0
    seen_effective = set()  # type: Set[str]

    for m in members:
        # Skip PAX / GNU long-name metadata members; they don't materialise
        # as filesystem entries.
        if m.type in _LD22_METADATA_TYPES:
            continue

        # Reject filesystem-writing dangerous types outright (LD22 v10).
        if m.issym() or m.islnk():
            raise ValueError(
                "Symlink/hardlink not allowed: {0!r}".format(m.name)
            )
        if m.ischr() or m.isblk() or m.isfifo():
            raise ValueError(
                "Device or fifo member: {0!r}".format(m.name)
            )

        # Allowlist: only regular files and directories from here on (LD22 v13).
        if not (m.isfile() or m.isdir()):
            raise ValueError(
                "Unsupported tar member type for {0!r}".format(m.name)
            )

        if m.isfile():
            total += m.size
            if total > _LD22_MAX_TOTAL_BYTES:
                raise ValueError(
                    "Tar expands to > {0} bytes".format(_LD22_MAX_TOTAL_BYTES)
                )

        # Reject backslashes (LD22 v12).
        if "\\" in m.name:
            raise ValueError(
                "Backslash in member name: {0!r}".format(m.name)
            )

        # Normalize: strip leading ``./`` (legitimate tar convention).
        normalized = m.name
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized or normalized.strip("/") == "":
            raise ValueError(
                "Empty or invalid member name: {0!r}".format(m.name)
            )

        # Reject ``..`` segments and intermediate ``.`` segments (LD22 v12).
        parts = normalized.split("/")
        for part in parts:
            if part == "..":
                raise ValueError(
                    "Parent-dir segment: {0!r}".format(m.name)
                )
            if part == ".":
                raise ValueError(
                    "Intermediate dot-segment: {0!r}".format(m.name)
                )

        # Reject absolute POSIX paths and Windows drive letters (LD22 v9).
        if normalized.startswith("/") or (
            len(normalized) >= 2
            and normalized[1] == ":"
            and normalized[0].isalpha()
        ):
            raise ValueError(
                "Absolute path member: {0!r}".format(m.name)
            )

        # Reject duplicate effective member paths (LD22 v12).
        # Normalise trailing slashes so ``foo`` and ``foo/`` collide.
        effective = normalized.rstrip("/")
        if effective in seen_effective:
            raise ValueError(
                "Duplicate effective member path: {0!r}".format(m.name)
            )
        seen_effective.add(effective)

        # Final defence: post-normalisation join must stay inside dest.
        joined = os.path.normpath(os.path.join(dest_abs, normalized))
        if not (joined == dest_abs or joined.startswith(dest_abs + os.sep)):
            raise ValueError(
                "Path traversal member: {0!r}".format(m.name)
            )


def safe_tar_extract(archive_path, output_dir):
    # type: (str, str) -> None
    """Extract a tar.gz archive with LD22 pre-validation safety.

    Pre-validates ALL members before any extraction. Zero filesystem writes
    on rejection. Implements the LD22 acceptance contract:

    - Rejects path traversal (``../``)
    - Rejects absolute POSIX paths and Windows drive letters
    - Rejects ANY symlink or hardlink member outright
    - Rejects device / fifo / block members
    - Rejects member types other than regular file or directory
    - Rejects backslashes in member names
    - Rejects duplicate effective member paths (e.g. ``foo`` + ``./foo``)
    - Rejects ``..`` and intermediate ``.`` path segments
    - Caps cumulative file size at 500 MB
    - Caps member count at 10000
    - Tolerates PAX header and GNU long-name metadata members (skipped)

    Extraction goes through an inner staging dir on the same filesystem so
    a mid-extract failure leaves ``output_dir`` empty.
    """
    archive_path = os.path.abspath(archive_path)
    output_dir = os.path.abspath(output_dir)

    if not os.path.isfile(archive_path):
        raise FileNotFoundError("Archive not found: {0}".format(archive_path))

    os.makedirs(output_dir, exist_ok=True)

    # Inner staging dir on the same filesystem so the post-validate move is a
    # cheap rename. The staging dir is always cleaned up in ``finally``.
    parent = os.path.dirname(output_dir)
    staging = tempfile.mkdtemp(dir=parent, prefix=".p2p-extract-")

    try:
        try:
            tar = tarfile.open(archive_path, "r:*")
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise ValueError(
                "Corrupt or unreadable tar archive at {0}: {1}".format(
                    archive_path, exc
                )
            )
        with tar:
            try:
                members = tar.getmembers()
            except (tarfile.TarError, EOFError) as exc:
                raise ValueError(
                    "Corrupt tar archive at {0}: {1}".format(archive_path, exc)
                )

            # LD22 pre-validation: zero writes on any rejection.
            _ld22_validate_members(members, staging)

            # All members passed pre-validation. Now extract.
            # Python 3.12+ supports extractall(filter='data'); use it as
            # additional defence-in-depth when available.
            try:
                sig = inspect.signature(tar.extractall)
                supports_filter = "filter" in sig.parameters
            except (TypeError, ValueError):
                supports_filter = False
            try:
                if supports_filter:
                    tar.extractall(path=staging, filter="data")
                else:
                    tar.extractall(path=staging)
            except (tarfile.TarError, EOFError) as exc:
                raise ValueError(
                    "Corrupt tar archive at {0}: {1}".format(
                        archive_path, exc
                    )
                )

        # Move contents from inner staging into output_dir.
        for item in os.listdir(staging):
            src = os.path.join(staging, item)
            dst = os.path.join(output_dir, item)
            if os.path.exists(dst):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                else:
                    os.remove(dst)
            os.replace(src, dst)

    finally:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


# Public LD22 alias used by docstrings and external callers. Identical
# behaviour to ``safe_tar_extract``; the alias matches the LD22 spec name.
safe_extractall = safe_tar_extract


def tar_list_entries(archive_path):
    # type: (str) -> List[str]
    """Return a list of entry names in a tar archive."""
    archive_path = os.path.abspath(archive_path)
    if not os.path.isfile(archive_path):
        raise FileNotFoundError("Archive not found: {0}".format(archive_path))

    with tarfile.open(archive_path, "r:*") as tar:
        return [m.name for m in tar.getmembers()]


__all__ = (
    "safe_tar_create",
    "safe_tar_extract",
    "safe_extractall",
    "tar_list_entries",
)
