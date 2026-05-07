"""Generic atomic-write primitive.

This module exists to host ONE thing: an atomic text-write helper that
``_world_root_io`` (and any future caller that wants the same protocol)
can layer on top of without reaching back into ``_common.py``.

Design constraints (locked by fn-15-la5.1):
    * Stdlib-only. No imports from ``_common`` or any other plugin
      module. This keeps the module on the lowest tier of the import
      graph so ``_common.py`` can later migrate its own atomic helper to
      this module without risking a cycle through ``_world_root_io``.
    * One public symbol: ``atomic_write_text``. World-root semantics
      (predicates, status enums, mount detection) live in
      ``_world_root_io``.
    * Atomic-write protocol matches the practice-scout sequence:
      ``mkstemp`` in the same directory as the target, write payload,
      ``fsync(fd)``, ``chmod`` to the requested mode, ``os.replace``,
      then ``fsync`` the parent directory so the rename is durable
      across crashes.
    * Parent-directory creation only changes the mode on first
      creation; an existing parent's permissions are NOT clobbered.
"""

from __future__ import annotations

import errno
import os
import tempfile


__all__ = ("atomic_write_text",)


def _ensure_parent_dir(parent: str, parent_mode: int) -> None:
    """Create ``parent`` with ``parent_mode`` if it does not yet exist.

    Existing directories are left untouched (no chmod), so callers do
    not silently clobber permissions on a pre-existing
    ``~/.config/alive/`` that the user has tightened to e.g. 0o500.
    """
    if parent in ("", "."):
        return
    try:
        os.mkdir(parent, parent_mode)
    except FileExistsError:
        return
    except OSError as exc:
        # ``mkdir`` may emit ENOENT when the parent's parent is
        # missing; recurse to scaffold the chain. Any other error is
        # the caller's problem and is re-raised.
        if exc.errno != errno.ENOENT:
            raise
        grandparent = os.path.dirname(parent)
        if not grandparent or grandparent == parent:
            raise
        _ensure_parent_dir(grandparent, parent_mode)
        try:
            os.mkdir(parent, parent_mode)
        except FileExistsError:
            return


def atomic_write_text(
    path,
    content,
    mode: int = 0o600,
    parent_mode: int = 0o700,
) -> None:
    """Atomically write ``content`` to ``path``.

    Parameters
    ----------
    path:
        Target file path. ``os.fspath``-compatible.
    content:
        UTF-8 text payload. Caller controls trailing newline.
    mode:
        Final file mode (octal). Defaults to ``0o600`` to match the
        config-file convention.
    parent_mode:
        Mode used when scaffolding the parent directory for the FIRST
        time. An existing parent directory is left untouched.

    Crash-safe sequence
    -------------------
    1. Create parent dir (if missing) with ``parent_mode``.
    2. ``mkstemp`` in the same parent dir (guarantees same filesystem
       so ``os.replace`` is atomic).
    3. Write payload, ``fsync(fd)``, close.
    4. ``chmod`` the temp file to ``mode``.
    5. ``os.replace(tmp, path)``.
    6. ``fsync`` the parent directory so the rename is on stable
       storage (best-effort: skipped silently on platforms that reject
       ``open(dir)``).
    """
    path = os.fspath(path)
    parent = os.path.dirname(path) or "."

    _ensure_parent_dir(parent, parent_mode)

    fd, tmp = tempfile.mkstemp(
        dir=parent,
        prefix="." + os.path.basename(path) + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # Best-effort directory fsync so the rename is durable. Pass
    # ``O_DIRECTORY`` when available so the open will refuse to
    # follow ``parent`` if it has been swapped to a non-directory
    # under us (rare race; cheap belt-and-suspenders).
    open_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        dir_fd = os.open(parent, open_flags)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
