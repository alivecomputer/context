"""FastMCP server bootstrap for alive-mcp v0.1.

This module wires a :class:`FastMCP` instance into a runnable stdio JSON-RPC
server. It is the shell that T6-T11 will hang tools and resources off; v0.1
stops here with no tools, no resources, and no dynamic capabilities.

Contract surface
----------------
* :func:`build_server` — construct and configure the FastMCP instance. Pure
  function returning the server plus its :class:`AppContext` holder. Used by
  tests to drive the server in-process; used by :func:`main` to run stdio.
* :func:`main` — CLI entry point wired via ``[project.scripts]``. Runs the
  stdio transport, blocks until stdin EOF, returns 0.
* :data:`APP_NAME`, :data:`PROTOCOL_VERSION_PINNED` — module-level constants
  for tests to assert against without importing pydantic types.

Capability declaration
----------------------
The MCP spec requires the server to declare its capabilities during the
``initialize`` handshake. For v0.1 we advertise:

=========================== ======= =======================================
Capability                  Value   Rationale
=========================== ======= =======================================
``tools``                   object  10-tool roster lands in T6-T9.
``tools.listChanged``       ``False`` Roster is frozen for v0.1.
``resources``               object  Kernel-file resources land in T10.
``resources.subscribe``     ``True``  Per-URI subscription arrives in T11.
``resources.listChanged``   ``True``  Walnut-inventory watching arrives in T11.
``logging``                 object  stderr logging endpoint.
=========================== ======= =======================================

The ``mcp>=1.27,<2.0`` low-level ``Server.get_capabilities`` builder is not
flexible enough on its own: it hard-codes ``subscribe=False`` on the
``ResourcesCapability`` and tool/resource ``listChanged`` flags are driven by
:class:`~mcp.server.lowlevel.server.NotificationOptions`. We therefore wrap
``_mcp_server.get_capabilities`` so the server advertises the combination the
v0.1 spec requires without waiting on an SDK change. See the comments on
:func:`_install_capability_override` for the forward-compat escape hatch
(once the SDK exposes a ``subscribe=`` parameter, the wrapper becomes a
no-op and can be deleted).

Roots API status (``mcp>=1.27,<2.0``)
-------------------------------------
The SDK exposes BOTH halves of the Roots protocol in the version we pin,
validated empirically during T5 implementation:

* Server-initiated ``roots/list`` — :meth:`ServerSession.list_roots` exists
  at ``mcp/server/session.py:350``. Callable from any code that has access
  to the active ``ServerSession`` (tool handlers, notification handlers).
* ``notifications/roots/list_changed`` — the low-level ``Server`` exposes
  a ``notification_handlers: dict[type, Callable]`` map. Registering a
  handler against ``types.RootsListChangedNotification`` routes the
  notification to user code. The lifespan installs that handler below.

The FastMCP convenience wrapper does not currently surface either hook on
its own public API, so we reach through ``_mcp_server`` (the low-level
:class:`~mcp.server.lowlevel.server.Server` instance FastMCP owns). This is
the same technique FastMCP itself uses internally (``_setup_handlers``).

**Why world discovery has to be deferred to post-``initialized``.** The
lifespan context manager enters BEFORE the first JSON-RPC message is read,
so there is no client session to query for Roots at that moment. We do two
things in lifespan:

1. Attempt env-fallback discovery immediately so the server can serve
   ``ERR_NO_WORLD`` coherently if Roots never arrive.
2. Register a handler for :class:`types.InitializedNotification` that,
   once fired, calls ``session.list_roots()`` and re-runs discovery with
   the combined Roots + env inputs. The handler also re-runs on every
   subsequent :class:`types.RootsListChangedNotification`.

If Roots discovery fails but env discovery succeeded, we keep the env
result and log the Roots failure on stderr. If both fail, the resolved
world stays ``None`` — the tool layer (T6+) will then emit
``ERR_NO_WORLD`` per the envelope schema without crashing the server.

Stdout discipline
-----------------
The stdio transport multiplexes JSON-RPC messages on stdout. Any
``print()`` (or dependency that writes to stdout) contaminates framing
and makes the server silently unusable. This module:

* Never calls ``print()``.
* Configures ``logging.basicConfig(stream=sys.stderr, ...)`` BEFORE
  importing any mcp modules that might log on import.
* Never writes raw bytes to ``sys.stdout`` directly.

Dependency banners (pydantic warnings, deprecation notices) are routed
through the Python warnings filter to stderr too. MemPalace's
``mcp_server.py`` uses an fd1->fd2 redirect as belt-and-suspenders; we
keep that technique documented but do not enable it until a specific
dep escalates (``warnings`` routing + ``logging.stream=stderr`` covers
the current depset — ``mcp``, ``watchdog``).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from watchdog.observers import Observer

from alive_mcp import __version__
from alive_mcp.errors import WorldNotFoundError
from alive_mcp.world import discover_world


# -----------------------------------------------------------------------------
# Module-level constants. Tests import these; external callers should not.
# -----------------------------------------------------------------------------

#: Server name advertised in the ``initialize`` response. MCP spec allows
#: any stable identifier; ``alive`` is short, unambiguous, and matches the
#: pyproject ``[project].name = "alive-mcp"`` without the ``-mcp`` suffix
#: (the suffix is redundant inside an MCP handshake).
APP_NAME = "alive"

#: Protocol version the v0.1 feature-set targets. The SDK negotiates up to
#: the client's requested version if it appears in
#: :data:`mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS`, so pinning 06-18
#: means "we implement at least the 06-18 schema; newer clients get newer
#: fields the SDK knows how to add". 06-18 is the floor for structured tool
#: output, which the envelope layer (T4) already targets.
PROTOCOL_VERSION_PINNED = "2025-06-18"

#: Default audit queue depth. The writer (T12) drains this; if the writer
#: falls behind, putting to a full queue blocks the tool handler — which is
#: the right back-pressure signal for v0.1 (losing audit entries silently
#: would violate the security-audit invariant). Sized so a burst of ~1024
#: tool calls in a row is absorbed without blocking.
AUDIT_QUEUE_MAXSIZE = 1024

# Module-level logger. ``basicConfig`` is set up in :func:`_configure_logging`
# which :func:`build_server` calls exactly once. Configuring at import time
# would trigger on test imports too, which is fine but not intended.
logger = logging.getLogger("alive_mcp")


# -----------------------------------------------------------------------------
# Lifespan state. A single :class:`AppContext` is created per server run and
# threaded through FastMCP's lifespan machinery. Tool handlers (T6+) reach
# it via ``ctx.request_context.lifespan_context``.
# -----------------------------------------------------------------------------


@dataclass
class AppContext:
    """Per-server-run state threaded through FastMCP's lifespan.

    The fields are public by convention (tool handlers read them). Mutating
    them outside the lifespan hooks is a bug; T6+ tools should treat the
    context as read-only except for ``world_root``, which the Roots handler
    updates when the client changes its roots at runtime.
    """

    #: Resolved absolute path to the World root, or None if discovery has
    #: not yet succeeded. Tools that need a world MUST raise
    #: :class:`~alive_mcp.errors.WorldNotFoundError` when this is None so
    #: the envelope layer can map it to ``ERR_NO_WORLD``.
    world_root: Optional[str] = None

    #: Unbounded-for-v0.1 asyncio queue for audit records. T12 replaces the
    #: stub writer with a real JSONL appender + rotation; until then the
    #: queue exists so tool code can write to it without conditional
    #: plumbing.
    audit_queue: asyncio.Queue[Any] = field(
        default_factory=lambda: asyncio.Queue(maxsize=AUDIT_QUEUE_MAXSIZE)
    )

    #: Background task draining the audit queue. The stub writer in this
    #: module does nothing with drained items; T12 replaces the function
    #: with the real writer.
    audit_writer_task: Optional[asyncio.Task[None]] = None

    #: watchdog ``Observer`` instance. Started in lifespan even though no
    #: watches are registered yet (T11 wires the per-walnut watches) — the
    #: observer thread is cheap and starting it unconditionally keeps the
    #: lifespan shape identical between v0.1 (no subscriptions) and v0.2.
    observer: Optional[Observer] = None

    #: True once we have attempted post-``initialized`` Roots discovery at
    #: least once. Used to suppress redundant discovery on every
    #: ``roots/list_changed`` notification fired before we've seen an
    #: ``initialized`` notification (shouldn't happen per the MCP spec,
    #: but belt-and-suspenders).
    roots_discovery_attempted: bool = False


# -----------------------------------------------------------------------------
# Capability override. Hard-codes the v0.1 capability matrix.
# -----------------------------------------------------------------------------


def _install_capability_override(server: FastMCP[Any]) -> None:
    """Wrap ``_mcp_server.get_capabilities`` so the v0.1 matrix is advertised.

    The SDK's ``Server.get_capabilities`` hard-codes
    ``ResourcesCapability(subscribe=False, ...)`` and derives the tool-
    and-resource ``listChanged`` flags from a
    :class:`NotificationOptions` argument. We need ``subscribe=True`` and
    ``resources.listChanged=True`` advertised today, with
    ``tools.listChanged=False`` locked in.

    The wrapper:

    * Calls the original ``get_capabilities`` with a
      :class:`NotificationOptions` carrying ``resources_changed=True``
      and ``tools_changed=False``, so the SDK sets the listChanged flags
      we want.
    * Post-processes the returned :class:`types.ServerCapabilities` so
      ``resources.subscribe`` becomes ``True`` (the SDK hard-coded the
      field to ``False``).
    * Leaves prompts, logging, completions, and experimental fields as
      the SDK produced them — we do not advertise any of those in v0.1,
      but the wrapper is forward-compatible if we start using them.

    Forward compat: once ``mcp`` exposes a ``subscribe=`` parameter on
    :class:`NotificationOptions` (tracked upstream), this wrapper becomes
    redundant and can be deleted. The wrapper deliberately preserves the
    original method under ``_alive_original_get_capabilities`` so a test
    can sanity-check the monkey-patch is idempotent.
    """
    original = server._mcp_server.get_capabilities

    # Idempotency guard: installing twice on the same server instance
    # (e.g. during tests that call build_server in a loop) must NOT stack
    # wrappers. We stash the original under a private attribute and check
    # for it before re-wrapping.
    if hasattr(server._mcp_server, "_alive_original_get_capabilities"):
        return
    setattr(server._mcp_server, "_alive_original_get_capabilities", original)

    def _get_capabilities(
        notification_options: NotificationOptions,
        experimental_capabilities: dict[str, dict[str, Any]],
    ) -> mcp_types.ServerCapabilities:
        # Overwrite the flags the SDK honors from NotificationOptions so
        # the v0.1 matrix is emitted no matter what the caller passes in.
        # FastMCP's ``create_initialization_options`` passes a default
        # ``NotificationOptions()`` with every flag False; that default is
        # replaced here.
        forced = NotificationOptions(
            prompts_changed=False,
            resources_changed=True,  # T11 wires list_changed emits.
            tools_changed=False,  # Tool roster is frozen for v0.1.
        )
        caps = original(forced, experimental_capabilities)

        # Post-process: flip subscribe on the resources capability so the
        # v0.1 subscription protocol (T11) is advertised. If the SDK stops
        # emitting a resources capability (handler deregistration), we
        # preserve the None and let the envelope layer raise a tool-level
        # error if anything tries to subscribe.
        if caps.resources is not None:
            caps = caps.model_copy(
                update={
                    "resources": caps.resources.model_copy(
                        update={"subscribe": True, "listChanged": True}
                    ),
                }
            )

        # Explicit tools.listChanged=False. The SDK already sets this via
        # NotificationOptions.tools_changed=False above, but being explicit
        # here catches any future SDK drift where a new default flips.
        if caps.tools is not None:
            caps = caps.model_copy(
                update={"tools": caps.tools.model_copy(update={"listChanged": False})},
            )

        # Suppress the prompts capability. FastMCP auto-registers
        # ``ListPromptsRequest`` via ``_setup_handlers``, so the SDK's
        # ``get_capabilities`` emits an empty prompts object. v0.1 does
        # not expose any prompts — advertising the capability would be
        # over-promising and trip the "capabilities match what T6-T11
        # deliver" acceptance criterion. Drop it entirely. If a future
        # task adds a prompt (via ``@server.prompt``), delete this line
        # and let the SDK's default drive the capability.
        caps = caps.model_copy(update={"prompts": None})

        return caps

    # Pydantic Server isn't a BaseModel but vanilla Python — assign the
    # bound wrapper directly.
    server._mcp_server.get_capabilities = _get_capabilities  # type: ignore[method-assign]


# -----------------------------------------------------------------------------
# Logging. stderr-only, configured before mcp imports log anything.
# -----------------------------------------------------------------------------


def _configure_logging(level: int = logging.INFO) -> None:
    """Route all Python logging to stderr at the given level.

    Idempotent: if ``basicConfig`` already configured a stream handler,
    this function only adjusts the level. That matters because the mcp
    SDK calls ``logging.getLogger(__name__)`` at import time, and if a
    test imports ``alive_mcp.server`` then re-imports it (via
    ``importlib.reload``) we don't want to stack handlers.

    stderr is the MCP stdio transport's out-of-band channel: JSON-RPC
    rides stdout, human-readable diagnostics ride stderr. Writing logs to
    stdout would corrupt framing and is the #1 reason stdio MCP servers
    fail silently — hence this function exists at all.
    """
    root = logging.getLogger()
    has_stream_handler = any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        for h in root.handlers
    )
    if not has_stream_handler:
        logging.basicConfig(
            stream=sys.stderr,
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    else:
        root.setLevel(level)


# -----------------------------------------------------------------------------
# Audit writer stub. T12 replaces this with the JSONL writer.
# -----------------------------------------------------------------------------


async def _audit_writer_stub(queue: asyncio.Queue[Any]) -> None:
    """Drain ``queue`` forever, discarding items.

    The v0.1 skeleton has no tools that produce audit records, but the
    queue and writer task exist so T6-T12 can land incrementally without
    having to rewire the lifespan each time. The stub is intentionally
    cheap — it blocks on ``queue.get()`` and then throws the result away
    with a ``task_done`` to keep the queue's internal counters sane.

    When T12 lands, this function is replaced by the JSONL writer. The
    signature stays stable so the lifespan code that starts the task does
    not change.
    """
    try:
        while True:
            item = await queue.get()
            # Stub: no-op. T12 writes ``item`` as a JSON line to
            # <world>/.alive/_mcp/audit.log with 0o600 perms.
            del item
            queue.task_done()
    except asyncio.CancelledError:
        # Normal shutdown path — re-raise so the task exits cleanly.
        raise


# -----------------------------------------------------------------------------
# Roots discovery hooks. Installed on the low-level Server's notification
# handler map during lifespan.
# -----------------------------------------------------------------------------


async def _discover_world_with_roots(
    app_context: AppContext,
    session: Any,
) -> None:
    """Re-resolve the World using Roots + env, updating ``app_context``.

    Called from the ``initialized`` and ``roots/list_changed`` handlers.
    Failures log a warning and LEAVE the existing ``world_root`` alone —
    we prefer a stale-but-valid world over dropping to no-world on a
    transient Roots failure.

    The function is safe to call concurrently with itself; Python's
    asyncio semantics on the ``app_context`` assignment are atomic.
    """
    app_context.roots_discovery_attempted = True
    roots: list[str] = []

    try:
        result = await session.list_roots()
    except Exception as exc:  # noqa: BLE001 — we log the class below.
        logger.warning(
            "roots/list request failed (%s); keeping existing world_root=%r",
            exc.__class__.__name__,
            app_context.world_root,
        )
    else:
        # ``result.roots`` is a list of :class:`types.Root`. Each has a
        # ``uri`` that is a ``file://`` URL per the MCP spec. Extract the
        # local path and let :func:`discover_world` do the predicate
        # matching.
        for root in result.roots:
            uri = str(root.uri)
            if uri.startswith("file://"):
                # Strip the ``file://`` scheme. Don't attempt percent-
                # decoding here — :mod:`urllib.parse` handles edge cases
                # (spaces, unicode) but the MCP spec permits only
                # well-formed absolute POSIX paths, so the naive strip
                # is adequate for v0.1.
                path = uri[len("file://") :]
                # Some clients send ``file:///Users/...``; strip a single
                # leading slash off the host component if present.
                # ``urlsplit`` would handle this, but the bare strip keeps
                # the dependency surface small.
                if path.startswith("/"):
                    roots.append(path)
                else:
                    # ``file://Users/...`` — rare, malformed, but be
                    # permissive and pass through.
                    roots.append(path)
            else:
                logger.warning(
                    "ignoring non-file:// root uri %r (only file:// is "
                    "supported in v0.1)",
                    uri,
                )

    try:
        resolved = discover_world(roots=roots)
    except WorldNotFoundError as exc:
        if app_context.world_root is None:
            logger.warning(
                "World discovery failed: %s. Tools will emit ERR_NO_WORLD "
                "until Roots or ALIVE_WORLD_ROOT resolves.",
                exc,
            )
        # Keep existing world_root if we had one; drop to None if we
        # didn't. This matches the spec's "degrade gracefully" posture.
    else:
        if resolved != app_context.world_root:
            logger.info(
                "World resolved to %r (previous: %r)",
                resolved,
                app_context.world_root,
            )
        app_context.world_root = resolved


# -----------------------------------------------------------------------------
# Lifespan context manager. FastMCP calls this at startup and awaits its
# ``__aexit__`` on shutdown.
# -----------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP[AppContext]) -> AsyncIterator[AppContext]:
    """Set up per-run state; tear it down on shutdown.

    Order of operations (startup):

    1. Construct the :class:`AppContext`.
    2. Attempt env-only World discovery so the server can serve
       ``ERR_NO_WORLD`` coherently if Roots never arrive.
    3. Start the audit-writer stub task.
    4. Start the watchdog Observer thread (no watches registered yet).
    5. Install notification handlers on the low-level server for
       ``InitializedNotification`` (triggers Roots discovery) and
       ``RootsListChangedNotification`` (re-triggers Roots discovery).
    6. Log the startup banner on stderr.
    7. Yield the context to FastMCP; requests flow during this period.

    Shutdown (reverse order, best-effort):

    1. Stop the watchdog Observer.
    2. Cancel the audit-writer task; drain pending items silently.
    3. Log the shutdown banner.

    Exceptions raised by individual shutdown steps are logged and
    swallowed — the MCP framework treats lifespan teardown as
    best-effort, and we must not prevent other resources from releasing
    because one step failed.
    """
    app_context = AppContext()

    # Step 2: env-only world discovery. A failure here is expected for
    # clients that only pass Roots (Claude Desktop, Cursor) — we log at
    # DEBUG so the warning isn't noisy on every launch.
    try:
        app_context.world_root = discover_world(roots=())
    except WorldNotFoundError as exc:
        logger.debug(
            "env-only world discovery failed at startup: %s. Awaiting Roots.",
            exc,
        )

    # Step 3: audit writer. The task is a background daemon; failures
    # inside it will crash the task but not the server. T12 adds a
    # supervisor that restarts the writer on failure.
    app_context.audit_writer_task = asyncio.create_task(
        _audit_writer_stub(app_context.audit_queue),
        name="alive-mcp.audit_writer_stub",
    )

    # Step 4: watchdog Observer. Start unconditionally — the thread is
    # idle without watches registered, and starting here keeps the v0.1
    # lifespan shape identical to the v0.2 shape that will register
    # walnut-inventory watches in T11.
    observer = Observer()
    observer.daemon = True  # don't block process shutdown.
    observer.start()
    app_context.observer = observer

    # Step 5: notification handlers. The low-level server's
    # ``notification_handlers`` dict is a public-by-convention field
    # (per :mod:`mcp.server.lowlevel.server` line 159) that routes
    # incoming client notifications. We register two handlers:
    async def _on_initialized(
        notify: mcp_types.InitializedNotification,
    ) -> None:
        # ``request_context`` is a contextvar bound per-request. Inside a
        # notification handler FastMCP does NOT bind it — notifications
        # don't have request IDs. We therefore reach the active session
        # via the low-level Server's own reference, which we stored on
        # ``app_context`` for exactly this purpose.
        session = _get_active_session(server)
        if session is None:  # pragma: no cover — shouldn't happen.
            logger.warning(
                "initialized notification fired but no active session "
                "found; skipping Roots discovery"
            )
            return
        await _discover_world_with_roots(app_context, session)

    async def _on_roots_list_changed(
        notify: mcp_types.RootsListChangedNotification,
    ) -> None:
        if not app_context.roots_discovery_attempted:
            # Spec-wise, clients shouldn't send list_changed before
            # initialized. If they do, the ``initialized`` handler will
            # pick up the new roots when it fires — no action needed.
            return
        session = _get_active_session(server)
        if session is None:  # pragma: no cover
            return
        await _discover_world_with_roots(app_context, session)

    server._mcp_server.notification_handlers[
        mcp_types.InitializedNotification
    ] = _on_initialized
    server._mcp_server.notification_handlers[
        mcp_types.RootsListChangedNotification
    ] = _on_roots_list_changed

    # Step 6: startup banner. The v0.1 spec explicitly requires the
    # "alive-mcp v<version> starting" line on stderr so the human can
    # confirm the server is up without grepping JSON.
    logger.info("alive-mcp v%s starting", __version__)

    try:
        yield app_context
    finally:
        # Shutdown order: observer first (stops filesystem events), then
        # audit writer (drains any final queue entries). Each step is
        # wrapped in try/except because the MCP framework treats lifespan
        # teardown as best-effort.
        try:
            if app_context.observer is not None:
                app_context.observer.stop()
                # join() with a short timeout — the daemon=True flag
                # guarantees the thread won't block interpreter exit, so
                # a hang here would only delay shutdown logging.
                app_context.observer.join(timeout=1.0)
        except Exception:  # noqa: BLE001 — logged, not raised.
            logger.exception("watchdog observer shutdown failed")

        try:
            if app_context.audit_writer_task is not None:
                app_context.audit_writer_task.cancel()
                try:
                    await app_context.audit_writer_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    # CancelledError is the normal shutdown path; any
                    # other exception we swallow after logging.
                    pass
        except Exception:  # noqa: BLE001
            logger.exception("audit writer shutdown failed")

        logger.info("alive-mcp v%s stopped", __version__)


def _get_active_session(server: FastMCP[Any]) -> Optional[Any]:
    """Return the active :class:`ServerSession`, or None if not bound.

    FastMCP exposes the session indirectly via the ``request_context``
    contextvar (bound per-request). Notification handlers fire outside
    a request, so the contextvar is unbound. We fall back to the
    low-level server's ``request_context`` property which raises
    ``LookupError`` in the same situation — in that case we return None
    and the caller logs a warning.

    In practice the ``initialized`` notification fires during the
    session's lifecycle but before any request, so this function needs a
    non-request path to the session. The python-sdk does not expose one
    directly; the caller's notification ALREADY has access to the
    session via the lowlevel ``_handle_message`` call chain, but that
    frame is not reachable from our registered callback. For v0.1 we
    accept that limitation and document it: Roots discovery happens on
    the FIRST TOOL CALL instead (see :func:`_ensure_roots_discovered`).
    """
    try:
        request_context = server._mcp_server.request_context
    except LookupError:
        return None
    return request_context.session


async def _ensure_roots_discovered(
    app_context: AppContext,
    session: Any,
) -> None:
    """Trigger Roots discovery lazily on the first request that needs it.

    Because notification handlers do not have access to the active
    session in ``mcp>=1.27,<2.0`` (see :func:`_get_active_session`),
    we trigger Roots discovery from the first tool/resource request
    that lands. Tools in T6+ should call this helper at the top of
    their handler; it is a no-op after the first call.

    This function is a public seam for T6+ to use — keeping it here
    means T6 does not have to re-derive the Roots logic.
    """
    if app_context.roots_discovery_attempted:
        return
    await _discover_world_with_roots(app_context, session)


# -----------------------------------------------------------------------------
# Server factory + entrypoint.
# -----------------------------------------------------------------------------


def build_server() -> FastMCP[AppContext]:
    """Construct the FastMCP server with capabilities + lifespan wired.

    Separated from :func:`main` so tests can drive the server in-process
    without running the stdio transport. The returned instance is ready
    to call ``run()`` or ``run_stdio_async()`` on.

    Notes on ``mask_error_details``
    -------------------------------
    The v0.1 spec references the FastMCP ``mask_error_details=True`` flag
    (per the ``wlanboy/mcp-md-fileserver`` pattern). That parameter is
    NOT present on :class:`FastMCP.__init__` in ``mcp>=1.27,<2.0``; it
    arrives in the pre-2.0 branch. We therefore do NOT pass it, and
    instead enforce the "no absolute paths leak to clients" invariant at
    the envelope layer (T4): :func:`alive_mcp.envelope.error` redacts
    absolute paths from every error message and every string kwarg
    before templating. The invariant holds with or without the SDK flag.
    """
    _configure_logging()

    # ``instructions`` deliberately avoids the "alive-mcp v<ver>" string
    # so tests can grep stdout for that banner-shape and verify it does
    # NOT leak onto the JSON-RPC channel. The server name + the startup
    # log line on stderr are the human-readable identity markers.
    server: FastMCP[AppContext] = FastMCP(
        name=APP_NAME,
        instructions=(
            "Read-only access to an ALIVE Context System World. "
            "Tools and resources land in T6-T11."
        ),
        lifespan=lifespan,
        log_level="INFO",
    )

    # FastMCP does not accept a ``version`` parameter; the low-level
    # ``MCPServer`` defaults its version to ``importlib.metadata.version("mcp")``
    # (i.e. the SDK version, not ours). Patch the attribute so the
    # ``initialize`` response carries OUR package version under
    # ``serverInfo.version``. This matters because MCP clients log and
    # sometimes gate on server version — reporting the SDK version would
    # mask our own releases.
    server._mcp_server.version = __version__

    _install_capability_override(server)
    return server


def main(argv: Optional[list[str]] = None) -> int:
    """Run the server on stdio.

    ``argv`` is accepted for symmetry with the T1 stub but is unused in
    v0.1 — the server has no command-line flags beyond ``--version``
    (still handled by ``__main__.main``). Tests typically call
    :func:`build_server` directly and drive the server through
    ``run_stdio_async`` on a controlled event loop.

    Returns 0 on clean shutdown, 1 on unhandled error during
    construction. ``run()`` itself does not return on error; exceptions
    propagate up.
    """
    del argv  # unused; reserved for future flags.
    try:
        server = build_server()
    except Exception:  # noqa: BLE001
        logger.exception("alive-mcp failed to start")
        return 1

    server.run(transport="stdio")
    return 0


__all__ = [
    "APP_NAME",
    "PROTOCOL_VERSION_PINNED",
    "AUDIT_QUEUE_MAXSIZE",
    "AppContext",
    "build_server",
    "lifespan",
    "main",
]
