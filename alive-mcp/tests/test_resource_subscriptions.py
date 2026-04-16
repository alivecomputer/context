"""Resource subscription tests (fn-10-60k.11 / T11).

Exercises the watchdog observer -> MCP notification pipeline end-to-end:

* :class:`ClassifyPathTests` -- pure-function classification of
  filesystem paths against the kernel-file allowlist. Covers every
  acceptance bullet for "which events become notifications":

  * ``key.md`` / ``log.md`` / ``insights.md`` / ``now.json`` map to
    ``kernel`` classifications.
  * v2 ``_generated/now.json`` routes to the same ``now`` URI as v3.
  * ``tasks.json`` / ``completed.json`` / ``links.yaml`` /
    ``people.yaml`` / ``history/chapter-*.md`` all classify as
    ``ignored``.
  * ``.alive/_mcp/**`` classifies as ``ignored`` (audit log tree).

* :class:`SubscriptionRegistryTests` -- subscriber set semantics
  (idempotent subscribe, idempotent unsubscribe, empty-set cleanup).

* :class:`DebouncedEmissionTests` -- invokes :class:`KernelEventHandler`
  directly on a controlled event loop and verifies:

  * 10 rapid events on the same URI produce exactly one notification.
  * Zero subscribers -> zero notifications.
  * Events across different URIs coalesce independently.
  * list-changed debounces separately from per-URI updates.

* :class:`ObserverEndToEndTests` -- spins up the full server (in-memory
  transport), subscribes via the client, writes kernel files, and
  asserts notifications arrive on the client's message_handler within
  a generous timeout. Verifies:

  * Writing to ``_kernel/log.md`` triggers one ``resources/updated``
    notification at the correct URI.
  * v2 ``_generated/now.json`` writes emit with the v3 URI.
  * Excluded paths (audit log, history chapters, tasks.json) do not
    trigger notifications.
  * Creating a fresh walnut's ``_kernel/key.md`` triggers
    ``resources/list_changed``.

These tests avoid the stdio transport (which would be slow and brittle
under pytest) and use the same in-memory session harness as T10's
:mod:`tests.test_resources_kernel`.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import tempfile
import unittest
from datetime import timedelta
from typing import Any, List, Optional

# Make ``src/`` importable.
import tests  # noqa: F401

from alive_mcp.resources.subscriptions import (  # noqa: E402
    KernelEventHandler,
    SubscriptionRegistry,
    classify_path,
)
from alive_mcp.uri import encode_kernel_uri  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builder -- same shape as test_resources_kernel.
# ---------------------------------------------------------------------------


def _new_world() -> tuple[pathlib.Path, Any]:
    tmpdir = tempfile.mkdtemp(prefix="alive-mcp-subs-test-")
    root = pathlib.Path(tmpdir)
    (root / ".alive").mkdir()
    cleanup = lambda: shutil.rmtree(tmpdir, ignore_errors=True)  # noqa: E731
    return root, cleanup


def _make_walnut(
    world_root: pathlib.Path,
    rel: str,
    *,
    log_body: str = "",
    v2_now: bool = False,
) -> None:
    key_path = world_root / rel / "_kernel" / "key.md"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("---\ntype: venture\n---\n\n# {}\n".format(rel))
    if log_body:
        log_path = world_root / rel / "_kernel" / "log.md"
        log_path.write_text(log_body)
    if v2_now:
        now_path = (
            world_root / rel / "_kernel" / "_generated" / "now.json"
        )
        now_path.parent.mkdir(parents=True, exist_ok=True)
        now_path.write_text("{}")


# ---------------------------------------------------------------------------
# Classification: pure function, no I/O.
# ---------------------------------------------------------------------------


class ClassifyPathTests(unittest.TestCase):
    """``classify_path`` covers the spec's event-filter matrix."""

    def setUp(self) -> None:
        self.world = "/fake/world"

    def test_key_md_classifies_as_kernel_key(self) -> None:
        cls = classify_path(
            self.world, "/fake/world/04_Ventures/alive/_kernel/key.md"
        )
        self.assertEqual(cls.kind, "kernel")
        self.assertEqual(cls.walnut_path, "04_Ventures/alive")
        self.assertEqual(cls.file, "key")

    def test_log_md_classifies_as_kernel_log(self) -> None:
        cls = classify_path(
            self.world, "/fake/world/04_Ventures/alive/_kernel/log.md"
        )
        self.assertEqual(cls.kind, "kernel")
        self.assertEqual(cls.file, "log")

    def test_insights_md_classifies_as_kernel_insights(self) -> None:
        cls = classify_path(
            self.world,
            "/fake/world/04_Ventures/alive/_kernel/insights.md",
        )
        self.assertEqual(cls.file, "insights")

    def test_now_json_v3_classifies_as_kernel_now(self) -> None:
        cls = classify_path(
            self.world, "/fake/world/04_Ventures/alive/_kernel/now.json"
        )
        self.assertEqual(cls.file, "now")

    def test_now_json_v2_generated_classifies_as_kernel_now(self) -> None:
        cls = classify_path(
            self.world,
            "/fake/world/04_Ventures/alive/_kernel/_generated/now.json",
        )
        self.assertEqual(cls.kind, "kernel")
        self.assertEqual(cls.file, "now")
        self.assertEqual(cls.walnut_path, "04_Ventures/alive")

    def test_tasks_json_classifies_as_ignored(self) -> None:
        """``tasks.json`` is not exposed as a resource in v0.1."""
        cls = classify_path(
            self.world,
            "/fake/world/04_Ventures/alive/_kernel/tasks.json",
        )
        self.assertEqual(cls.kind, "ignored")

    def test_completed_json_classifies_as_ignored(self) -> None:
        cls = classify_path(
            self.world,
            "/fake/world/04_Ventures/alive/_kernel/completed.json",
        )
        self.assertEqual(cls.kind, "ignored")

    def test_history_chapter_classifies_as_ignored(self) -> None:
        cls = classify_path(
            self.world,
            "/fake/world/04_Ventures/alive/_kernel/history/chapter-01.md",
        )
        self.assertEqual(cls.kind, "ignored")

    def test_links_yaml_classifies_as_ignored(self) -> None:
        cls = classify_path(
            self.world,
            "/fake/world/04_Ventures/alive/_kernel/links.yaml",
        )
        self.assertEqual(cls.kind, "ignored")

    def test_people_yaml_classifies_as_ignored(self) -> None:
        cls = classify_path(
            self.world,
            "/fake/world/04_Ventures/alive/_kernel/people.yaml",
        )
        self.assertEqual(cls.kind, "ignored")

    def test_audit_log_tree_classifies_as_ignored(self) -> None:
        """``.alive/_mcp/**`` never produces resource notifications.

        T12 rotates the audit log via writes to this tree; filtering
        here prevents a feedback loop where rotation triggers
        subscription emissions.
        """
        cls = classify_path(
            self.world, "/fake/world/.alive/_mcp/audit.log"
        )
        self.assertEqual(cls.kind, "ignored")
        cls2 = classify_path(
            self.world,
            "/fake/world/.alive/_mcp/subdir/nested.json",
        )
        self.assertEqual(cls2.kind, "ignored")

    def test_non_kernel_file_classifies_as_ignored(self) -> None:
        cls = classify_path(
            self.world,
            "/fake/world/04_Ventures/alive/README.md",
        )
        self.assertEqual(cls.kind, "ignored")

    def test_path_outside_world_classifies_as_ignored(self) -> None:
        """A stale event for a path above the world root must not crash."""
        cls = classify_path(
            self.world, "/completely/elsewhere/file.md"
        )
        self.assertEqual(cls.kind, "ignored")

    def test_nested_walnut_path_classifies_correctly(self) -> None:
        """Multi-segment walnut paths are supported (people, clients)."""
        cls = classify_path(
            self.world,
            "/fake/world/02_Life/people/ben-flint/_kernel/key.md",
        )
        self.assertEqual(cls.kind, "kernel")
        self.assertEqual(cls.walnut_path, "02_Life/people/ben-flint")
        self.assertEqual(cls.file, "key")

    def test_kernel_dir_at_root_ignored(self) -> None:
        """``_kernel`` directly at world root has no walnut owner."""
        cls = classify_path(self.world, "/fake/world/_kernel/key.md")
        self.assertEqual(cls.kind, "ignored")


# ---------------------------------------------------------------------------
# SubscriptionRegistry unit tests.
# ---------------------------------------------------------------------------


class SubscriptionRegistryTests(unittest.IsolatedAsyncioTestCase):
    """Subscriber set bookkeeping -- add/remove/has_subscribers."""

    async def test_subscribe_adds_to_set(self) -> None:
        reg = SubscriptionRegistry()
        await reg.subscribe("alive://walnut/a/kernel/log", "sess-1")
        self.assertTrue(
            await reg.has_subscribers("alive://walnut/a/kernel/log")
        )

    async def test_subscribe_is_idempotent(self) -> None:
        reg = SubscriptionRegistry()
        await reg.subscribe("alive://walnut/a/kernel/log", "sess-1")
        await reg.subscribe("alive://walnut/a/kernel/log", "sess-1")
        # still just one entry.
        await reg.unsubscribe("alive://walnut/a/kernel/log", "sess-1")
        self.assertFalse(
            await reg.has_subscribers("alive://walnut/a/kernel/log")
        )

    async def test_unsubscribe_unknown_is_noop(self) -> None:
        reg = SubscriptionRegistry()
        # No raise.
        await reg.unsubscribe("alive://walnut/a/kernel/log", "sess-1")
        self.assertFalse(
            await reg.has_subscribers("alive://walnut/a/kernel/log")
        )

    async def test_multiple_sessions_on_same_uri(self) -> None:
        reg = SubscriptionRegistry()
        await reg.subscribe("alive://walnut/a/kernel/log", "sess-1")
        await reg.subscribe("alive://walnut/a/kernel/log", "sess-2")
        await reg.unsubscribe("alive://walnut/a/kernel/log", "sess-1")
        self.assertTrue(
            await reg.has_subscribers("alive://walnut/a/kernel/log")
        )
        await reg.unsubscribe("alive://walnut/a/kernel/log", "sess-2")
        self.assertFalse(
            await reg.has_subscribers("alive://walnut/a/kernel/log")
        )

    async def test_any_subscribers_across_uris(self) -> None:
        reg = SubscriptionRegistry()
        self.assertFalse(await reg.any_subscribers())
        await reg.subscribe("alive://walnut/a/kernel/log", "sess-1")
        self.assertTrue(await reg.any_subscribers())
        await reg.unsubscribe("alive://walnut/a/kernel/log", "sess-1")
        self.assertFalse(await reg.any_subscribers())


# ---------------------------------------------------------------------------
# Debounced emission -- exercise KernelEventHandler directly.
# ---------------------------------------------------------------------------


class _FakeEvent:
    """Minimal stand-in for :class:`watchdog.events.FileSystemEvent`.

    Watchdog's real event classes are frozen dataclasses with a
    ``event_type`` class attribute. We fake the shape with a tiny
    mutable holder so tests can drive the handler without involving
    the real filesystem.
    """

    def __init__(
        self,
        src_path: str,
        event_type: str = "modified",
        is_directory: bool = False,
        dest_path: str = "",
    ) -> None:
        self.src_path = src_path
        self.event_type = event_type
        self.is_directory = is_directory
        self.dest_path = dest_path
        self.is_synthetic = False


class DebouncedEmissionTests(unittest.IsolatedAsyncioTestCase):
    """Verify burst-to-single emission, subscriber filter, per-URI keying."""

    async def asyncSetUp(self) -> None:
        self.world = "/fake/world"
        self.registry = SubscriptionRegistry()
        self.updated_calls: List[str] = []
        self.list_changed_calls = 0

        async def notify_updated(uri: str) -> None:
            self.updated_calls.append(uri)

        async def notify_list_changed() -> None:
            self.list_changed_calls += 1

        loop = asyncio.get_running_loop()
        self.handler = KernelEventHandler(
            world_root=self.world,
            loop=loop,
            registry=self.registry,
            notify_updated=notify_updated,
            notify_list_changed=notify_list_changed,
            debounce_seconds=0.05,  # 50ms for test speed
        )

    async def test_no_subscribers_drops_notification(self) -> None:
        """Event arrives, nobody is subscribed -- no emission."""
        event = _FakeEvent(
            src_path="/fake/world/04_Ventures/alive/_kernel/log.md"
        )
        self.handler.on_any_event(event)
        await asyncio.sleep(0.15)
        self.assertEqual(self.updated_calls, [])

    async def test_single_event_with_subscriber_emits_once(self) -> None:
        uri = encode_kernel_uri("04_Ventures/alive", "log")
        await self.registry.subscribe(uri, "sess-1")
        event = _FakeEvent(
            src_path="/fake/world/04_Ventures/alive/_kernel/log.md"
        )
        self.handler.on_any_event(event)
        await asyncio.sleep(0.15)
        self.assertEqual(self.updated_calls, [uri])

    async def test_10_rapid_events_coalesce_to_one(self) -> None:
        """Acceptance: 10 rapid writes to same file -> 1 debounced notify."""
        uri = encode_kernel_uri("04_Ventures/alive", "log")
        await self.registry.subscribe(uri, "sess-1")
        for _ in range(10):
            event = _FakeEvent(
                src_path="/fake/world/04_Ventures/alive/_kernel/log.md"
            )
            self.handler.on_any_event(event)
        await asyncio.sleep(0.15)
        self.assertEqual(self.updated_calls, [uri])

    async def test_different_uris_debounce_independently(self) -> None:
        uri_log = encode_kernel_uri("04_Ventures/alive", "log")
        uri_key = encode_kernel_uri("04_Ventures/alive", "key")
        await self.registry.subscribe(uri_log, "sess-1")
        await self.registry.subscribe(uri_key, "sess-1")
        self.handler.on_any_event(
            _FakeEvent(
                src_path="/fake/world/04_Ventures/alive/_kernel/log.md"
            )
        )
        self.handler.on_any_event(
            _FakeEvent(
                src_path="/fake/world/04_Ventures/alive/_kernel/key.md"
            )
        )
        await asyncio.sleep(0.15)
        self.assertEqual(sorted(self.updated_calls), sorted([uri_log, uri_key]))

    async def test_v2_now_routes_to_v3_uri(self) -> None:
        """A v2 ``_generated/now.json`` write emits for the v3 URI."""
        uri = encode_kernel_uri("04_Ventures/alive", "now")
        await self.registry.subscribe(uri, "sess-1")
        self.handler.on_any_event(
            _FakeEvent(
                src_path="/fake/world/04_Ventures/alive/_kernel/_generated/now.json"
            )
        )
        await asyncio.sleep(0.15)
        self.assertEqual(self.updated_calls, [uri])

    async def test_list_changed_on_key_md_create(self) -> None:
        """Creating ``_kernel/key.md`` fires debounced list_changed iff subs."""
        # Any subscription suffices -- list_changed is a bare notification.
        await self.registry.subscribe(
            encode_kernel_uri("04_Ventures/other", "log"), "sess-1"
        )
        self.handler.on_any_event(
            _FakeEvent(
                src_path="/fake/world/04_Ventures/fresh/_kernel/key.md",
                event_type="created",
            )
        )
        await asyncio.sleep(0.15)
        self.assertEqual(self.list_changed_calls, 1)

    async def test_list_changed_on_key_md_delete(self) -> None:
        await self.registry.subscribe(
            encode_kernel_uri("04_Ventures/other", "log"), "sess-1"
        )
        self.handler.on_any_event(
            _FakeEvent(
                src_path="/fake/world/04_Ventures/gone/_kernel/key.md",
                event_type="deleted",
            )
        )
        await asyncio.sleep(0.15)
        self.assertEqual(self.list_changed_calls, 1)

    async def test_list_changed_on_key_md_modify_does_not_fire(self) -> None:
        """A key.md MODIFY (edit, not create) does NOT change inventory."""
        await self.registry.subscribe(
            encode_kernel_uri("04_Ventures/other", "log"), "sess-1"
        )
        self.handler.on_any_event(
            _FakeEvent(
                src_path="/fake/world/04_Ventures/alive/_kernel/key.md",
                event_type="modified",
            )
        )
        await asyncio.sleep(0.15)
        self.assertEqual(self.list_changed_calls, 0)

    async def test_list_changed_without_subscribers_drops(self) -> None:
        """Nobody is listening; walnut create fires no list_changed."""
        self.handler.on_any_event(
            _FakeEvent(
                src_path="/fake/world/04_Ventures/lonely/_kernel/key.md",
                event_type="created",
            )
        )
        await asyncio.sleep(0.15)
        self.assertEqual(self.list_changed_calls, 0)

    async def test_audit_log_writes_do_not_notify(self) -> None:
        """Writes to ``.alive/_mcp/**`` never emit subscription notifications."""
        uri = encode_kernel_uri("04_Ventures/alive", "log")
        await self.registry.subscribe(uri, "sess-1")
        self.handler.on_any_event(
            _FakeEvent(src_path="/fake/world/.alive/_mcp/audit.log")
        )
        await asyncio.sleep(0.15)
        self.assertEqual(self.updated_calls, [])

    async def test_history_chapter_writes_do_not_notify(self) -> None:
        uri = encode_kernel_uri("04_Ventures/alive", "log")
        await self.registry.subscribe(uri, "sess-1")
        self.handler.on_any_event(
            _FakeEvent(
                src_path="/fake/world/04_Ventures/alive/_kernel/history/chapter-01.md"
            )
        )
        await asyncio.sleep(0.15)
        self.assertEqual(self.updated_calls, [])

    async def test_directory_events_are_ignored(self) -> None:
        """Directory events (mkdir, rmdir on ``_kernel/``) produce nothing."""
        uri = encode_kernel_uri("04_Ventures/alive", "log")
        await self.registry.subscribe(uri, "sess-1")
        event = _FakeEvent(
            src_path="/fake/world/04_Ventures/alive/_kernel",
            is_directory=True,
            event_type="modified",
        )
        self.handler.on_any_event(event)
        await asyncio.sleep(0.15)
        self.assertEqual(self.updated_calls, [])

    async def test_cancel_pending_cancels_timers(self) -> None:
        """Shutdown: pending debounce timers must be cancelled.

        Otherwise a timer scheduled at t=T-100ms would fire after the
        loop closes and reach into a torn-down session.
        """
        uri = encode_kernel_uri("04_Ventures/alive", "log")
        await self.registry.subscribe(uri, "sess-1")
        self.handler.on_any_event(
            _FakeEvent(
                src_path="/fake/world/04_Ventures/alive/_kernel/log.md"
            )
        )
        # Give the emitter thread handoff a tick to schedule the timer.
        await asyncio.sleep(0.01)
        self.handler.cancel_pending()
        await asyncio.sleep(0.15)
        # Debounced emission was cancelled -- zero calls.
        self.assertEqual(self.updated_calls, [])


# ---------------------------------------------------------------------------
# End-to-end: real server + real watchdog Observer + real filesystem.
# ---------------------------------------------------------------------------


def _run_with_world_and_messages(
    world_root: pathlib.Path,
    coro_factory: Any,
    messages: List[Any],
) -> Any:
    """Start server bound to ``world_root`` with a message_handler.

    Appends every received notification object to ``messages`` so the
    test can assert on what arrived. The harness mirrors
    :func:`_run_with_world` in test_resources_kernel but installs the
    client's message_handler hook so ``resources/updated`` /
    ``resources/list_changed`` notifications are observable.
    """
    from mcp import types as mcp_types
    from mcp.shared.memory import create_connected_server_and_client_session

    from alive_mcp.server import build_server

    async def runner() -> Any:
        world_uri = pathlib.Path(world_root).as_uri()

        async def list_roots_cb(ctx: Any) -> mcp_types.ListRootsResult:
            return mcp_types.ListRootsResult(
                roots=[
                    mcp_types.Root(
                        uri=world_uri,  # type: ignore[arg-type]
                        name="fixture",
                    )
                ]
            )

        async def message_handler(message: Any) -> None:
            messages.append(message)

        server = build_server()
        async with create_connected_server_and_client_session(
            server,
            list_roots_callback=list_roots_cb,
            message_handler=message_handler,
            client_info=mcp_types.Implementation(
                name="alive-mcp-subs-test", version="0.0.0"
            ),
            read_timeout_seconds=timedelta(seconds=10),
        ) as client:
            # Wait for initialized + Roots discovery to land, which
            # also starts the observer.
            await asyncio.sleep(0.3)
            return await coro_factory(client)

    return asyncio.run(runner())


def _filter_updated_for_uri(
    messages: List[Any], uri: str
) -> List[Any]:
    """Return only ``resources/updated`` notifications matching ``uri``."""
    from mcp import types as mcp_types

    out = []
    for m in messages:
        if isinstance(m, mcp_types.ServerNotification):
            root = m.root
            if isinstance(root, mcp_types.ResourceUpdatedNotification):
                if str(root.params.uri) == uri:
                    out.append(root)
    return out


def _filter_list_changed(messages: List[Any]) -> List[Any]:
    from mcp import types as mcp_types

    out = []
    for m in messages:
        if isinstance(m, mcp_types.ServerNotification):
            root = m.root
            if isinstance(root, mcp_types.ResourceListChangedNotification):
                out.append(root)
    return out


class ObserverEndToEndTests(unittest.TestCase):
    """Full pipeline: client subscribes, server observer fires notifications.

    The debounce window is the T11 default (500ms); tests include
    enough sleep after each write to let the debounced emission run.
    """

    def setUp(self) -> None:
        self.world_root, cleanup = _new_world()
        self.addCleanup(cleanup)
        _make_walnut(
            self.world_root,
            "04_Ventures/alive",
            log_body="---\nwalnut: alive\n---\n\nseed\n",
        )

    def _wait_long_enough_for_debounce(self) -> float:
        """Return a sleep duration comfortably past the 500ms debounce.

        Picked so CI variability and coarse FS event latencies don't
        flake the assertion; 1.5s is enough for FSEvents on macOS
        (which coalesces at ~1s in idle mode) plus the 500ms
        debounce.
        """
        return 1.5

    def test_write_log_md_triggers_updated_notification(self) -> None:
        """Subscribe to log URI, write to log.md, assert one updated."""
        from pydantic import AnyUrl

        uri = encode_kernel_uri("04_Ventures/alive", "log")
        messages: List[Any] = []

        async def factory(client: Any) -> Any:
            await client.subscribe_resource(AnyUrl(uri))
            # Write AFTER subscribing so the registry has the URI when
            # the FS event arrives.
            log_path = (
                self.world_root / "04_Ventures/alive/_kernel/log.md"
            )
            log_path.write_text("new content\n")
            await asyncio.sleep(self._wait_long_enough_for_debounce())
            return None

        _run_with_world_and_messages(self.world_root, factory, messages)
        updated = _filter_updated_for_uri(messages, uri)
        self.assertGreaterEqual(
            len(updated),
            1,
            msg="expected at least one resources/updated for {!r}; "
            "received messages: {}".format(uri, messages),
        )

    def test_v2_now_write_uses_v3_uri(self) -> None:
        """Writing ``_generated/now.json`` emits the ``/kernel/now`` URI."""
        from pydantic import AnyUrl

        _make_walnut(self.world_root, "04_Ventures/v2-shaped", v2_now=True)
        uri = encode_kernel_uri("04_Ventures/v2-shaped", "now")
        messages: List[Any] = []

        async def factory(client: Any) -> Any:
            await client.subscribe_resource(AnyUrl(uri))
            now_path = (
                self.world_root
                / "04_Ventures/v2-shaped/_kernel/_generated/now.json"
            )
            now_path.write_text('{"phase": "testing"}')
            await asyncio.sleep(self._wait_long_enough_for_debounce())
            return None

        _run_with_world_and_messages(self.world_root, factory, messages)
        updated = _filter_updated_for_uri(messages, uri)
        self.assertGreaterEqual(len(updated), 1)

    def test_no_subscribers_means_no_notifications(self) -> None:
        """Writing without subscribing produces no ``updated`` traffic."""
        messages: List[Any] = []

        async def factory(client: Any) -> Any:
            log_path = (
                self.world_root / "04_Ventures/alive/_kernel/log.md"
            )
            log_path.write_text("churn " * 100)
            await asyncio.sleep(self._wait_long_enough_for_debounce())
            return None

        _run_with_world_and_messages(self.world_root, factory, messages)
        from mcp import types as mcp_types

        updated = [
            m
            for m in messages
            if isinstance(m, mcp_types.ServerNotification)
            and isinstance(m.root, mcp_types.ResourceUpdatedNotification)
        ]
        self.assertEqual(updated, [])

    def test_audit_log_writes_do_not_notify(self) -> None:
        """Writes to ``.alive/_mcp/audit.log`` never trigger notifications.

        Subscribing to the log URI while the audit log path is
        written verifies the path-level exclusion: the URI has a
        subscriber, but audit-log writes are at a path the classifier
        drops before emission.
        """
        from pydantic import AnyUrl

        uri = encode_kernel_uri("04_Ventures/alive", "log")
        messages: List[Any] = []

        async def factory(client: Any) -> Any:
            await client.subscribe_resource(AnyUrl(uri))
            audit_path = self.world_root / ".alive" / "_mcp" / "audit.log"
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            audit_path.write_text('{"evt": "one"}\n')
            audit_path.write_text('{"evt": "two"}\n')
            await asyncio.sleep(self._wait_long_enough_for_debounce())
            return None

        _run_with_world_and_messages(self.world_root, factory, messages)
        updated = _filter_updated_for_uri(messages, uri)
        self.assertEqual(
            len(updated),
            0,
            msg="audit-log writes should never emit notifications; "
            "got {}".format(updated),
        )

    def test_history_chapter_writes_do_not_notify(self) -> None:
        from pydantic import AnyUrl

        uri = encode_kernel_uri("04_Ventures/alive", "log")
        messages: List[Any] = []

        async def factory(client: Any) -> Any:
            await client.subscribe_resource(AnyUrl(uri))
            ch_path = (
                self.world_root
                / "04_Ventures/alive/_kernel/history/chapter-01.md"
            )
            ch_path.parent.mkdir(parents=True, exist_ok=True)
            ch_path.write_text("chapter content")
            await asyncio.sleep(self._wait_long_enough_for_debounce())
            return None

        _run_with_world_and_messages(self.world_root, factory, messages)
        updated = _filter_updated_for_uri(messages, uri)
        self.assertEqual(len(updated), 0)

    def test_new_walnut_triggers_list_changed(self) -> None:
        """Creating ``<new>/_kernel/key.md`` fires ``list_changed``."""
        from pydantic import AnyUrl

        # Subscribe to SOMETHING so the list_changed emission passes
        # the ``any_subscribers`` filter. (Per spec: only emit if a
        # client is subscribed to at least one resource.)
        uri = encode_kernel_uri("04_Ventures/alive", "log")
        messages: List[Any] = []

        async def factory(client: Any) -> Any:
            await client.subscribe_resource(AnyUrl(uri))
            new_key = (
                self.world_root
                / "04_Ventures/fresh-walnut/_kernel/key.md"
            )
            new_key.parent.mkdir(parents=True, exist_ok=True)
            new_key.write_text(
                "---\ntype: venture\n---\n\n# Fresh walnut\n"
            )
            await asyncio.sleep(self._wait_long_enough_for_debounce())
            return None

        _run_with_world_and_messages(self.world_root, factory, messages)
        list_changed = _filter_list_changed(messages)
        self.assertGreaterEqual(len(list_changed), 1)

    def test_rapid_writes_coalesce(self) -> None:
        """Many writes within the debounce window -> few notifications."""
        from pydantic import AnyUrl

        uri = encode_kernel_uri("04_Ventures/alive", "log")
        messages: List[Any] = []

        async def factory(client: Any) -> Any:
            await client.subscribe_resource(AnyUrl(uri))
            log_path = (
                self.world_root / "04_Ventures/alive/_kernel/log.md"
            )
            # 20 rapid rewrites. Debounce (500ms) should coalesce
            # these into ONE emission once the storm subsides.
            for i in range(20):
                log_path.write_text("churn #{}\n".format(i))
            await asyncio.sleep(self._wait_long_enough_for_debounce())
            return None

        _run_with_world_and_messages(self.world_root, factory, messages)
        updated = _filter_updated_for_uri(messages, uri)
        # Allow slight slack -- some OSes emit a handful of events
        # across the 500ms window even with debouncing (large writes
        # chunking into multiple FSEvent batches). The key property
        # is "not 20"; the acceptance bullet says "single debounced
        # notification (not 10)".
        self.assertGreater(len(updated), 0)
        self.assertLessEqual(
            len(updated),
            3,
            msg="expected debounced emission to collapse 20 writes; "
            "got {} notifications".format(len(updated)),
        )


# ---------------------------------------------------------------------------
# Server integration sanity.
# ---------------------------------------------------------------------------


class ServerIntegrationTests(unittest.TestCase):
    """AppContext + lifespan wiring for the subscription stack."""

    def test_app_context_has_registry_on_construction(self) -> None:
        from alive_mcp.server import AppContext

        ctx = AppContext()
        # Registry must exist before the observer starts so
        # subscribe handlers can safely write to it.
        self.assertIsNotNone(ctx.subscription_registry)

    def test_build_server_installs_subscribe_handlers(self) -> None:
        """``build_server`` -> lifespan must register subscribe handlers."""
        # We can't easily observe lifespan side-effects without running
        # the server; but we can assert the subscribe/unsubscribe
        # handlers install once a session runs end-to-end.
        from mcp import types as mcp_types

        world_root, cleanup = _new_world()
        self.addCleanup(cleanup)
        _make_walnut(world_root, "04_Ventures/alive")

        observed_handlers: dict[Any, Any] = {}

        async def factory(client: Any) -> Any:
            from alive_mcp.server import build_server  # noqa: F401

            # By the time the factory runs, the server lifespan has
            # executed and (via the subscribe_resource_handler seam)
            # installed the request handlers. Send a real subscribe
            # request to verify end-to-end -- a missing handler would
            # either 404 (method not found) or hang; the empty-result
            # success path is the contract.
            from pydantic import AnyUrl

            uri = encode_kernel_uri("04_Ventures/alive", "log")
            result = await client.subscribe_resource(AnyUrl(uri))
            observed_handlers["subscribe"] = result
            result2 = await client.unsubscribe_resource(AnyUrl(uri))
            observed_handlers["unsubscribe"] = result2
            return None

        _run_with_world_and_messages(world_root, factory, [])
        self.assertIn("subscribe", observed_handlers)
        self.assertIn("unsubscribe", observed_handlers)
        # Both handlers return EmptyResult.
        # ``subscribe_resource`` returns ``types.EmptyResult``; the
        # exact type is less interesting than "no error raised".
        self.assertIsNotNone(observed_handlers["subscribe"])
        self.assertIsNotNone(observed_handlers["unsubscribe"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
