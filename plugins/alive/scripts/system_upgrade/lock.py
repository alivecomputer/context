"""``UpgradeLock`` -- holder-aware wrapper around ``_common.flock_file``.

Wraps the shared advisory-lock primitive so the upgrade orchestrator can:

* attach holder metadata (``squirrel_id``, ``started_iso``,
  ``tool_version``, ``pid``) to a JSON sidecar when the lock is held;
* surface contention as ``UpgradeLockBusy`` carrying the prior
  holder's metadata so the operator can grep for the squirrel UUID;
* perform best-effort ordered cleanup on release (meta first, then the
  flock itself) without converting "remaining file" into a hard
  failure.

The contract for cleanup is final-state, not transactional: by the
time ``release()`` returns, neither the meta file nor the flock file
should exist; if either remains it is recorded as a release-time
warning. Tests assert final-state absence -- not cross-file atomicity
(impossible across two ``os.unlink`` calls).

``WrongLockError`` retains its existing ``_common`` meaning (guard
misuse for split-lock callers); we **do not** raise it for contention.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any, Dict, Optional

import _common
from _atomic_io import atomic_write_text


__all__ = (
    "LOCK_RELATIVE_PATH",
    "LOCK_META_RELATIVE_PATH",
    "UpgradeLock",
    "UpgradeLockBusy",
    "build_squirrel_id",
)


#: Path of the flock sentinel file relative to the world root.
LOCK_RELATIVE_PATH = ".alive/.system-upgrade.lock"

#: Path of the lock-meta JSON sidecar relative to the world root.
LOCK_META_RELATIVE_PATH = ".alive/.system-upgrade.lock-meta.json"


def build_squirrel_id(session_id_override: Optional[str] = None) -> str:
    """Return the 8-char squirrel id for the current upgrade run.

    Reads via ``_common.resolve_session_id`` (which honours
    ``ALIVE_SESSION_ID`` then ``CLAUDE_SESSION_ID`` then synthesizes a
    hex8-prefixed anonymous id). The first 8 characters of the
    resolved session ID are returned -- per the bible's
    ``squirrel_short_id = session_id[:8]`` convention.

    When the resolver synthesises an id (no env var present) the
    output already starts with 8 hex chars, so ``[:8]`` still yields
    a stable 8-char label. CI canary runs that bypass the resolver
    entirely should pass an explicit override.

    The literal ``"cli-anon"`` prefix called out in the spec is a
    historical artefact of an earlier draft -- the current
    ``resolve_session_id`` synthesis already produces hex8-prefixed
    identifiers, so we return ``[:8]`` of whatever the resolver
    yields. CLI callers that want a forensic distinction from
    Claude-driven runs should set ``ALIVE_SESSION_ID`` themselves.
    """
    sid = _common.resolve_session_id(session_id_override)
    return _common.squirrel_short_id(sid)


class UpgradeLockBusy(RuntimeError):
    """Raised when the upgrade lock is already held by another run.

    Wraps the prior holder's metadata so the operator can disambiguate
    "I have two upgrades racing" from "I have a stuck lock".

    Attributes
    ----------
    holder : dict | None
        Parsed ``.system-upgrade.lock-meta.json`` body, or ``None``
        when the meta file was missing / unreadable. Keys: ``squirrel_id``,
        ``started_iso``, ``tool_version``, ``pid``.
    lock_path : str
        Absolute path of the flock sentinel.
    """

    def __init__(
        self,
        message: str,
        holder: Optional[Dict[str, Any]] = None,
        lock_path: str = "",
    ) -> None:
        super().__init__(message)
        self.holder = holder
        self.lock_path = lock_path


class UpgradeLock:
    """Holder-aware wrapper over ``_common.flock_file``.

    Usage::

        lock = UpgradeLock(world_root_resolved)
        lock.acquire(tool_version="3.2.0")
        try:
            ...  # phases 1-12
        finally:
            lock.release()

    Or as a context manager::

        with UpgradeLock(world_root).held(tool_version="3.2.0") as guard:
            ...

    The lock paths are derived from ``world_root`` at construction time;
    callers MUST pass the **resolved** (``os.path.realpath``-applied)
    world root so the lock and its sidecar land in the canonical
    location regardless of which symlink the user typed.
    """

    def __init__(
        self,
        world_root_resolved: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._world_root = os.path.abspath(os.fspath(world_root_resolved))
        self._timeout = float(timeout_seconds)
        self._guard_ctx = None  # active context manager, set by acquire()
        self._guard = None  # LockGuard token from _common.flock_file
        self._meta_written = False

    @property
    def lock_path(self) -> str:
        """Absolute path of the flock sentinel."""
        return os.path.join(self._world_root, LOCK_RELATIVE_PATH)

    @property
    def lock_meta_path(self) -> str:
        """Absolute path of the lock-meta JSON sidecar."""
        return os.path.join(self._world_root, LOCK_META_RELATIVE_PATH)

    def _read_meta(self) -> Optional[Dict[str, Any]]:
        """Return the holder dict from the meta sidecar or None on miss."""
        try:
            with open(self.lock_meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def acquire(
        self,
        tool_version: str,
        squirrel_id: Optional[str] = None,
        started_iso: Optional[str] = None,
    ) -> None:
        """Acquire the lock and write the holder-metadata sidecar.

        Order:
            1. Enter ``_common.flock_file`` (raises ``FlockTimeoutError``
               on contention; we translate to ``UpgradeLockBusy``).
            2. Atomically write ``.system-upgrade.lock-meta.json`` with
               ``{squirrel_id, started_iso, tool_version, pid}``.

        Raises
        ------
        UpgradeLockBusy
            Another run holds the lock; ``self.holder`` carries the
            prior holder's metadata when available.
        """
        if self._guard_ctx is not None:
            raise RuntimeError("UpgradeLock.acquire() called twice")

        ctx = _common.flock_file(
            self.lock_path, timeout_seconds=self._timeout
        )
        try:
            guard = ctx.__enter__()
        except _common.FlockTimeoutError as exc:
            holder = self._read_meta()
            raise UpgradeLockBusy(
                "system-upgrade lock held: {}".format(exc),
                holder=holder,
                lock_path=self.lock_path,
            ) from exc

        self._guard_ctx = ctx
        self._guard = guard

        # Lock now held. Build and write the sidecar.
        meta = {
            "squirrel_id": (
                squirrel_id if squirrel_id is not None
                else build_squirrel_id()
            ),
            "started_iso": (
                started_iso if started_iso is not None
                else _common.iso_now()
            ),
            "tool_version": tool_version,
            "pid": os.getpid(),
        }
        try:
            atomic_write_text(
                self.lock_meta_path,
                json.dumps(meta, indent=2, sort_keys=True) + "\n",
                mode=0o600,
            )
        except Exception:
            # Sidecar write failed; release the flock so we don't leave
            # the world locked without holder metadata.
            self._release_flock_silent()
            raise
        self._meta_written = True

    def release(self) -> Dict[str, Any]:
        """Best-effort ordered cleanup; return a release report.

        Cleanup order:
            1. ``unlink`` the lock-meta sidecar.
            2. Exit the ``flock_file`` context manager (releases the
               kernel lock; on POSIX the flock fd is closed and the
               sentinel file remains on disk by design).
            3. ``unlink`` the flock sentinel file.

        Each step tolerates ``FileNotFoundError`` (an interrupted prior
        run may have left only one of the two files). Other ``OSError``
        instances are recorded in the report's ``warnings[]`` so the
        operator can investigate without the upgrade itself failing.

        Returns
        -------
        dict
            ``{"meta_present_after": bool, "lock_present_after": bool,
            "warnings": list[str]}``. Both ``*_after`` flags should be
            ``False`` for a clean release; tests assert final-state
            absence.
        """
        warnings = []
        # 1. unlink the meta sidecar
        try:
            os.unlink(self.lock_meta_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            warnings.append(
                "could not remove lock-meta {}: {}".format(
                    self.lock_meta_path, exc
                )
            )

        # 2. release the flock
        if self._guard_ctx is not None:
            try:
                self._guard_ctx.__exit__(None, None, None)
            except Exception as exc:
                warnings.append(
                    "flock release raised: {}".format(exc)
                )
            self._guard_ctx = None
            self._guard = None

        # 3. unlink the flock sentinel
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            warnings.append(
                "could not remove lock file {}: {}".format(
                    self.lock_path, exc
                )
            )

        return {
            "meta_present_after": os.path.exists(self.lock_meta_path),
            "lock_present_after": os.path.exists(self.lock_path),
            "warnings": warnings,
        }

    def _release_flock_silent(self) -> None:
        """Internal: release the flock context, swallowing exceptions."""
        if self._guard_ctx is not None:
            try:
                self._guard_ctx.__exit__(None, None, None)
            except Exception:
                pass
            self._guard_ctx = None
            self._guard = None

    # Context-manager sugar ---------------------------------------------------

    class _Held:
        """Context-manager view of an UpgradeLock held for a code block."""

        def __init__(self, parent: "UpgradeLock", **kwargs: Any) -> None:
            self._parent = parent
            self._kwargs = kwargs

        def __enter__(self) -> "UpgradeLock":
            self._parent.acquire(**self._kwargs)
            return self._parent

        def __exit__(self, exc_type, exc, tb) -> None:
            self._parent.release()

    def held(self, **kwargs: Any) -> "_Held":
        """Return a context manager that acquires + releases the lock."""
        return UpgradeLock._Held(self, **kwargs)
