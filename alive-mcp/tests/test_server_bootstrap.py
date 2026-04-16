"""Bootstrap tests for the FastMCP server (T5 / fn-10-60k.5).

Acceptance coverage (verbatim from the task spec):

* ``uv run alive-mcp`` starts, reads a canonical ``initialize`` message,
  echoes ``protocolVersion`` ``2025-06-18`` in the response.
* Sending ``notifications/initialized`` transitions the server to
  running state (no error response, no crash).
* Closing stdin (EOF) causes clean shutdown.
* All logs go to stderr, nothing to stdout except JSON-RPC messages.
* Lifespan hook logs ``"alive-mcp v0.1.0 starting"`` on stderr.
* ``ERR_NO_WORLD`` path does NOT crash the server on initialize (tools
  emit it lazily; the handshake itself succeeds whether a World is
  resolved or not).
* Unit test spawns the server as subprocess, sends initialize via stdin
  pipe, asserts response shape.
* FastMCP Roots API surface is exercised enough to catch SDK drift (the
  session object exposes ``list_roots`` and the low-level server exposes
  ``notification_handlers`` with a slot for
  ``RootsListChangedNotification``). If either API disappears in a future
  ``mcp`` release, this test fails loudly at build time.
* Capabilities declared match the v0.1 matrix: ``tools.listChanged=False``,
  ``resources.subscribe=True``, ``resources.listChanged=True``. No over-
  advertised primitives (no prompts, no experimental).

Subprocess harness
------------------
The server runs as a separate Python process so we exercise the real
stdio transport (fds + pipe framing + EOF handling) rather than an
in-process shim. Each test wires stdin/stdout/stderr to pipes, sends a
hand-assembled JSON-RPC frame, reads one response frame, and closes
stdin to signal shutdown. Timeouts keep the suite deterministic on CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from typing import Any

# Ensure the ``src/`` layout is importable (tests/__init__.py does this
# too, but being explicit keeps this module self-contained for the
# in-process capability test below).
import tests  # noqa: F401 — side-effect import for src/ path injection.


# -----------------------------------------------------------------------------
# Helpers. Kept inline rather than in a conftest so the test module is
# self-contained and greppable.
# -----------------------------------------------------------------------------

# Per MCP spec, stdio JSON-RPC frames are delimited by a single newline.
# (The spec forbids embedded newlines inside a frame; pretty-printing a
# payload would corrupt framing.) We therefore serialize with
# ``separators`` that omit whitespace and append ``\n`` manually.
_NEWLINE = b"\n"

# The protocol version we pin. Matches :data:`alive_mcp.server.PROTOCOL_VERSION_PINNED`.
_PROTOCOL = "2025-06-18"

# Canonical initialize request per the MCP spec. Client name is a test
# marker so we can grep for it in audit logs later (T12).
_INITIALIZE_REQUEST: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": _PROTOCOL,
        "capabilities": {
            "roots": {"listChanged": True},
        },
        "clientInfo": {
            "name": "alive-mcp-test-harness",
            "version": "0.0.0",
        },
    },
}

_INITIALIZED_NOTIFICATION: dict[str, Any] = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}


def _encode(frame: dict[str, Any]) -> bytes:
    """Serialize a JSON-RPC frame with the exact wire shape MCP expects.

    ``separators=(",", ":")`` matches the envelope's own serialization
    style. ``ensure_ascii=False`` keeps unicode identifiers (walnut
    names like ``ryn-okata``) readable on the wire.
    """
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    ) + _NEWLINE


def _spawn_server(env_overrides: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    """Start the server as a subprocess with stdio pipes.

    ``ALIVE_WORLD_ROOT`` is deliberately unset so the discovery path that
    emits ``ERR_NO_WORLD`` (but must not crash the server) is exercised.
    Individual tests can override the env by passing ``env_overrides``.

    We invoke via ``python -m alive_mcp`` rather than the installed
    ``alive-mcp`` script because the console script may not be on
    ``PATH`` when the test runs from a src checkout without
    ``uv sync``/``pip install -e .``. The ``-m`` form always works as
    long as ``tests/__init__.py`` added ``src/`` to ``sys.path``.
    """
    env = dict(os.environ)
    # Strip any ambient world pointer so discovery fails — keeps the
    # handshake surface area identical across developer machines.
    env.pop("ALIVE_WORLD_ROOT", None)
    env.pop("ALIVE_WORLD_PATH", None)
    # Prepend ``src/`` so ``python -m alive_mcp`` works from a clean
    # checkout without ``uv sync``.
    repo_src = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
    )
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        repo_src + (os.pathsep + existing if existing else "")
    )
    if env_overrides:
        env.update(env_overrides)

    return subprocess.Popen(
        [sys.executable, "-m", "alive_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _read_one_frame(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> dict[str, Any]:
    """Read a single newline-delimited JSON frame from the server's stdout.

    Uses ``proc.stdout.readline()`` which blocks on the pipe; a hung
    server manifests as a pytest timeout rather than a hung test run.
    The timeout is applied by the test-level ``@unittest.skipUnless`` or
    by our ``_communicate_with_timeout`` helper — ``readline`` itself
    cannot be interrupted, so callers wrap this in a thread if needed.
    """
    del timeout  # readline blocks; the caller should use communicate().
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        # EOF without a response. Drain stderr so the AssertionError
        # carries the server's diagnostic output.
        assert proc.stderr is not None
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        raise AssertionError(
            f"server exited without sending a frame; stderr=\n{stderr}"
        )
    return json.loads(line.decode("utf-8"))


# -----------------------------------------------------------------------------
# Subprocess-based integration tests. These exercise the real stdio
# transport end-to-end.
# -----------------------------------------------------------------------------


class InitializeHandshakeTests(unittest.TestCase):
    """Drive the server through a canonical initialize + initialized flow."""

    def test_initialize_returns_expected_protocol_version(self) -> None:
        """Server echoes the pinned protocol version and server metadata."""
        proc = _spawn_server()
        try:
            # Send initialize, read the response, then send initialized
            # and close stdin for a clean shutdown. ``communicate`` with
            # a timeout is the safest shape — it prevents a hung server
            # from wedging the test run.
            input_bytes = _encode(_INITIALIZE_REQUEST) + _encode(
                _INITIALIZED_NOTIFICATION
            )
            stdout, stderr = proc.communicate(input=input_bytes, timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail("server did not respond + shut down within 15s")
        finally:
            if proc.poll() is None:
                proc.kill()

        # Every non-blank line on stdout MUST be a valid JSON-RPC 2.0
        # frame. The ``initialize`` response appears among them; any
        # additional frames are legitimate if they parse as JSON-RPC
        # (e.g. a server-initiated ``roots/list`` request that our
        # test harness didn't answer). What we will NOT tolerate is a
        # non-JSON line on stdout — that's the print()-contamination
        # failure mode.
        init_response = None
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                frame = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                self.fail(
                    "non-JSON line on stdout (print() contamination?): "
                    "{line!r}; decode error: {exc}. Stderr: {stderr!r}".format(
                        line=line.decode("utf-8", errors="replace"),
                        exc=exc,
                        stderr=stderr.decode("utf-8", errors="replace"),
                    )
                )
            self.assertEqual(
                frame.get("jsonrpc"),
                "2.0",
                msg=f"stdout frame is not JSON-RPC 2.0: {frame!r}",
            )
            if frame.get("id") == 1 and "result" in frame:
                init_response = frame

        self.assertIsNotNone(
            init_response,
            msg="server never emitted initialize response (id=1)",
        )
        response = init_response
        result = response["result"]

        # Acceptance: protocolVersion echoed at 2025-06-18.
        self.assertEqual(result.get("protocolVersion"), _PROTOCOL)

        # Server info advertises our app name.
        self.assertEqual(result.get("serverInfo", {}).get("name"), "alive")

        # Capabilities match the v0.1 matrix.
        caps = result.get("capabilities", {})
        self.assertIn("tools", caps)
        self.assertIn("resources", caps)

        tools_caps = caps["tools"]
        # tools.listChanged MUST be False (frozen roster). The MCP wire
        # shape omits False booleans when ``exclude_none`` serialization
        # is used, so we accept both absent-and-False.
        self.assertIn(tools_caps.get("listChanged", False), (False,))

        resources_caps = caps["resources"]
        self.assertTrue(
            resources_caps.get("subscribe"),
            msg=(
                "resources.subscribe must be True so T11 subscriptions "
                "are advertised. Got {!r}".format(resources_caps)
            ),
        )
        self.assertTrue(
            resources_caps.get("listChanged"),
            msg=(
                "resources.listChanged must be True so T11 walnut-"
                "inventory watching is advertised. Got {!r}".format(
                    resources_caps
                )
            ),
        )

        # Prompts capability MUST NOT be advertised (v0.1 does not
        # implement prompts; advertising would be over-promising).
        self.assertNotIn(
            "prompts",
            caps,
            msg=(
                "v0.1 must not advertise prompts capability. Got caps "
                "keys: {!r}".format(sorted(caps.keys()))
            ),
        )

        # Completions capability MUST NOT be advertised either.
        self.assertNotIn("completions", caps)

    def test_server_shuts_down_cleanly_on_stdin_eof(self) -> None:
        """Closing stdin causes the server to exit 0 within a bounded time."""
        proc = _spawn_server()
        try:
            # No initialize — just close stdin immediately. The server
            # must notice EOF and exit without error.
            input_bytes = b""
            proc.communicate(input=input_bytes, timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail("server did not exit within 10s of stdin EOF")

        # A clean exit on EOF is exit code 0. Anyio's stdio_server
        # raises on EOF which the low-level server swallows; we expect
        # the process to come out of ``run()`` normally.
        self.assertEqual(proc.returncode, 0, msg="server exited non-zero on EOF")

    def test_stdout_carries_only_json_rpc_frames(self) -> None:
        """No ``print()`` output contaminates the JSON-RPC channel.

        This is the most load-bearing invariant in the module: any
        ``print()`` anywhere in the import chain — or in a dep's import
        path — would surface here as an extra line on stdout that
        doesn't parse as JSON-RPC. The test encodes "every line on
        stdout must be a JSON object with a jsonrpc field".
        """
        proc = _spawn_server()
        try:
            input_bytes = _encode(_INITIALIZE_REQUEST) + _encode(
                _INITIALIZED_NOTIFICATION
            )
            stdout, _stderr = proc.communicate(input=input_bytes, timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail("server did not respond within 15s")

        for raw in stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                frame = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                self.fail(
                    "non-JSON line on stdout (print() contamination?): "
                    "{line!r}; decode error: {exc}".format(
                        line=line.decode("utf-8", errors="replace"),
                        exc=exc,
                    )
                )
            else:
                self.assertEqual(
                    frame.get("jsonrpc"),
                    "2.0",
                    msg="every stdout line must be a JSON-RPC 2.0 frame",
                )

    def test_startup_banner_emitted_on_stderr(self) -> None:
        """The lifespan logs ``alive-mcp v<ver> starting`` to stderr.

        Required by the acceptance criteria — gives the human a
        confirm-it's-running signal without parsing JSON-RPC. Also
        verifies stderr is the ONLY diagnostic channel (no banner on
        stdout).
        """
        proc = _spawn_server()
        try:
            input_bytes = _encode(_INITIALIZE_REQUEST) + _encode(
                _INITIALIZED_NOTIFICATION
            )
            stdout, stderr = proc.communicate(input=input_bytes, timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail("server did not respond within 15s")

        stderr_text = stderr.decode("utf-8", errors="replace")
        self.assertIn(
            "alive-mcp v",
            stderr_text,
            msg="startup banner missing from stderr",
        )
        self.assertIn("starting", stderr_text)

        # The banner MUST NOT appear on stdout.
        self.assertNotIn(b"alive-mcp v", stdout)
        self.assertNotIn(b"starting", stdout)

    def test_initialize_succeeds_without_world(self) -> None:
        """Discovery failure at init does NOT error the handshake.

        ``ERR_NO_WORLD`` is a tool-level error; tools emit it lazily on
        first call. The initialize handshake must succeed whether a
        World has been resolved or not, because the client may set
        Roots AFTER initialize via a roots/list_changed notification.

        The client's ``capabilities.roots`` in the request tells the
        server it's safe to issue a server-initiated ``roots/list``
        request, which we see on stdout as an additional JSON-RPC
        frame. The test accepts that extra frame — what we're asserting
        is that the initialize RESPONSE (id=1) carries a ``result``,
        not an ``error``.
        """
        proc = _spawn_server()
        try:
            input_bytes = _encode(_INITIALIZE_REQUEST) + _encode(
                _INITIALIZED_NOTIFICATION
            )
            stdout, _stderr = proc.communicate(input=input_bytes, timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail("server did not respond within 15s")

        init_response = None
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            frame = json.loads(line.decode("utf-8"))
            if frame.get("id") == 1 and ("result" in frame or "error" in frame):
                init_response = frame
                break

        self.assertIsNotNone(init_response, msg="no initialize response")
        self.assertIn("result", init_response)
        self.assertNotIn("error", init_response)


# -----------------------------------------------------------------------------
# In-process unit tests. These are fast and exercise module surface
# without spawning subprocesses.
# -----------------------------------------------------------------------------


class BuildServerTests(unittest.TestCase):
    """build_server() produces a configured FastMCP instance."""

    def test_build_server_returns_fastmcp_instance(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from alive_mcp.server import build_server

        server = build_server()
        self.assertIsInstance(server, FastMCP)

    def test_build_server_is_idempotent_on_capability_override(self) -> None:
        """Calling build_server repeatedly must not stack monkey-patches.

        The capability override stashes the original ``get_capabilities``
        under a private attribute; re-applying the override against the
        SAME server instance must be a no-op, or we'd nest wrappers and
        eventually blow the stack on hot-reload in dev.
        """
        from alive_mcp.server import _install_capability_override, build_server

        server = build_server()
        original_wrapper = server._mcp_server.get_capabilities
        _install_capability_override(server)  # Second call.
        self.assertIs(
            server._mcp_server.get_capabilities,
            original_wrapper,
            msg="capability override stacked on second install",
        )


class CapabilityOverrideTests(unittest.TestCase):
    """The v0.1 capability matrix is advertised correctly."""

    def test_capabilities_advertise_subscribe_true_and_list_changed(self) -> None:
        from mcp.server.lowlevel.server import NotificationOptions

        from alive_mcp.server import build_server

        server = build_server()
        caps = server._mcp_server.get_capabilities(NotificationOptions(), {})

        self.assertIsNotNone(caps.resources, "resources capability absent")
        self.assertTrue(caps.resources.subscribe)
        self.assertTrue(caps.resources.listChanged)

        self.assertIsNotNone(caps.tools, "tools capability absent")
        self.assertFalse(caps.tools.listChanged)

    def test_capabilities_do_not_advertise_unimplemented_primitives(self) -> None:
        """v0.1 does not implement prompts or completions."""
        from mcp.server.lowlevel.server import NotificationOptions

        from alive_mcp.server import build_server

        server = build_server()
        caps = server._mcp_server.get_capabilities(NotificationOptions(), {})

        # Prompts: we do not register prompt handlers, so FastMCP's
        # auto-registration might still advertise an empty capability.
        # If it does, it must at least not advertise listChanged — a
        # safety check that ensures we didn't accidentally wire prompt
        # change notifications.
        if caps.prompts is not None:
            self.assertFalse(caps.prompts.listChanged)

        # Completions: FastMCP only registers a completion handler if a
        # completion function is attached via ``@mcp.completion()``. We
        # don't attach one in v0.1; capability must be absent.
        self.assertIsNone(
            caps.completions,
            msg=(
                "v0.1 must not advertise completions capability. Got {!r}"
            ).format(caps.completions),
        )


class RootsDiscoveryEndToEndTests(unittest.TestCase):
    """Exercise the Roots discovery path end-to-end with a live client.

    Uses the mcp SDK's in-memory transport
    (:func:`mcp.shared.memory.create_connected_server_and_client_session`)
    to drive the server with a real ClientSession. The client's
    ``list_roots_callback`` answers the server's ``roots/list``
    request with a fixture World, and the test asserts that the
    server's :class:`AppContext.world_root` gets populated — i.e. the
    full Roots round-trip (server REQUEST -> client RESPONSE -> server
    discovery -> AppContext mutation) works.

    This is the test that was requested by the T5 acceptance bullet
    "FastMCP Roots API exercised: server requests roots/list from
    client in test harness that simulates client, verifies response
    handling". It's an in-process test rather than a subprocess pump
    because: (a) the in-memory transport exists for exactly this
    purpose, (b) a stdout/stdin pump has to simulate the JSON-RPC
    framing by hand, and (c) failures in the real dispatch path would
    otherwise be hard to diagnose.
    """

    def test_server_requests_roots_and_populates_world_root(self) -> None:
        import asyncio
        import os
        import tempfile
        from datetime import timedelta

        from mcp import types as mcp_types
        from mcp.shared.memory import create_connected_server_and_client_session

        from alive_mcp.server import AppContext, build_server

        async def run() -> None:
            # Fixture World directory satisfying the ``.alive/`` predicate.
            with tempfile.TemporaryDirectory(prefix="alive-mcp-roots-") as tmp:
                world_root = os.path.realpath(tmp)
                os.makedirs(os.path.join(world_root, ".alive"), exist_ok=True)

                # Client-side callback: when the server sends
                # ``roots/list``, respond with our fixture World as a
                # single ``file://`` root. Tracks whether the callback
                # was actually invoked — the strongest evidence that
                # the server issued a server-initiated request.
                callback_invocations: list[str] = []

                async def list_roots_cb(
                    ctx: Any,
                ) -> mcp_types.ListRootsResult:
                    callback_invocations.append(world_root)
                    return mcp_types.ListRootsResult(
                        roots=[
                            mcp_types.Root(
                                uri=f"file://{world_root}",  # type: ignore[arg-type]
                                name="fixture",
                            )
                        ]
                    )

                server = build_server()

                # Register a probe tool BEFORE the in-memory session
                # spins up. The probe reads AppContext from the
                # lifespan result and stashes it onto the shared dict
                # so the test can assert on it without worrying about
                # dict-vs-TextContent serialization quirks in FastMCP's
                # tool-result wrapping.
                captured: dict[str, Any] = {}

                @server.tool(name="_probe")
                async def probe() -> dict[str, Any]:
                    ctx = server.get_context()
                    app_ctx: AppContext = ctx.request_context.lifespan_context
                    captured["world_root"] = app_ctx.world_root
                    captured["roots_discovery_attempted"] = (
                        app_ctx.roots_discovery_attempted
                    )
                    captured["active_session"] = app_ctx.active_session
                    return {"ok": True}

                async with create_connected_server_and_client_session(
                    server,
                    list_roots_callback=list_roots_cb,
                    client_info=mcp_types.Implementation(
                        name="alive-mcp-roots-harness",
                        version="0.0.0",
                    ),
                    read_timeout_seconds=timedelta(seconds=10),
                ) as client:
                    # The ``initialized`` notification is dispatched as
                    # a background task in the server's task group; it
                    # may not have completed by the time
                    # ``client.initialize()`` returns. A short sleep
                    # lets the dispatch run to completion before we
                    # probe — 250ms is orders of magnitude more than
                    # the actual round-trip on an in-memory stream.
                    await asyncio.sleep(0.25)

                    await client.call_tool("_probe", arguments={})

                    # Strongest assertion: the client-side callback
                    # actually ran. This can only happen if the server
                    # issued a ``roots/list`` request — i.e. the Roots
                    # API path in the server is wired correctly.
                    self.assertEqual(
                        len(callback_invocations),
                        1,
                        msg=(
                            "client list_roots callback did not fire. "
                            "The server never issued roots/list."
                        ),
                    )

                    self.assertEqual(
                        captured.get("world_root"),
                        world_root,
                        msg=(
                            "server resolved world_root to "
                            f"{captured.get('world_root')!r} instead of "
                            f"{world_root!r} after Roots round-trip."
                        ),
                    )
                    self.assertTrue(captured["roots_discovery_attempted"])
                    self.assertIsNotNone(captured["active_session"])

        asyncio.run(run())


class RootsApiSurfaceTests(unittest.TestCase):
    """Validate the SDK still exposes both halves of the Roots protocol.

    If ``mcp`` ever removes :meth:`ServerSession.list_roots` or drops
    :class:`RootsListChangedNotification`, we want a loud build-time
    failure pointing at this test rather than a runtime surprise in
    production. The test is deliberately surface-level — we're not
    testing the SDK's behavior, just its presence.
    """

    def test_server_session_exposes_list_roots(self) -> None:
        from mcp.server.session import ServerSession

        self.assertTrue(
            hasattr(ServerSession, "list_roots"),
            msg=(
                "mcp.server.session.ServerSession.list_roots is gone. "
                "The Roots API contract changed; update server.py's "
                "_discover_world_with_roots accordingly."
            ),
        )

    def test_roots_list_changed_notification_type_exists(self) -> None:
        from mcp import types

        self.assertTrue(
            hasattr(types, "RootsListChangedNotification"),
            msg=(
                "mcp.types.RootsListChangedNotification is gone. The "
                "Roots notification contract changed; update server.py's "
                "lifespan handler registration accordingly."
            ),
        )

    def test_notification_handlers_is_a_mutable_mapping(self) -> None:
        """Low-level Server still exposes a mutable notification handler dict."""
        from mcp.server.lowlevel.server import Server

        server = Server(name="test")
        self.assertTrue(hasattr(server, "notification_handlers"))
        self.assertIsInstance(server.notification_handlers, dict)


class AppContextTests(unittest.TestCase):
    """AppContext defaults match the lifespan contract."""

    def test_defaults(self) -> None:
        import asyncio

        from alive_mcp.server import AUDIT_QUEUE_MAXSIZE, AppContext

        ctx = AppContext()
        self.assertIsNone(ctx.world_root)
        self.assertIsNone(ctx.audit_writer_task)
        self.assertIsNone(ctx.observer)
        self.assertFalse(ctx.roots_discovery_attempted)
        self.assertIsInstance(ctx.audit_queue, asyncio.Queue)
        self.assertEqual(ctx.audit_queue.maxsize, AUDIT_QUEUE_MAXSIZE)


class MainEntrypointTests(unittest.TestCase):
    """__main__.main dispatches correctly between --version and server."""

    def test_version_flag_prints_version(self) -> None:
        """``alive-mcp --version`` returns 0 and prints the version.

        Exercises the non-server path. The server path is covered by the
        subprocess tests above; invoking it in-process would hang on the
        stdio transport.
        """
        import io
        from contextlib import redirect_stdout

        from alive_mcp import __version__
        from alive_mcp.__main__ import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--version"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), __version__)


# -----------------------------------------------------------------------------
# Platform guards. Windows CI doesn't run these integration tests because
# the stdio-via-pipe shape behaves differently on Windows and our v0.1
# target is POSIX (Claude Desktop on macOS is the primary client).
# -----------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover
    # Force-skip the subprocess tests on Windows. The in-process tests
    # (BuildServerTests, CapabilityOverrideTests, etc.) run fine.
    InitializeHandshakeTests = unittest.skip(  # type: ignore[misc]
        "subprocess stdio tests require POSIX; use uv on macOS/Linux"
    )(InitializeHandshakeTests)


if __name__ == "__main__":
    unittest.main()
