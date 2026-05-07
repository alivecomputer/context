"""``FileSnapshot`` -- foundation primitive for content-fingerprint detection.

Walks the world + plugin install root at the start of an upgrade run,
captures raw bytes (or just presence) of inputs that downstream phase-3
detection (T3) and phase-3 retired-pattern pre-scan (T4) consume.

Why a snapshot
--------------
The detection + walkthrough-eligibility pre-scan must read from a
**frozen** view of the world. Phase 8 (cleanup), phase 9 (migrate),
and phase 10 (surface dispatch) mutate disk; if T3/T4 were re-reading
files at decision time, an in-flight write could change the answer
under them. The snapshot is built once in phase 2 and shared by every
later phase that needs world content.

Contract
--------
* Globs are templated absolute paths. Templates: ``<world>`` (expanded
  to ``world_root_resolved``) and ``<plugin_root>``. Raw absolute paths
  without templates are accepted as-is.
* Glob engine is the stdlib ``glob.glob(pattern, recursive=True)``;
  ``Path.glob`` is **not** used because it rejects absolute patterns.
* Match results are sorted lexically and deduplicated by their
  template-expanded path. The same lexical path produced by two rules
  is read once; the more permissive mode wins
  (``full > head > exists_only``).
* Realpath is **not** used for keying or dedup. Symlinks are part of
  the model: aliased lexical paths produce separate snapshot entries
  intentionally.
* Default exclusions are applied AFTER rule expansion but BEFORE read.
  Excluded paths land in ``snapshot.skipped[]`` for forensic visibility.
* The returned ``FileSnapshot`` is frozen; mutating attempts raise
  ``FileSnapshotFrozenError``.
"""

from __future__ import annotations

import glob as _glob
import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple


__all__ = (
    "DEFAULT_MAX_BYTES",
    "FileSnapshot",
    "FileSnapshotFrozenError",
    "SnapshotRule",
    "iter_default_excludes",
)


#: Files larger than this in bytes are skipped by ``full`` rules; head
#: rules clip rather than skip.
DEFAULT_MAX_BYTES = 256 * 1024  # 256 KiB

# Default exclusions. Patterns ending in ``/`` are subtree exclusions
# (path startswith "<world_root>/<pattern>"). Plain names are exact
# basename matches under ``.alive/`` only when the path is a direct
# child of ``.alive/``. We keep this simple by listing path-relative
# fragments matched against the fragment from world_root.
_DEFAULT_EXCLUDE_SUBTREES = (
    ".alive/upgrades/",
)
_DEFAULT_EXCLUDE_PREFIXES = (
    ".alive/.rollback-",
)
_DEFAULT_EXCLUDE_BASENAMES_UNDER_ALIVE = (
    ".system-upgrade.lock",
    ".system-upgrade.lock-meta.json",
)

# Heuristic for binary detection: a NUL byte in the first ``_BIN_SCAN``
# bytes flags the file as binary and excludes it.
_BIN_SCAN = 8 * 1024


def iter_default_excludes() -> List[str]:
    """Return a human-readable list of the default exclusion rules.

    Useful for logging and tests; callers should not rely on the
    string format being machine-stable.
    """
    out = []
    for s in _DEFAULT_EXCLUDE_SUBTREES:
        out.append("subtree:{}".format(s))
    for s in _DEFAULT_EXCLUDE_PREFIXES:
        out.append("prefix:{}".format(s))
    for s in _DEFAULT_EXCLUDE_BASENAMES_UNDER_ALIVE:
        out.append("basename-under-.alive:{}".format(s))
    out.append("max-bytes:{}".format(DEFAULT_MAX_BYTES))
    out.append("binary:nul-in-first-{}-bytes".format(_BIN_SCAN))
    return out


class FileSnapshotFrozenError(RuntimeError):
    """Raised when a caller mutates a populated ``FileSnapshot``."""


@dataclass(frozen=True)
class SnapshotRule:
    """One input rule for ``FileSnapshot.populate``.

    Three modes (precedence ``full > head > exists_only`` when two
    rules match the same lexical path):

    * ``max_bytes is None and exists_only is False``  -- full content read.
    * ``max_bytes is N and exists_only is False``     -- head-mode (read
      first ``N`` bytes only; bypasses the default-max-bytes exclusion
      because head rules clip rather than skip).
    * ``exists_only is True``                         -- presence/absence
      only; no bytes captured.

    The ``glob`` field accepts the templates ``<world>`` and
    ``<plugin_root>``. Templates are substituted at ``populate`` time;
    raw absolute paths without templates are also accepted.
    """

    glob: str
    max_bytes: Optional[int] = None
    exists_only: bool = False

    @property
    def mode(self) -> str:
        """Human-readable mode label for diagnostics."""
        if self.exists_only:
            return "exists_only"
        if self.max_bytes is None:
            return "full"
        return "head"


def _mode_priority(mode: str) -> int:
    """Lower is more permissive. Used to pick the winner on dedup."""
    return {"full": 0, "head": 1, "exists_only": 2}[mode]


def _expand_path(template: str, world_root: str, plugin_root: str) -> str:
    """Expand ``<world>`` / ``<plugin_root>`` templates in *template*."""
    out = template
    out = out.replace("<world>", world_root)
    out = out.replace("<plugin_root>", plugin_root)
    return out


def _is_under_subtree(rel: str, subtree: str) -> bool:
    """``rel`` startswith ``subtree`` (segment-aware)."""
    return rel == subtree.rstrip("/") or rel.startswith(subtree)


def _excluded_reason(world_root: str, path: str) -> Optional[str]:
    """Return an exclusion reason for *path*, or None if not excluded.

    Path-only check; the size+binary checks happen at read time.
    """
    try:
        rel = os.path.relpath(path, world_root)
    except ValueError:
        # Different drive on Windows; never excluded by world-relative rules.
        return None
    rel = rel.replace(os.sep, "/")
    # Codex completion-review fix (R20 idempotency): direct-child ``.yaml``
    # files under ``.alive/upgrades/`` are the canonical upgrade-record
    # artefacts that the prior-record floor lift consults to keep
    # detection at-target after demo_cleanup removes the on-disk v3.2
    # signal. The broad ``.alive/upgrades/`` subtree exclusion exists
    # to keep ``pre-upgrade-<ts>.tar.gz`` blobs out of the snapshot;
    # carve out the YAML records so the explicit
    # ``<world>/.alive/upgrades/*.yaml`` snapshot rule actually
    # captures them. Nested paths and tarballs remain excluded.
    if rel.startswith(".alive/upgrades/"):
        tail = rel[len(".alive/upgrades/"):]
        if tail and "/" not in tail and tail.endswith(".yaml"):
            return None
    for sub in _DEFAULT_EXCLUDE_SUBTREES:
        if _is_under_subtree(rel, sub):
            return "default-subtree:{}".format(sub)
    for prefix in _DEFAULT_EXCLUDE_PREFIXES:
        if rel.startswith(prefix):
            return "default-prefix:{}".format(prefix)
    # basename-under-.alive
    if rel.startswith(".alive/"):
        tail = rel[len(".alive/"):]
        if "/" not in tail and tail in _DEFAULT_EXCLUDE_BASENAMES_UNDER_ALIVE:
            return "default-basename-under-.alive:{}".format(tail)
    return None


def _looks_binary(blob: bytes) -> bool:
    """NUL-byte heuristic in the first 8 KiB."""
    return b"\x00" in blob[:_BIN_SCAN]


@dataclass
class _Entry:
    """Internal: per-path captured state."""

    path: str  # template-expanded absolute path (snapshot key)
    mode: str  # "full" | "head" | "exists_only"
    exists: bool
    data: Optional[bytes] = None  # None for exists_only OR missing files
    size: int = 0  # actual file size at capture time (0 if missing)
    skipped_reason: Optional[str] = None


@dataclass
class FileSnapshot:
    """Frozen view of selected paths under the world + plugin root.

    Construct via :meth:`populate`; instances should not be mutated
    after that returns.
    """

    world_root: str
    plugin_root: str
    _entries: Dict[str, _Entry] = field(default_factory=dict)
    _frozen: bool = False
    skipped: List[Tuple[str, str]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def populate(
        cls,
        world_root: str,
        plugin_root: str,
        rules: List[SnapshotRule],
    ) -> "FileSnapshot":
        """Walk the rule list and capture matched paths.

        Returns a frozen ``FileSnapshot``. Subsequent mutation attempts
        (``set_entry`` etc.) raise ``FileSnapshotFrozenError``.
        """
        snap = cls(world_root=world_root, plugin_root=plugin_root)

        # Step 1: expand each rule, glob, and collect candidate paths
        # tagged with the rule that produced them.
        candidates: Dict[str, _Entry] = {}
        for rule in rules:
            pattern = _expand_path(rule.glob, world_root, plugin_root)
            matches = _glob.glob(pattern, recursive=True)
            matches.sort()
            for raw in matches:
                # Lexical path is the snapshot key. We do NOT realpath
                # here -- aliased symlinked paths are intentionally
                # kept as separate entries.
                key = os.path.abspath(raw)
                # Skip directories silently for content rules; for
                # exists_only rules a directory match is recorded as
                # exists=True with mode=exists_only.
                if os.path.isdir(key) and not rule.exists_only:
                    continue
                # Apply default path exclusions BEFORE read.
                excl = _excluded_reason(world_root, key)
                if excl is not None:
                    snap.skipped.append((key, excl))
                    continue

                entry = _Entry(
                    path=key, mode=rule.mode,
                    exists=True,
                )
                # Precedence merge: keep the more permissive (lower-priority) mode.
                prev = candidates.get(key)
                if prev is None:
                    candidates[key] = entry
                else:
                    if _mode_priority(rule.mode) < _mode_priority(prev.mode):
                        # Promote to a more permissive mode.
                        prev.mode = rule.mode

        # Step 2: read each candidate per its winning mode.
        # We also need to honor max_bytes from the rule that won.
        # We simplified by tracking only the mode label above; for head
        # mode we look up the smallest max_bytes across head-mode rules
        # that matched the path. This keeps the contract intact even
        # when two head rules disagree on N (smallest wins; conservative).
        head_max: Dict[str, int] = {}
        for rule in rules:
            if rule.mode != "head":
                continue
            pattern = _expand_path(rule.glob, world_root, plugin_root)
            for raw in _glob.glob(pattern, recursive=True):
                key = os.path.abspath(raw)
                cur = head_max.get(key)
                cap = rule.max_bytes if rule.max_bytes is not None else 0
                if cur is None or cap < cur:
                    head_max[key] = cap

        for key, entry in candidates.items():
            if entry.mode == "exists_only":
                # Existence already True by virtue of being in candidates.
                snap._entries[key] = entry
                continue
            try:
                stat = os.stat(key)
            except OSError as exc:
                # Race: file vanished between glob and stat.
                snap.skipped.append((key, "stat-error:{}".format(exc)))
                continue
            entry.size = stat.st_size

            if entry.mode == "full" and stat.st_size > DEFAULT_MAX_BYTES:
                snap.skipped.append((
                    key,
                    "default-max-bytes:size={} > {}".format(
                        stat.st_size, DEFAULT_MAX_BYTES
                    ),
                ))
                continue

            try:
                # Always read up to the relevant cap; head mode reads
                # first N bytes only.
                if entry.mode == "head":
                    cap = head_max.get(key, DEFAULT_MAX_BYTES)
                    with open(key, "rb") as f:
                        data = f.read(cap)
                else:
                    with open(key, "rb") as f:
                        data = f.read(DEFAULT_MAX_BYTES + 1)
                        # Re-check size mid-flight against cap.
                        if len(data) > DEFAULT_MAX_BYTES:
                            snap.skipped.append((
                                key,
                                "default-max-bytes:read>cap",
                            ))
                            continue
            except OSError as exc:
                snap.skipped.append((key, "read-error:{}".format(exc)))
                continue

            if _looks_binary(data):
                snap.skipped.append((key, "default-binary:nul-detected"))
                continue

            entry.data = data
            snap._entries[key] = entry

        snap._frozen = True
        return snap

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def paths(self) -> List[str]:
        """Sorted list of every path captured in the snapshot."""
        return sorted(self._entries.keys())

    def exists(self, path: str) -> bool:
        """True iff *path* was matched by an ``exists_only`` rule and present.

        Also returns True for paths captured under ``full`` / ``head``
        modes (those imply the path exists).
        """
        key = os.path.abspath(path)
        entry = self._entries.get(key)
        return entry is not None and entry.exists

    def read(self, path: str) -> bytes:
        """Return captured bytes for *path*.

        Raises:
            KeyError -- ``path`` was not in the snapshot.
            ValueError -- ``path`` was captured under ``exists_only``
                (no bytes available).
        """
        key = os.path.abspath(path)
        entry = self._entries.get(key)
        if entry is None:
            raise KeyError("path not in snapshot: {}".format(path))
        if entry.mode == "exists_only":
            raise ValueError(
                "path {} was captured under exists_only; bytes unavailable"
                .format(path)
            )
        if entry.data is None:
            raise ValueError(
                "path {} has no data captured (mode={}, size={})"
                .format(path, entry.mode, entry.size)
            )
        return entry.data

    def mode(self, path: str) -> str:
        """Mode (``full`` | ``head`` | ``exists_only``) for *path*."""
        key = os.path.abspath(path)
        entry = self._entries.get(key)
        if entry is None:
            raise KeyError("path not in snapshot: {}".format(path))
        return entry.mode

    # ------------------------------------------------------------------
    # Mutation guard
    # ------------------------------------------------------------------

    def _ensure_unfrozen(self) -> None:
        if self._frozen:
            raise FileSnapshotFrozenError(
                "FileSnapshot is frozen; rebuild via populate() to alter rules"
            )
