"""``PostStateOverlay`` -- virtual mutation layer atop a ``FileSnapshot``.

Phase 11 (verify) needs to run against the post-migration world. On a
real run that's the live filesystem. Under ``--dry-run`` we cannot
write to disk, so phases 8-10 mutate this overlay instead and verify
reads through it.

Semantics
---------
* ``set(path, data)`` records ``data: bytes`` as the post-state content
  for ``path``.
* ``set(path, None)`` records ``path`` as DELETED in post-state. A
  subsequent ``read_through`` raises ``FileNotFoundError`` for that
  path even if the snapshot has bytes for it.
* ``read_through(path, snapshot)`` looks up the overlay first; if
  present and non-deleted, returns the overlay bytes. Else falls
  through to ``snapshot.read(path)``. If both miss, raises
  ``FileNotFoundError``.
* The overlay is mutable by design (phases progressively populate it);
  there is no freeze step.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional


__all__ = ("PostStateOverlay",)


# Sentinel mapping for an explicit "delete this path in post-state" entry.
_DELETED = object()


class PostStateOverlay:
    """Mutable mapping from absolute path -> bytes-or-deleted.

    Constructor takes no arguments. Use ``set`` to record overlay
    state, ``read_through`` to query.
    """

    def __init__(self) -> None:
        self._store: Dict[str, object] = {}

    @staticmethod
    def _key(path: str) -> str:
        return os.path.abspath(path)

    def set(self, path: str, data: Optional[bytes]) -> None:
        """Record the post-state for *path*.

        ``data=None`` records a deletion. ``data: bytes`` records a
        write. The overlay does not validate the bytes; callers are
        expected to pass canonical post-migration content.
        """
        key = self._key(path)
        if data is None:
            self._store[key] = _DELETED
            return
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                "PostStateOverlay.set expects bytes or None; got {}"
                .format(type(data).__name__)
            )
        self._store[key] = bytes(data)

    def has(self, path: str) -> bool:
        """True iff *path* has any overlay entry (deleted or written)."""
        return self._key(path) in self._store

    def is_deleted(self, path: str) -> bool:
        """True iff *path* is recorded as deleted in the overlay."""
        return self._store.get(self._key(path)) is _DELETED

    def read_through(self, path: str, snapshot) -> bytes:
        """Read *path*, overlay-first, snapshot-second.

        Raises ``FileNotFoundError`` when the overlay records a deletion
        for *path*, OR when neither the overlay nor the snapshot has
        bytes for it. Bubbles ``ValueError`` from ``snapshot.read``
        unchanged when the snapshot captured *path* under ``exists_only``.
        """
        key = self._key(path)
        entry = self._store.get(key)
        if entry is _DELETED:
            raise FileNotFoundError(
                "post-state overlay records deletion: {}".format(path)
            )
        if entry is not None:
            return entry  # type: ignore[return-value]
        # Fall through to the snapshot.
        try:
            return snapshot.read(path)
        except KeyError:
            raise FileNotFoundError(
                "neither overlay nor snapshot has bytes for {}".format(path)
            )

    def paths(self) -> List[str]:
        """Sorted list of every path with an overlay entry."""
        return sorted(self._store.keys())
