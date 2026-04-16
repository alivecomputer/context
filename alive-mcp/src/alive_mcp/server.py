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

# Configure stderr logging BEFORE importing mcp/watchdog. Any import-time
# logging those packages emit (pydantic warnings, deprecation notices,
# etc.) would otherwise be routed to an inherited stdout handler that
# the host process may have installed — which would corrupt the
# JSON-RPC frame channel. ``force=True`` replaces any pre-existing
# handlers unconditionally. Doing this at module top-level means the
# invariant holds for EVERY importer, not just :func:`main`.
import logging  # noqa: E402 — ordering is load-bearing, see above.
import sys  # noqa: E402

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)

import asyncio  # noqa: E402
import urllib.parse  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any, AsyncIterator, Optional  # noqa: E402

from mcp import types as mcp_types  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.lowlevel.server import NotificationOptions  # noqa: E402
from watchdog.observers import Observer  # noqa: E402

from alive_mcp import __version__  # noqa: E402
from alive_mcp.errors import WorldNotFoundError  # noqa: E402
from alive_mcp.world import discover_world  # noqa: E402


# -----------------------------------------------------------------------------
# Module-level constants. Tests import these; external callers should not.
# -----------------------------------------------------------------------------

#: Server name advertised in the ``initialize`` response. MCP spec allows
#: any stable identifier; ``alive`` is short, unambiguous, and matches the
#: pyproject ``[project].name = "alive-mcp"`` without the ``-mcp`` suffix
#: (the suffix is redundant inside an MCP handshake).
APP_NAME = "alive"

#: Protocol version the v0.1 feature-set targets. This is an ADVISORY
#: constant — the SDK controls the actual negotiation: on
#: ``initialize`` it echoes the client's requested ``protocolVersion``
#: if it appears in :data:`mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS`
#: (which includes ``2025-06-18``), otherwise it falls back to
#: :data:`mcp.types.LATEST_PROTOCOL_VERSION`. Tests use this constant
#: as the expected response value. If a future SDK adds a server-side
#: minimum-version knob, this constant becomes the enforcement input.
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

    #: Bounded asyncio queue for audit records with back-pressure.
    #: ``maxsize=AUDIT_QUEUE_MAXSIZE`` means a slow/stalled writer
    #: eventually blocks producers rather than silently dropping
    #: audit entries — the right posture for a security-audit channel
    #: where dropped entries would violate the invariant. T12 replaces
    #: the stub writer with a real JSONL appender + rotation; until
    #: then the queue exists so tool code can write to it without
    #: conditional plumbing.
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

    #: The active :class:`ServerSession`, captured during the first
    #: ``_handle_message`` dispatch (see :func:`_install_session_capture`).
    #: MCP notifications fire outside a bound request-context, so the
    #: low-level ``request_ctx`` contextvar is unset when the
    #: ``initialized`` or ``roots/list_changed`` handlers run. We
    #: therefore stash the session here via a ``_handle_message`` wrapper
    #: and read it from the notification handlers.
    active_session: Optional[Any] = None


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
        #
        # ``resources_changed=True`` is a deliberate v0.1 commitment —
        # the T5 task brief locks in "YES, advertise both subscribe and
        # listChanged" even though the actual notification emission
        # lands in T11. Rationale: advertising early lets MCP clients
        # subscribe to the kernel-file resources T10 exposes without a
        # second round-trip when T11 adds the walnut-inventory watches.
        # The v0.1 -> v0.2 capability shape stays stable for clients,
        # which matters for the "invites MCP-capable agent" goal.
        forced = NotificationOptions(
            prompts_changed=False,
            resources_changed=True,  # Advertised in v0.1, emitted in T11.
            tools_changed=False,  # Tool roster is frozen for v0.1.
        )
        caps = original(forced, experimental_capabilities)

        # Post-process: flip subscribe on the resources capability so the
        # v0.1 subscription protocol (T11) is advertised. ``listChanged``
        # was already set to True by ``NotificationOptions(resources_
        # changed=True)`` above; we set it again explicitly to make the
        # intent obvious to a future maintainer reviewing this block. If
        # the SDK stops emitting a resources capability (handler
        # deregistration), we preserve the None and let the envelope
        # layer raise a tool-level error if anything tries to subscribe.
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

    Uses ``logging.basicConfig(..., force=True)`` so any pre-existing
    handlers — including ones a host process or a dep installed before
    we were imported — are REPLACED with a single stderr handler. This
    is load-bearing: an embedded stdout handler (e.g. a notebook
    integration's default) would contaminate the JSON-RPC frame channel
    and make the server silently unusable. The safe default is "one
    stderr handler, no matter what came before."

    ``force=True`` is available since Python 3.8; our floor is 3.10, so
    this is unconditionally safe. We re-call basicConfig on every
    invocation because the function is cheap and the "own-the-handler"
    invariant is more important than idempotence.

    stderr is the MCP stdio transport's out-of-band channel: JSON-RPC
    rides stdout, human-readable diagnostics ride stderr. Writing logs
    to stdout would corrupt framing and is the #1 reason stdio MCP
    servers fail silently.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


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


def _file_uri_to_path(uri: str) -> Optional[str]:
    """Parse an MCP ``file://`` root URI into a local absolute path.

    Handles the shapes real MCP clients emit in the wild:

    * ``file:///Users/patrick/world`` — the canonical POSIX shape.
    * ``file://localhost/Users/patrick/world`` — accepted by the URI
      spec (RFC 8089). Treated as equivalent to ``file:///...``.
    * ``file:///Users/patrick/my%20world`` — percent-encoded spaces
      and unicode. :func:`urllib.parse.unquote` normalizes these.

    Non-``file://`` URIs (``http://``, custom schemes) return ``None``
    and the caller logs a warning — v0.1 only supports local roots.
    Unexpected URI shapes (``file://`` with a non-local host, missing
    path, etc.) also return ``None``.

    Returns the absolute local path (percent-decoded), or ``None`` if
    the URI cannot be safely parsed.
    """
    try:
        parts = urllib.parse.urlsplit(uri)
    except ValueError:
        return None

    if parts.scheme != "file":
        return None

    # RFC 8089: ``file://<host>/<path>`` where <host> is empty or
    # "localhost" means "local filesystem". Any other host is a remote
    # resource we can't access.
    host = parts.netloc
    if host not in ("", "localhost"):
        return None

    # Percent-decode the path component. An empty path ("file://")
    # with no segments is malformed for our purposes — bail.
    path = urllib.parse.unquote(parts.path)
    if not path:
        return None

    return path


#: Timeout (seconds) for the server-initiated ``roots/list`` request.
#: The request is synchronous — the server awaits the client's reply —
#: so a client that ignores ``roots/list`` would otherwise block our
#: ``initialized`` handler indefinitely. 5s is generous for a
#: well-behaved client (in-memory + stdio both return in single-digit
#: ms) but short enough that a bad client doesn't stall Roots
#: discovery forever. Env-fallback kicks in on timeout.
ROOTS_LIST_TIMEOUT_S = 5.0


def _client_supports_roots(session: Any) -> bool:
    """Return True if the client advertised ``roots`` capability on ``initialize``.

    The MCP spec says the server may only send ``roots/list`` if the
    client declared ``capabilities.roots`` in its initialize payload.
    Sending unconditionally is a spec violation and — more pragmatically —
    can hang the server if the client's message handler silently drops
    requests for capabilities it didn't opt in to.

    The SDK stashes the client's initialize params on
    ``session.client_params``; ``capabilities.roots`` is ``None`` if
    the client did not advertise roots. We treat any non-None value as
    support (the subtype ``RootsCapability`` carries a ``listChanged``
    flag but the presence of the object itself is the capability
    assertion).
    """
    try:
        client_params = session.client_params
    except AttributeError:  # pragma: no cover — SDK shape change.
        return False
    if client_params is None:
        return False
    caps = getattr(client_params, "capabilities", None)
    if caps is None:
        return False
    return getattr(caps, "roots", None) is not None


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

    # Skip the server-initiated request entirely if the client didn't
    # advertise roots capability. Two reasons:
    #
    # 1. MCP spec compliance: servers must not send requests that
    #    require a capability the client didn't declare.
    # 2. Hang safety: a client that dropped the request without
    #    responding would otherwise pin our discovery task on the
    #    ``await`` until the timeout fires (5s per
    #    :data:`ROOTS_LIST_TIMEOUT_S`). Skipping the request entirely
    #    cuts the 5s idle out of every "no Roots, env-only" startup.
    if not _client_supports_roots(session):
        logger.info(
            "client did not advertise roots capability; skipping "
            "roots/list request, using env-only World discovery"
        )
    else:
        try:
            # ``asyncio.wait_for`` returns on cancel via
            # ``TimeoutError`` (Py3.11+; earlier used
            # ``asyncio.TimeoutError``, which is aliased to
            # ``TimeoutError`` on 3.11+). We catch the broad
            # ``Exception`` below so any SDK-specific error (closed
            # stream, etc.) takes the same fallback path.
            result = await asyncio.wait_for(
                session.list_roots(), timeout=ROOTS_LIST_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            logger.warning(
                "roots/list request did not complete within %.1fs; "
                "degrading to env-only discovery for this cycle",
                ROOTS_LIST_TIMEOUT_S,
            )
            result = None
        except Exception as exc:  # noqa: BLE001 — class logged below.
            logger.warning(
                "roots/list request failed (%s); keeping existing "
                "world_root=%r",
                exc.__class__.__name__,
                app_context.world_root,
            )
            result = None

        if result is not None:
            # ``result.roots`` is a list of :class:`types.Root`. Each
            # has a ``uri`` that is a ``file://`` URL per the MCP spec.
            # Extract the local path and let :func:`discover_world` do
            # the predicate matching.
            for root in result.roots:
                uri = str(root.uri)
                path = _file_uri_to_path(uri)
                if path is None:
                    logger.warning(
                        "ignoring unsupported root uri %r (only local "
                        "file:// roots are supported in v0.1)",
                        uri,
                    )
                    continue
                roots.append(path)

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


def _install_session_capture(
    server: FastMCP[Any], app_context: AppContext
) -> None:
    """Wrap the low-level ``_handle_message`` so each dispatch captures the session.

    Notifications (``initialized``, ``roots/list_changed``) fire via
    :meth:`Server._handle_notification`, which takes only the
    notification payload — the active :class:`ServerSession` is NOT
    threaded through. The low-level ``request_ctx`` contextvar is only
    bound during request handling, never during notification handling.

    To let the ``initialized`` handler call ``session.list_roots()``,
    we need a side channel from the dispatch layer (which has the
    session) to the handler (which does not). The cheapest reliable
    side channel is to wrap :meth:`Server._handle_message` — the
    dispatch method receives ``session`` as a parameter on every call.
    We stash it on :attr:`AppContext.active_session` so the
    notification handlers can pick it up.

    The wrapper is installed once per lifespan entry and removed on
    exit (best-effort — the server process typically exits right
    after lifespan teardown, so a leaked wrapper is harmless). The
    wrapper preserves the original method's signature exactly so any
    future SDK-level change to `_handle_message` is surfaced by a
    TypeError at the wrapper boundary rather than silently breaking.

    Forward-compat note: if ``mcp`` ever adds a public hook for
    "dispatch starts" (e.g. middleware), this wrapper becomes
    obsolete and can be replaced with the supported extension point.
    """
    original = server._mcp_server._handle_message

    async def _wrapped(
        message: Any,
        session: Any,
        lifespan_context: Any,
        raise_exceptions: bool = False,
    ) -> None:
        # Capture on EVERY dispatch, not just the first — a client that
        # reconnects within the same server run would bind a fresh
        # session. The stdio transport in v0.1 only has one session per
        # run, but the shape is forward-compatible.
        app_context.active_session = session
        await original(message, session, lifespan_context, raise_exceptions)

    # Bind to the instance, not the class. Other servers constructed
    # from the same SDK are unaffected.
    server._mcp_server._handle_message = _wrapped  # type: ignore[method-assign]


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

    # Step 5a: runtime Roots API validation. The T5 spec requires us
    # to VERIFY the SDK actually exposes both halves of the Roots
    # protocol (server-initiated ``roots/list`` request + a hook for
    # the ``roots/list_changed`` notification). If either surface is
    # missing — because the pin range ``mcp>=1.27,<2.0`` includes a
    # future release that refactored the API — we log a warning on
    # stderr and degrade to env-only World discovery. Tests
    # (RootsApiSurfaceTests) also assert presence, but the build-time
    # check and the runtime check serve different audiences: tests
    # fail CI, runtime checks keep a deployed server working.
    from mcp.server.session import ServerSession  # local import.

    _roots_api_available = (
        hasattr(ServerSession, "list_roots")
        and hasattr(mcp_types, "RootsListChangedNotification")
        and hasattr(mcp_types, "InitializedNotification")
        and isinstance(
            getattr(server._mcp_server, "notification_handlers", None),
            dict,
        )
    )

    if not _roots_api_available:
        # Graceful degrade: keep the env-resolved ``world_root`` (if
        # any), skip Roots wiring entirely. Tools that need a world
        # will still emit ``ERR_NO_WORLD`` if env discovery also
        # failed — the envelope layer handles that uniformly.
        logger.warning(
            "FastMCP Roots API surface missing "
            "(list_roots / RootsListChangedNotification / "
            "notification_handlers). Degrading to env-only World "
            "discovery. Set ALIVE_WORLD_ROOT to point at your World."
        )
    else:
        # Step 5b: install the session-capture wrapper BEFORE the handlers
        # that depend on :attr:`AppContext.active_session`. The wrapper
        # intercepts every ``_handle_message`` call and stashes the session
        # onto the context so notification handlers can reach it.
        _install_session_capture(server, app_context)

        # Step 5c: notification handlers. The low-level server's
        # ``notification_handlers`` dict is a public-by-convention field
        # (per :mod:`mcp.server.lowlevel.server` line 159) that routes
        # incoming client notifications. The handlers read the session from
        # :attr:`AppContext.active_session` (populated by the capture
        # wrapper) because notification dispatch does not bind
        # ``request_ctx`` or pass a session argument.
        async def _on_initialized(
            notify: mcp_types.InitializedNotification,
        ) -> None:
            session = app_context.active_session
            if session is None:  # pragma: no cover — capture runs first.
                logger.warning(
                    "initialized notification fired but no active session "
                    "captured; skipping Roots discovery"
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
            session = app_context.active_session
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


async def _ensure_roots_discovered(app_context: AppContext) -> None:
    """Trigger Roots discovery lazily on first demand.

    Reads :attr:`AppContext.active_session` (populated by the capture
    wrapper installed in the lifespan) and runs Roots + env discovery
    if it has not already run. No-op after the first successful call.

    This is a public seam for T6+ tools to call at the top of their
    handler. The normal discovery path is the
    ``initialized``-notification handler; this helper is a safety net
    for the (rare) case where a tool fires before ``initialized`` is
    processed (shouldn't happen per the MCP spec, but belt-and-
    suspenders: the client could interleave messages aggressively).
    """
    if app_context.roots_discovery_attempted:
        return
    session = app_context.active_session
    if session is None:  # pragma: no cover — capture runs before tools.
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
    #
    # ``mask_error_details`` compatibility shim: the spec references a
    # ``mask_error_details=True`` FastMCP option that prevents the SDK
    # from echoing raw exception strings into error responses. That
    # parameter arrives in the pre-2.0 branch of ``mcp``; our pin
    # (``mcp>=1.27,<2.0``) does not have it. We detect the parameter
    # at runtime and pass it when available — so the server becomes
    # masked automatically once the SDK lands the feature, without a
    # code change here. Until then, the envelope layer (T4) enforces
    # redaction on EVERY error message and EVERY string kwarg before
    # templating (see :mod:`alive_mcp.envelope`), and the caretaker
    # contract forbids raising plain exceptions on the tool surface —
    # so the invariant holds via two independent mechanisms.
    import inspect as _inspect

    _fastmcp_params = _inspect.signature(FastMCP.__init__).parameters
    _fastmcp_extra_kwargs: dict[str, Any] = {}
    if "mask_error_details" in _fastmcp_params:
        _fastmcp_extra_kwargs["mask_error_details"] = True

    server: FastMCP[AppContext] = FastMCP(
        name=APP_NAME,
        instructions=(
            "Read-only access to an ALIVE Context System World. "
            "Tools and resources land in T6-T11."
        ),
        lifespan=lifespan,
        log_level="INFO",
        **_fastmcp_extra_kwargs,
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

    # Register tool surface. Each tool module exposes a ``register``
    # callable that attaches its tools to the server via the FastMCP
    # ``tool`` decorator. Done after capability install so the
    # ``tools.listChanged=False`` flag is already committed -- the
    # roster frozen here is the roster advertised on initialize.
    # Import locally to keep the server module's import graph narrow
    # (tools pull in envelope + errors + paths + vendored helpers).
    from alive_mcp.tools import bundle as _bundle_tools  # noqa: E402
    from alive_mcp.tools import walnut as _walnut_tools  # noqa: E402

    _walnut_tools.register(server)
    _bundle_tools.register(server)

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
