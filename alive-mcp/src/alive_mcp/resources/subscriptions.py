"""Watchdog observer -> MCP resource notifications (fn-10-60k.11 / T11).

This module connects the two halves of the resource subscription story:

* **The filesystem side** -- a :class:`watchdog.observers.Observer` running
  a single recursive watch over the resolved World root. Watchdog uses
  thread-based emitters on every supported platform (FSEvents on macOS,
  inotify on Linux, ReadDirectoryChangesW on Windows); the handler below
  therefore runs OFF the asyncio event loop and must marshal work back
  via :func:`asyncio.run_coroutine_threadsafe`.
* **The protocol side** -- a :class:`SubscriptionRegistry` tracking which
  resource URIs have at least one active subscriber, plus the async
  emission routine that fires ``notifications/resources/updated`` and
  ``notifications/resources/list_changed`` through the active
  :class:`mcp.server.session.ServerSession`.

The two halves talk through :class:`KernelEventHandler`, which filters
raw filesystem events down to the five URI-bearing kernel files
(``key.md``, ``log.md``, ``insights.md``, ``now.json``, and the v2
fallback ``_generated/now.json``), debounces per ``(walnut, file)`` key
for 500ms, and only emits notifications for URIs that a client is
actually subscribed to.

Design choices
--------------
**Single observer for server lifetime.** FastMCP v1.27 does not expose
reliable per-subscriber lifecycle hooks -- disconnect detection may not
emit ``unsubscribe``. Running one recursive Observer for the process
lifetime sidesteps leak-prone lazy-start/stop logic. The memory cost is
a single FSEvents subscription (macOS) or inotify watch tree (Linux) --
acceptable for a server that's already long-lived.

**Exact-filename filtering, not glob.** We match on a small allowlist:

* ``<walnut>/_kernel/key.md``
* ``<walnut>/_kernel/log.md``
* ``<walnut>/_kernel/insights.md``
* ``<walnut>/_kernel/now.json`` (v3)
* ``<walnut>/_kernel/_generated/now.json`` (v2 fallback, routes to
  the same ``now`` URI as v3)

Everything else is dropped at the filter: ``tasks.json`` /
``completed.json`` are not resources in v0.1, ``history/chapter-*.md``
are synthesized log chapters and not resources, ``links.yaml`` /
``people.yaml`` are v0.2 overflow, and ``.alive/_mcp/**`` is the
audit-log tree (filtering it here is belt-and-braces -- T12 writes
there and would otherwise trigger resource-update storms as the log
rotates).

**500ms debounce per ``(walnut, file)`` key.** The ALIVE save protocol
rewrites ``now.json`` fully each save, and ``log.md`` gets a prepend on
nearly every save. Each such write typically emits a burst of raw
events (open, multiple modified, close, sometimes a rename dance for
atomic-write tools). The debounce coalesces that burst into a single
notification per URI per 500ms window.

Debouncing is implemented with a cancellable
:func:`asyncio.call_later` per URI -- when a new event arrives on the
same URI, the existing scheduled emission is cancelled and a fresh
500ms timer starts. The emission fires after 500ms of quiet, NOT on
the leading edge. Rationale: the leading-edge notification would fire
before the file is durable on disk (atomic writes are rename-in), and
the trailing edge coincides with the close-and-fsync point.

**Emission filter.** Even with the debounce, we only actually emit a
notification if at least one subscriber has registered for the URI.
That keeps the common "nobody is listening" case free of protocol
traffic. Subscriber tracking uses a dict ``{uri: set[session_id]}``
guarded by an :class:`asyncio.Lock`.

**list_changed semantics.** The walnut inventory changes when a new
``<walnut>/_kernel/key.md`` appears (walnut created) or a tracked one
disappears (walnut deleted or renamed). We watch CREATE and DELETE
events specifically on paths ending in ``/_kernel/key.md`` and emit
``notifications/resources/list_changed`` on those transitions, also
debounced per 500ms.

Threading model
---------------
Three execution contexts interact:

1. **Watchdog emitter thread** runs :meth:`KernelEventHandler.dispatch`.
   Pure Python, no asyncio primitives. Its only job is to classify the
   event and schedule async work on the event loop.
2. **Asyncio event loop thread** runs the debounce timers, the
   subscription registry lock, and the actual
   :meth:`ServerSession.send_resource_updated` coroutine.
3. **MCP request handlers** (subscribe / unsubscribe) run in the
   asyncio loop's request context; they mutate the registry under its
   lock.

The handshake: thread 1 calls :func:`asyncio.run_coroutine_threadsafe`
with the event loop captured at observer start. The loop thread picks
up the coroutine and runs the debounce + emit logic there.

Public API
----------
* :class:`SubscriptionRegistry` -- per-URI subscriber set + lock.
* :class:`KernelEventHandler` -- watchdog handler; bridges to asyncio.
* :func:`start_observer` -- factory that wires a new Observer to a
  world root with a given registry and session accessor.
* :func:`register_subscribe_handlers` -- install MCP
  ``resources/subscribe`` and ``resources/unsubscribe`` request handlers
  on the low-level server that mutate the registry.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Optional,
    Set,
    Tuple,
)

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from alive_mcp.uri import encode_kernel_uri

logger = logging.getLogger("alive_mcp.resources.subscriptions")


# ---------------------------------------------------------------------------
# Tunables.
# ---------------------------------------------------------------------------


#: Debounce window (seconds). Bursts of filesystem events that arrive
#: inside this window on the same ``(walnut, file)`` key are coalesced
#: into a single notification. 500ms matches the T11 spec -- short
#: enough that interactive edits feel responsive, long enough to
#: absorb a typical save protocol's open/modify/close triplet and any
#: atomic-rename dance.
DEBOUNCE_SECONDS = 0.5


#: Name of the per-walnut kernel directory. Duplicated from
#: :mod:`alive_mcp.tools.walnut` (``_KERNEL_DIRNAME``) rather than
#: imported to keep the import graph narrow -- this module already
#: pulls in watchdog, and the tools package transitively pulls in the
#: envelope + vendored walnut_paths modules we don't need here.
_KERNEL_DIRNAME = "_kernel"

#: v2 fallback "generated" subdirectory that holds ``now.json`` under
#: the pre-v3 layout. Matches :func:`walnut_tools._resolve_now_path`.
_GENERATED_DIRNAME = "_generated"


# ---------------------------------------------------------------------------
# Subscription registry.
# ---------------------------------------------------------------------------


@dataclass
class SubscriptionRegistry:
    """Per-URI subscriber set with an async lock.

    The registry maps a resource URI (string form, exactly as a client
    sent it on ``resources/subscribe``) to the set of session identities
    that have subscribed. "Session identity" is intentionally loose --
    v0.1 stdio transport has exactly one session per server process, so
    the tracked set is always either empty or a singleton. The shape is
    forward-compat: once the SDK exposes a real disconnect hook, the
    identity doubles as the key the hook passes for bulk removal.

    The lock is an :class:`asyncio.Lock` because every mutation happens
    on the event loop: MCP request handlers (subscribe / unsubscribe)
    run in request context, and the emission path acquires it via a
    loop-scheduled coroutine. The watchdog emitter thread NEVER touches
    the registry directly -- it schedules work onto the loop and lets
    the loop serialize.

    The ``has_subscribers`` helper uses an instance lock-free read;
    the set's length check is atomic in CPython (GIL-protected), and
    the emission path re-checks after taking the lock so a race on
    "did someone just unsubscribe?" collapses into a harmless no-op
    emission.
    """

    #: URI -> set of subscriber identity tokens. Empty sets are removed
    #: on unsubscribe so ``has_subscribers`` returns False quickly.
    _subscribers: Dict[str, Set[Any]] = field(default_factory=dict)

    #: Async lock serializing mutations. Created lazily on first async
    #: operation so tests can construct a registry without an event
    #: loop.
    _lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        """Return the async lock, creating it on first use.

        Creating the lock lazily (rather than in ``__init__``) avoids
        the :class:`DeprecationWarning` CPython raises when
        :class:`asyncio.Lock` is constructed without a running loop.
        The lazy form binds the lock to whichever loop is running at
        first use -- which is always the server's loop because the
        registry is only touched from within that loop.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def subscribe(self, uri: str, session_token: Any) -> None:
        """Add ``session_token`` to the subscriber set for ``uri``.

        The token is stored by identity (``set`` uses hash + equality);
        anything hashable works. In v0.1 we pass the session object
        itself -- stdio has exactly one session per run, so set
        membership is trivial. Subscribing twice from the same session
        is idempotent.
        """
        async with self._get_lock():
            self._subscribers.setdefault(uri, set()).add(session_token)

    async def unsubscribe(self, uri: str, session_token: Any) -> None:
        """Remove ``session_token`` from the subscriber set for ``uri``.

        No-op if the token was not subscribed (silent idempotent drop).
        Cleans up the empty set so the URI key disappears once nobody
        is listening -- keeps ``has_subscribers`` fast.
        """
        async with self._get_lock():
            existing = self._subscribers.get(uri)
            if existing is None:
                return
            existing.discard(session_token)
            if not existing:
                del self._subscribers[uri]

    async def has_subscribers(self, uri: str) -> bool:
        """Return True iff at least one session is subscribed to ``uri``.

        Checked from the emission path BEFORE calling ``send_resource_
        updated`` so a "nobody is listening" event is dropped with zero
        protocol traffic. The check acquires the lock; the emission
        coroutine then runs outside the lock because the SDK's
        ``send_notification`` is itself async and may await the
        transport.
        """
        async with self._get_lock():
            return uri in self._subscribers and bool(self._subscribers[uri])

    async def any_subscribers(self) -> bool:
        """Return True iff ANY URI has at least one subscriber.

        Used by the list-changed emission path. ``list_changed`` is a
        bare notification (no URI), so we don't need per-URI filtering
        -- we just need to know "is there any listener to serve?"
        """
        async with self._get_lock():
            return any(bool(s) for s in self._subscribers.values())


# ---------------------------------------------------------------------------
# Watchdog event handler.
# ---------------------------------------------------------------------------


#: Type of the async callable that fires a per-URI updated notification.
#: Takes the URI; returns an awaitable. Invoked on the event loop.
NotifyUpdatedFn = Callable[[str], Awaitable[None]]

#: Type of the async callable that fires a list-changed notification.
NotifyListChangedFn = Callable[[], Awaitable[None]]


@dataclass
class _FileClassification:
    """Result of classifying a raw filesystem path against the kernel matrix.

    One of three shapes:

    * ``kind == "kernel"``, ``walnut_path`` and ``file`` set -- the
      path maps to an ``alive://`` URI that should be debounced and
      emitted as ``resources/updated`` if any subscribers exist.
    * ``kind == "inventory"``, ``walnut_path`` set -- the path is a
      ``_kernel/key.md`` whose creation or deletion changes walnut
      inventory; should trigger a debounced list-changed emission.
    * ``kind == "ignored"`` -- neither of the above; drop.
    """

    kind: str
    walnut_path: Optional[str] = None
    file: Optional[str] = None


def classify_path(world_root: str, abs_path: str) -> _FileClassification:
    """Classify a filesystem path into kernel-file / inventory / ignored.

    Pure function -- no filesystem access, no side effects. The caller
    passes the resolved world root (single realpath at observer start
    time) and the raw event path from watchdog. This module's correctness
    depends on the event path being ABSOLUTE; watchdog events carry
    absolute paths on every supported platform when the watch was
    scheduled with an absolute root, which is how :func:`start_observer`
    schedules the watch.

    Kernel-file shape:

    * ``<world>/<walnut_path>/_kernel/key.md``   -> file=``key``
    * ``<world>/<walnut_path>/_kernel/log.md``   -> file=``log``
    * ``<world>/<walnut_path>/_kernel/insights.md`` -> file=``insights``
    * ``<world>/<walnut_path>/_kernel/now.json`` -> file=``now`` (v3)
    * ``<world>/<walnut_path>/_kernel/_generated/now.json`` -> file=``now`` (v2)

    Any ``_kernel/tasks.json``, ``_kernel/completed.json``,
    ``_kernel/history/*``, ``_kernel/links.yaml``, ``_kernel/people.yaml``
    -- ignored. Anything under ``.alive/_mcp/`` -- ignored. Non-
    ``_kernel`` paths -- ignored.

    ``walnut_path`` is returned as a POSIX-style relpath (forward slashes
    on all platforms) so it can feed :func:`encode_kernel_uri` directly.
    """
    # Normalize both paths to an absolute form so ``relpath`` produces
    # the POSIX-shape walnut path we want. ``os.path.normpath`` collapses
    # "." and repeated separators; we don't call realpath here because
    # the watchdog event already carries a resolved path for the tree
    # we scheduled, and calling realpath would require another syscall
    # on every event (hot path).
    try:
        rel = os.path.relpath(abs_path, world_root)
    except ValueError:
        # ``relpath`` raises on Windows when the two paths are on
        # different drives. If that happens something is broken
        # upstream (scheduler pointed at a root we don't own);
        # ignore the event rather than crash the handler thread.
        return _FileClassification(kind="ignored")

    # Split on the platform separator THEN normalize to forward slash
    # segments for the rest of the logic. Using ``os.sep`` keeps the
    # split correct on Windows (``\\``), and the subsequent join uses
    # forward slashes so the emitted URI shape is POSIX regardless of
    # host platform.
    parts = rel.replace(os.sep, "/").split("/")

    # Guard against ``..`` climbing out of the world -- relpath produces
    # leading ``..`` segments when abs_path is not under world_root.
    # This happens during race conditions around delete events where
    # watchdog emits a path that has already been moved; treat as ignored.
    if not parts or parts[0] == "..":
        return _FileClassification(kind="ignored")

    # Filter out the audit-log tree early. ``.alive/_mcp/**`` is where
    # T12's audit writer lands; filtering here prevents the log's own
    # rotation from triggering a resource-update loop.
    if parts[0] == ".alive":
        return _FileClassification(kind="ignored")

    # Find the ``_kernel`` segment. Walnut paths can be multi-segment
    # (``02_Life/people/ben-flint``), so the kernel dir can be at any
    # depth >= 1. If absent, the event is outside any walnut's kernel.
    try:
        kernel_idx = parts.index(_KERNEL_DIRNAME)
    except ValueError:
        return _FileClassification(kind="ignored")

    # Everything before ``_kernel`` is the walnut path; everything after
    # is the in-kernel subpath.
    walnut_segments = parts[:kernel_idx]
    if not walnut_segments:
        # ``_kernel`` directly at world root has no walnut owner; ignore.
        return _FileClassification(kind="ignored")
    walnut_path = "/".join(walnut_segments)

    in_kernel = parts[kernel_idx + 1 :]
    if not in_kernel:
        # Event on the ``_kernel`` directory itself (directory modify).
        # Not actionable -- ignore.
        return _FileClassification(kind="ignored")

    # v3 kernel files are flat under ``_kernel/``. Multi-segment paths
    # under ``_kernel/`` are either the v2 generated layout
    # (``_generated/now.json``) or files we explicitly exclude
    # (``history/chapter-*.md``).
    if len(in_kernel) == 1:
        filename = in_kernel[0]
        if filename == "key.md":
            return _FileClassification(
                kind="kernel", walnut_path=walnut_path, file="key"
            )
        if filename == "log.md":
            return _FileClassification(
                kind="kernel", walnut_path=walnut_path, file="log"
            )
        if filename == "insights.md":
            return _FileClassification(
                kind="kernel", walnut_path=walnut_path, file="insights"
            )
        if filename == "now.json":
            return _FileClassification(
                kind="kernel", walnut_path=walnut_path, file="now"
            )
        # ``tasks.json``, ``completed.json``, ``links.yaml``,
        # ``people.yaml`` -- all explicitly excluded from v0.1
        # resource surface. ``history`` is a directory, not a file, so
        # it never reaches this branch anyway.
        return _FileClassification(kind="ignored")

    # v2 fallback: ``_generated/now.json`` inside the kernel.
    if len(in_kernel) == 2 and in_kernel == [_GENERATED_DIRNAME, "now.json"]:
        return _FileClassification(
            kind="kernel", walnut_path=walnut_path, file="now"
        )

    # Everything else under ``_kernel/`` (``history/chapter-*.md``,
    # unknown subdirs) is ignored.
    return _FileClassification(kind="ignored")


def _classify_for_inventory(
    world_root: str, abs_path: str
) -> Optional[str]:
    """Return the walnut path if ``abs_path`` is a ``_kernel/key.md`` file.

    Separate from :func:`classify_path` because list-changed cares only
    about key.md create/delete events, not modifications. A key.md
    MODIFY does not change inventory (walnut already exists); treating
    it uniformly with CREATE would cause list-changed storms on every
    save. Returning the walnut path lets the debounce layer key the
    pending emission by ``"__listchanged__:<walnut>"`` so concurrent
    creates of different walnuts each debounce independently.
    """
    cls = classify_path(world_root, abs_path)
    if cls.kind == "kernel" and cls.file == "key":
        return cls.walnut_path
    return None


class KernelEventHandler(FileSystemEventHandler):
    """Watchdog handler that bridges FS events to MCP notifications.

    Responsibilities:

    1. Classify each raw filesystem event against the kernel-file
       allowlist (:func:`classify_path`).
    2. For kernel updates: marshal onto the asyncio loop, debounce per
       ``(walnut, file)`` key, and fire ``resources/updated`` iff a
       subscriber exists.
    3. For walnut inventory changes (key.md create / delete): marshal
       onto the loop, debounce per walnut path, and fire
       ``resources/list_changed`` iff any subscriber exists.

    The handler lives on the watchdog emitter thread; every reach into
    asyncio state goes through :func:`asyncio.run_coroutine_threadsafe`
    with the loop captured at observer start.

    The debounce state lives on the asyncio loop side -- a dict of
    pending :class:`asyncio.TimerHandle` objects keyed by URI (for
    per-resource updates) or a sentinel key (for list-changed). Both
    the dict and the handle mutations happen inside a coroutine that
    only runs on the loop, so no cross-thread locking is needed for
    the timers themselves.
    """

    def __init__(
        self,
        world_root: str,
        loop: asyncio.AbstractEventLoop,
        registry: SubscriptionRegistry,
        notify_updated: NotifyUpdatedFn,
        notify_list_changed: NotifyListChangedFn,
        *,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        self._world_root = world_root
        self._loop = loop
        self._registry = registry
        self._notify_updated = notify_updated
        self._notify_list_changed = notify_list_changed
        self._debounce_seconds = debounce_seconds

        # Debounce timers live on the loop. We mutate this dict only
        # from the loop thread (inside ``_on_event_async``), so no
        # lock is needed -- the GIL + single-thread invariant is
        # sufficient. A dict check-then-cancel-then-reschedule is
        # race-free under that invariant.
        self._pending_timers: Dict[str, asyncio.TimerHandle] = {}

        # Sentinel URI used as a dict key for the list-changed
        # debounce. Any string that can't collide with a real
        # ``alive://`` URI is fine; using a ``\x00``-prefixed string
        # makes the collision risk plainly impossible.
        self._LIST_CHANGED_KEY = "\x00list_changed"

    # ------------------------------------------------------------------
    # Watchdog dispatch surface. Runs on the emitter thread.
    # ------------------------------------------------------------------

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Receive one raw filesystem event from the watchdog emitter.

        The method is called on the watchdog emitter thread; we MUST
        NOT await anything here and MUST NOT touch the asyncio loop
        directly. The only work done on this thread is the cheap
        classification + a :func:`run_coroutine_threadsafe` handoff.

        Directory events are ignored -- all the files we care about
        are files, and directory events (mkdir, rmdir) would create
        noise on every save protocol that touches ``_kernel/``
        subdirectories.
        """
        if event.is_directory:
            return

        # ``src_path`` is bytes on some watchdog builds (depending on
        # how the root was scheduled). Watchdog promises to match the
        # input encoding -- we schedule with a str, so ``src_path``
        # is a str here. Defend anyway: a bytes path is converted via
        # the filesystem encoding so we don't silently drop events.
        src_path = event.src_path
        if isinstance(src_path, (bytes, bytearray)):
            try:
                src_path = os.fsdecode(src_path)
            except (UnicodeDecodeError, ValueError):
                return

        event_type = event.event_type

        # --- Per-kernel-file updated notification path ---------------
        cls = classify_path(self._world_root, src_path)
        if cls.kind == "kernel" and cls.walnut_path and cls.file:
            uri = encode_kernel_uri(cls.walnut_path, cls.file)
            self._schedule_updated(uri)

        # --- Walnut-inventory list_changed path ----------------------
        # Only CREATE and DELETE of ``_kernel/key.md`` change the
        # inventory. MODIFY / MOVED src -> dest are handled below for
        # the dest path (a renamed walnut reappears under a new
        # walnut_path).
        if event_type in ("created", "deleted"):
            walnut = _classify_for_inventory(self._world_root, src_path)
            if walnut is not None:
                self._schedule_list_changed()

        # A rename of ``_kernel/key.md`` shows up as a moved event.
        # Both endpoints matter: the source path's inventory entry is
        # gone, the dest path's is new. Watchdog emits the event with
        # the OLD path as ``src_path`` and the new path as
        # ``dest_path``; check both.
        if event_type == "moved":
            dest_path = getattr(event, "dest_path", "")
            if isinstance(dest_path, (bytes, bytearray)):
                try:
                    dest_path = os.fsdecode(dest_path)
                except (UnicodeDecodeError, ValueError):
                    dest_path = ""
            moved_src = _classify_for_inventory(self._world_root, src_path)
            moved_dst = (
                _classify_for_inventory(self._world_root, dest_path)
                if dest_path
                else None
            )
            if moved_src is not None or moved_dst is not None:
                self._schedule_list_changed()
            # If the move affected a kernel file (log.md rename during
            # a save), also schedule an updated notification for the
            # destination URI so the client re-reads content. Watchdog
            # already fires a ``modified`` / ``created`` for atomic
            # save targets on most platforms; this is belt-and-braces.
            if dest_path:
                dst_cls = classify_path(self._world_root, dest_path)
                if (
                    dst_cls.kind == "kernel"
                    and dst_cls.walnut_path
                    and dst_cls.file
                ):
                    uri = encode_kernel_uri(
                        dst_cls.walnut_path, dst_cls.file
                    )
                    self._schedule_updated(uri)

    # ------------------------------------------------------------------
    # Bridge helpers. Still on the emitter thread.
    # ------------------------------------------------------------------

    def _schedule_updated(self, uri: str) -> None:
        """Hand off a URI to the loop for debounced ``resources/updated``.

        Uses :func:`asyncio.run_coroutine_threadsafe` -- the only safe
        way to schedule a coroutine on a loop running in another
        thread. The returned :class:`concurrent.futures.Future` is
        discarded; failures inside the coroutine are logged by the
        coroutine itself.

        ``run_coroutine_threadsafe`` raises :class:`RuntimeError` if
        the loop has been closed (server shutting down). Catch and
        log -- late events during shutdown must not crash the
        observer thread.
        """
        try:
            asyncio.run_coroutine_threadsafe(
                self._on_update_event_async(uri),
                self._loop,
            )
        except RuntimeError:
            # Loop closed -- server is shutting down. Drop the event.
            logger.debug(
                "event for %r arrived after loop close; dropped", uri
            )

    def _schedule_list_changed(self) -> None:
        """Hand off a list-changed hint to the loop for debounced emission."""
        try:
            asyncio.run_coroutine_threadsafe(
                self._on_list_changed_event_async(),
                self._loop,
            )
        except RuntimeError:
            logger.debug("list-changed event after loop close; dropped")

    # ------------------------------------------------------------------
    # Loop-side coroutines. Run on the asyncio event loop.
    # ------------------------------------------------------------------

    async def _on_update_event_async(self, uri: str) -> None:
        """Debounce an incoming updated event, firing after quiet period.

        Cancels any previously-scheduled timer for this URI and schedules
        a new one :data:`DEBOUNCE_SECONDS` into the future. If quiet
        persists, the timer fires and invokes the emission coroutine.
        If another event arrives before the window closes, the timer is
        rescheduled -- so a burst of 10 writes to the same file within
        500ms produces exactly one notification.

        Runs on the asyncio loop thread, so mutating ``_pending_timers``
        is race-free (single-thread invariant). Even though this
        coroutine is awaitable, there is NO await before the
        dict-mutation, so we don't yield control inside the critical
        section.
        """
        existing = self._pending_timers.get(uri)
        if existing is not None and not existing.cancelled():
            existing.cancel()

        def _fire() -> None:
            # The timer fires on the loop thread. Schedule the actual
            # emission as a task so this callback returns quickly and
            # the loop isn't blocked by the await chain inside the
            # emit coroutine (which may await the transport).
            self._pending_timers.pop(uri, None)
            task = self._loop.create_task(self._emit_updated(uri))
            # Attach a done-callback so an uncaught exception inside
            # the emit path is logged, not silently swallowed by the
            # loop's default "unretrieved task exception" warning.
            task.add_done_callback(_log_task_exception)

        handle = self._loop.call_later(self._debounce_seconds, _fire)
        self._pending_timers[uri] = handle

    async def _on_list_changed_event_async(self) -> None:
        """Debounce an incoming list-changed hint.

        Same debounce mechanics as :meth:`_on_update_event_async` but
        keyed by the list-changed sentinel so multiple walnut-inventory
        transitions within 500ms collapse into a single
        ``notifications/resources/list_changed``.
        """
        key = self._LIST_CHANGED_KEY
        existing = self._pending_timers.get(key)
        if existing is not None and not existing.cancelled():
            existing.cancel()

        def _fire() -> None:
            self._pending_timers.pop(key, None)
            task = self._loop.create_task(self._emit_list_changed())
            task.add_done_callback(_log_task_exception)

        handle = self._loop.call_later(self._debounce_seconds, _fire)
        self._pending_timers[key] = handle

    async def _emit_updated(self, uri: str) -> None:
        """Fire a ``resources/updated`` notification if subscribers exist.

        The subscriber check runs under the registry lock; the actual
        ``send_resource_updated`` call runs OUTSIDE the lock because
        the SDK's send path awaits the transport. Holding the registry
        lock across an arbitrarily-long transport await would block
        unrelated subscribe / unsubscribe handlers.
        """
        if not await self._registry.has_subscribers(uri):
            logger.debug(
                "resource %r changed but has no subscribers; dropped", uri
            )
            return
        try:
            await self._notify_updated(uri)
        except Exception:  # noqa: BLE001 -- logged, not raised.
            # Transport failure: the session is probably torn down or
            # the client disconnected. Log at warning and carry on --
            # the observer outlives individual sessions.
            logger.warning(
                "failed to send resources/updated for %r", uri,
                exc_info=True,
            )

    async def _emit_list_changed(self) -> None:
        """Fire a ``resources/list_changed`` notification if any subs exist."""
        if not await self._registry.any_subscribers():
            logger.debug(
                "walnut inventory changed but no subscribers; dropped"
            )
            return
        try:
            await self._notify_list_changed()
        except Exception:  # noqa: BLE001
            logger.warning(
                "failed to send resources/list_changed", exc_info=True
            )

    # ------------------------------------------------------------------
    # Lifecycle.
    # ------------------------------------------------------------------

    def cancel_pending(self) -> None:
        """Cancel every pending debounce timer.

        Called from the lifespan shutdown path so timers scheduled at
        t=T-100ms don't fire post-shutdown (and try to reach into a
        torn-down session). Safe to call multiple times. Must run on
        the loop thread.
        """
        for handle in list(self._pending_timers.values()):
            if not handle.cancelled():
                handle.cancel()
        self._pending_timers.clear()


def _log_task_exception(task: "asyncio.Task[Any]") -> None:
    """Log uncaught exceptions from debounce-fire tasks.

    The loop's default ``task_exception`` logging is terse and prints
    only when the task is garbage-collected without its exception
    having been retrieved. Attaching this done-callback gives us a
    consistent log line at task-completion time regardless of GC
    timing.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    logger.warning(
        "subscription emit task raised %s", exc.__class__.__name__,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


# ---------------------------------------------------------------------------
# Observer bootstrap.
# ---------------------------------------------------------------------------


def start_observer(
    world_root: str,
    loop: asyncio.AbstractEventLoop,
    registry: SubscriptionRegistry,
    notify_updated: NotifyUpdatedFn,
    notify_list_changed: NotifyListChangedFn,
    *,
    debounce_seconds: float = DEBOUNCE_SECONDS,
) -> Tuple[BaseObserver, KernelEventHandler]:
    """Create, schedule, and start a watchdog Observer on ``world_root``.

    Returns the ``(observer, handler)`` pair. Callers hold onto the
    observer for the lifetime of the server and call ``observer.stop()``
    + ``observer.join()`` at shutdown; the handler is retained so
    :meth:`KernelEventHandler.cancel_pending` can be called before the
    event loop closes.

    The watch is recursive across the entire World root. Watchdog's
    :class:`ObservedWatch` supports per-event-type filtering, but we
    DON'T use it here -- the filename-level filter in
    :func:`classify_path` is the single source of truth for
    "interesting event" classification, and keeping filtering in one
    place makes the logic reviewable.

    ``daemon=True`` on the observer thread is load-bearing: without it,
    a runtime crash or lifespan bug that skipped ``observer.stop()``
    would leave the thread alive preventing interpreter exit. With
    daemon mode, Python tears the thread down on process exit
    regardless.

    Rationale for recursive vs per-walnut: at the v0.1 scale target (
    Patrick's ~43-walnut world), a single recursive watch is cheaper
    than 43 independent watches (one FSEvents subscription vs 43, one
    inotify watch tree vs 43 -- and inotify in particular has a
    fs.inotify.max_user_watches limit on Linux that a per-walnut
    approach would eat into). The recursive watch is also cheaper to
    maintain: walnut creation or deletion doesn't require us to
    re-schedule anything.
    """
    handler = KernelEventHandler(
        world_root=world_root,
        loop=loop,
        registry=registry,
        notify_updated=notify_updated,
        notify_list_changed=notify_list_changed,
        debounce_seconds=debounce_seconds,
    )
    observer: BaseObserver = Observer()
    # ``daemon=True`` on the thread itself; cheap insurance against a
    # shutdown path that skips ``.stop()``.
    observer.daemon = True
    observer.schedule(handler, world_root, recursive=True)
    observer.start()
    logger.info(
        "started watchdog observer on %r (debounce=%.0fms)",
        world_root,
        debounce_seconds * 1000,
    )
    return observer, handler


# ---------------------------------------------------------------------------
# MCP subscribe / unsubscribe handlers.
# ---------------------------------------------------------------------------


def register_subscribe_handlers(
    server: FastMCP[Any],
    registry: SubscriptionRegistry,
    session_getter: Callable[[], Any],
) -> None:
    """Install MCP ``resources/subscribe`` and ``unsubscribe`` handlers.

    The handlers mutate ``registry`` under its async lock. The
    ``session_getter`` returns the active :class:`ServerSession` (or
    None if no session is bound); we use its ``id()`` as the subscriber
    token so the same session subscribing twice is idempotent, and
    different sessions are disambiguated even when the SDK shape
    changes across versions.

    Why install both handlers even though v0.1 drops lazy-on-subscribe:
    the registry IS the emission filter. A client that never subscribes
    gets no ``updated`` traffic; a client that subscribes gets traffic
    until it explicitly unsubscribes OR its session tears down.
    Without the subscribe handler, every update would be dropped by
    ``has_subscribers(uri) == False`` -- functionally correct but
    protocol-incorrect (the client thinks it subscribed).

    Low-level registration via ``server._mcp_server.request_handlers``
    mirrors the T10 kernel-resource handler installation. FastMCP's
    decorator (``@server._mcp_server.subscribe_resource``) would also
    work but requires a slightly different callback shape; using the
    dict directly keeps the registration style consistent across T10
    and T11.
    """

    async def _subscribe(req: mcp_types.SubscribeRequest) -> mcp_types.ServerResult:
        uri = str(req.params.uri)
        session = session_getter()
        token = id(session) if session is not None else "anonymous"
        await registry.subscribe(uri, token)
        logger.debug("subscribed session=%r to uri=%r", token, uri)
        return mcp_types.ServerResult(mcp_types.EmptyResult())

    async def _unsubscribe(req: mcp_types.UnsubscribeRequest) -> mcp_types.ServerResult:
        uri = str(req.params.uri)
        session = session_getter()
        token = id(session) if session is not None else "anonymous"
        await registry.unsubscribe(uri, token)
        logger.debug("unsubscribed session=%r from uri=%r", token, uri)
        return mcp_types.ServerResult(mcp_types.EmptyResult())

    server._mcp_server.request_handlers[mcp_types.SubscribeRequest] = _subscribe
    server._mcp_server.request_handlers[mcp_types.UnsubscribeRequest] = _unsubscribe


__all__ = [
    "DEBOUNCE_SECONDS",
    "KernelEventHandler",
    "NotifyListChangedFn",
    "NotifyUpdatedFn",
    "SubscriptionRegistry",
    "classify_path",
    "register_subscribe_handlers",
    "start_observer",
]
