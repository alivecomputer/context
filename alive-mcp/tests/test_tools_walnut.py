"""Unit tests for the walnut-centric MCP tools (fn-10-60k.6 / T6).

Covers the three walnut tools exposed in :mod:`alive_mcp.tools.walnut`:

* ``list_walnuts`` -- inventory, pagination, domain field, health signal.
* ``get_walnut_state`` -- v3 / v2 fallback, unknown walnut, bad payload.
* ``read_walnut_kernel`` -- four kernel files, v3/v2 fallback for ``now``,
  ``ERR_WALNUT_NOT_FOUND``, ``ERR_KERNEL_FILE_MISSING``.

Plus the two infrastructure pieces:

* ``@audited`` stub preserves signatures, tool-name override, sync+async.
* Registration wires the three tools onto a FastMCP instance with
  ``readOnlyHint=True`` annotations.

Tests exercise tool handlers directly (not via the stdio JSON-RPC
round-trip) so the assertions operate on plain envelope dicts, which
keeps the suite stdlib-only and fast. The ``build_server()`` registration
test IS an end-to-end shape check against FastMCP's tool registry.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock

# Make ``src/`` importable the same way tests/__init__.py does.
import tests  # noqa: F401

from alive_mcp import errors  # noqa: E402
from alive_mcp.tools import walnut as walnut_tools  # noqa: E402
from alive_mcp.tools._audit_stub import audited  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture world builder. Each test gets a fresh tempdir with a small
# ALIVE world layout so tests don't share state.
# ---------------------------------------------------------------------------


@dataclass
class FixtureWorld:
    """Handle + paths for a temp ALIVE world used in a single test."""

    root: pathlib.Path
    cleanup: Any

    def walnut_path(self, rel: str) -> pathlib.Path:
        return self.root / rel

    def write_kernel_file(self, walnut_rel: str, name: str, content: str) -> pathlib.Path:
        target = self.walnut_path(walnut_rel) / "_kernel" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_v2_now(self, walnut_rel: str, data: dict[str, Any]) -> pathlib.Path:
        target = (
            self.walnut_path(walnut_rel)
            / "_kernel"
            / "_generated"
            / "now.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data), encoding="utf-8")
        return target


def _make_walnut(
    world: FixtureWorld,
    rel: str,
    *,
    goal: str = "",
    rhythm: str = "",
    updated: Optional[str] = None,
    phase: str = "building",
    include_log: bool = False,
    include_insights: bool = False,
) -> None:
    """Create a walnut under ``world.root`` with minimal frontmatter + state."""
    fm_lines = ["---"]
    fm_lines.append("type: venture")
    if goal:
        fm_lines.append("goal: {}".format(goal))
    if rhythm:
        fm_lines.append("rhythm: {}".format(rhythm))
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append("# {}".format(rel))
    world.write_kernel_file(rel, "key.md", "\n".join(fm_lines))

    if updated is not None:
        now_data = {"phase": phase, "updated": updated, "next": None}
        world.write_kernel_file(
            rel, "now.json", json.dumps(now_data)
        )

    if include_log:
        world.write_kernel_file(
            rel,
            "log.md",
            (
                "---\nwalnut: {name}\n---\n\n## 2026-04-16 squirrel:abcdef01\n\n"
                "Log content.\n\nsigned: squirrel:abcdef01\n"
            ).format(name=os.path.basename(rel)),
        )
    if include_insights:
        world.write_kernel_file(
            rel,
            "insights.md",
            "---\nsections: []\n---\n\n# Insights\n\nStanding knowledge.\n",
        )


def _new_world() -> FixtureWorld:
    tmpdir = tempfile.mkdtemp(prefix="alive-mcp-test-")
    root = pathlib.Path(tmpdir)
    # Mark as ALIVE world with the .alive sentinel.
    (root / ".alive").mkdir()
    return FixtureWorld(root=root, cleanup=lambda: shutil.rmtree(tmpdir, ignore_errors=True))


class _FakeLifespan:
    """Stand-in for :class:`AppContext` used by tools.

    The tools only read ``world_root`` off whatever ``lifespan_context``
    returns, so the stub needs exactly one attribute. Using a
    dataclass-esque shim avoids importing the real AppContext (which
    pulls in asyncio.Queue and watchdog transitively) into the test.
    """

    def __init__(self, world_root: Optional[str]) -> None:
        self.world_root = world_root


class _FakeRequestContext:
    def __init__(self, world_root: Optional[str]) -> None:
        self.lifespan_context = _FakeLifespan(world_root=world_root)


def _fake_ctx(world_root: Optional[str]) -> Any:
    """Build a minimal Context-like object exposing only request_context."""
    ctx = MagicMock()
    ctx.request_context = _FakeRequestContext(world_root)
    return ctx


def _run(coro: Any) -> Any:
    """Run an async coroutine and return the result. asyncio.run-friendly."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Audit stub tests.
# ---------------------------------------------------------------------------


class AuditStubPreservesSemantics(unittest.TestCase):
    """The stub decorator is a pass-through with stable metadata.

    T12 replaces the body with the JSONL writer; these tests lock the
    signature and name surface so a replacement that regresses them
    fails loudly.
    """

    def test_sync_function_forwards_args_and_result(self) -> None:
        @audited
        def add(a: int, b: int) -> int:
            return a + b

        self.assertEqual(add(2, 3), 5)
        # functools.wraps preserves __name__.
        self.assertEqual(add.__name__, "add")
        # Tool name defaults to the wrapped function name.
        self.assertEqual(getattr(add, "__alive_tool_name__"), "add")

    def test_async_function_forwards_args_and_result(self) -> None:
        @audited
        async def mul(a: int, b: int) -> int:
            return a * b

        self.assertEqual(_run(mul(4, 5)), 20)
        self.assertEqual(mul.__name__, "mul")

    def test_tool_name_override(self) -> None:
        @audited(tool_name="public_name")
        async def _private_impl(x: int) -> int:
            return x + 1

        self.assertEqual(_run(_private_impl(1)), 2)
        # Wrapped function keeps its defined name for debugging...
        self.assertEqual(_private_impl.__name__, "_private_impl")
        # ...but the audit side carries the override.
        self.assertEqual(
            getattr(_private_impl, "__alive_tool_name__"),
            "public_name",
        )

    def test_decorator_does_not_raise_on_failing_func(self) -> None:
        @audited
        async def explode() -> None:
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            _run(explode())


# ---------------------------------------------------------------------------
# Cursor codec tests.
# ---------------------------------------------------------------------------


class CursorCodec(unittest.TestCase):
    """Cursor is opaque but must round-trip integer offsets."""

    def test_roundtrip(self) -> None:
        for offset in (0, 1, 42, 999, 10_000):
            token = walnut_tools._encode_cursor(offset)
            self.assertEqual(walnut_tools._decode_cursor(token), offset)

    def test_none_and_empty_decode_to_zero(self) -> None:
        self.assertEqual(walnut_tools._decode_cursor(None), 0)
        self.assertEqual(walnut_tools._decode_cursor(""), 0)

    def test_malformed_raises_invalid_cursor(self) -> None:
        with self.assertRaises(errors.InvalidCursorError):
            walnut_tools._decode_cursor("!!!not-base64!!!")
        # Non-integer body.
        bad = base64.urlsafe_b64encode(b"hello").rstrip(b"=").decode("ascii")
        with self.assertRaises(errors.InvalidCursorError):
            walnut_tools._decode_cursor(bad)

    def test_negative_offset_raises(self) -> None:
        bad = base64.urlsafe_b64encode(b"-5").rstrip(b"=").decode("ascii")
        with self.assertRaises(errors.InvalidCursorError):
            walnut_tools._decode_cursor(bad)


# ---------------------------------------------------------------------------
# list_walnuts tests.
# ---------------------------------------------------------------------------


class ListWalnutsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)

        # Three walnuts across three domains plus one under People/
        # to exercise the domain=None case.
        _make_walnut(
            self.world,
            "04_Ventures/nova-station",
            goal="Build the rocket",
            rhythm="weekly",
            updated="2026-04-15T12:00:00Z",
        )
        _make_walnut(
            self.world,
            "02_Life/people/ben-flint",
            goal="Keep Ben updated",
            rhythm="weekly",
            updated="2026-04-10T12:00:00Z",
        )
        _make_walnut(
            self.world,
            "05_Experiments/lock-in-lab",
            goal="Run the experiment",
            rhythm="monthly",
            updated="2026-01-01T12:00:00Z",  # older -> waiting
        )
        _make_walnut(
            self.world,
            "People/ryn-okata",
            goal="Engineering lead",
            rhythm="biweekly",
            updated="2026-04-14T12:00:00Z",
        )

    def _call(self, **kwargs: Any) -> dict[str, Any]:
        ctx = _fake_ctx(str(self.world.root))
        return _run(walnut_tools.list_walnuts(ctx, **kwargs))

    def test_returns_envelope_with_walnuts(self) -> None:
        envelope = self._call()
        self.assertFalse(envelope["isError"])
        structured = envelope["structuredContent"]
        self.assertEqual(structured["total"], 4)
        self.assertIsNone(structured["next_cursor"])
        walnuts = structured["walnuts"]
        self.assertEqual(len(walnuts), 4)

    def test_records_shape_matches_spec(self) -> None:
        envelope = self._call()
        walnut = envelope["structuredContent"]["walnuts"][0]
        self.assertEqual(
            set(walnut.keys()),
            {"path", "name", "domain", "goal", "health", "updated"},
        )

    def test_domain_is_first_path_segment(self) -> None:
        envelope = self._call()
        mapping = {
            w["path"]: w["domain"]
            for w in envelope["structuredContent"]["walnuts"]
        }
        self.assertEqual(mapping["04_Ventures/nova-station"], "04_Ventures")
        self.assertEqual(mapping["02_Life/people/ben-flint"], "02_Life")
        self.assertEqual(mapping["05_Experiments/lock-in-lab"], "05_Experiments")
        # People/ sits outside the ALIVE numbering; domain is None.
        self.assertIsNone(mapping["People/ryn-okata"])

    def test_path_is_canonical_identifier(self) -> None:
        envelope = self._call()
        for w in envelope["structuredContent"]["walnuts"]:
            # name is display-only; path is POSIX-relpath ending in name.
            self.assertEqual(
                pathlib.PurePosixPath(w["path"]).name,
                w["name"],
            )

    def test_sorted_by_posix_path(self) -> None:
        envelope = self._call()
        paths = [w["path"] for w in envelope["structuredContent"]["walnuts"]]
        self.assertEqual(paths, sorted(paths))

    def test_health_is_computed_from_rhythm_and_updated(self) -> None:
        envelope = self._call()
        by_path = {
            w["path"]: w
            for w in envelope["structuredContent"]["walnuts"]
        }
        # Old updated (2026-01-01) with monthly rhythm -> waiting (past 2x).
        self.assertEqual(by_path["05_Experiments/lock-in-lab"]["health"], "waiting")

    def test_health_unknown_when_missing_rhythm(self) -> None:
        _make_walnut(self.world, "04_Ventures/no-rhythm", goal="x", updated="2026-04-15T12:00:00Z")
        envelope = self._call()
        by_path = {
            w["path"]: w
            for w in envelope["structuredContent"]["walnuts"]
        }
        self.assertEqual(by_path["04_Ventures/no-rhythm"]["health"], "unknown")

    def test_pagination_emits_cursor_and_limits(self) -> None:
        envelope = self._call(limit=2)
        structured = envelope["structuredContent"]
        self.assertEqual(len(structured["walnuts"]), 2)
        self.assertEqual(structured["total"], 4)
        self.assertIsNotNone(structured["next_cursor"])
        # Decoded cursor must be 2 (offset into sorted list).
        self.assertEqual(
            walnut_tools._decode_cursor(structured["next_cursor"]),
            2,
        )

        # Next page uses the cursor.
        env2 = self._call(limit=2, cursor=structured["next_cursor"])
        self.assertEqual(len(env2["structuredContent"]["walnuts"]), 2)
        self.assertIsNone(env2["structuredContent"]["next_cursor"])

    def test_no_world_returns_err_no_world(self) -> None:
        ctx = _fake_ctx(None)
        envelope = _run(walnut_tools.list_walnuts(ctx))
        self.assertTrue(envelope["isError"])
        self.assertEqual(
            envelope["structuredContent"]["error"],
            "NO_WORLD",
        )

    def test_invalid_cursor_returns_error(self) -> None:
        envelope = self._call(cursor="@@@-not-a-cursor")
        self.assertTrue(envelope["isError"])
        self.assertEqual(
            envelope["structuredContent"]["error"],
            "INVALID_CURSOR",
        )

    def test_cursor_past_end_returns_empty(self) -> None:
        envelope = self._call(
            cursor=walnut_tools._encode_cursor(999), limit=10
        )
        self.assertFalse(envelope["isError"])
        self.assertEqual(envelope["structuredContent"]["walnuts"], [])
        self.assertIsNone(envelope["structuredContent"]["next_cursor"])

    def test_symlinked_key_md_escape_not_counted_as_walnut(self) -> None:
        """A walnut whose key.md symlinks outside the World is filtered.

        Security regression guard: without the symlink-realpath check
        in ``_kernel_file_in_world``, a directory containing a
        ``_kernel/key.md`` symlink to ``/etc/passwd`` (or any path
        outside the authorized World root) would be treated as a
        walnut and its content exposed via read_walnut_kernel. The
        openWorldHint=False contract forbids this.
        """
        # Create a bait "walnut" at 04_Ventures/bait whose key.md is
        # a symlink to a file OUTSIDE the world.
        bait_kernel = self.world.walnut_path("04_Ventures/bait") / "_kernel"
        bait_kernel.mkdir(parents=True, exist_ok=True)
        outside_target = pathlib.Path(
            tempfile.mkstemp(prefix="alive-mcp-escape-")[1]
        )
        outside_target.write_text("fake secret\n")
        self.addCleanup(lambda: outside_target.unlink(missing_ok=True))
        (bait_kernel / "key.md").symlink_to(outside_target)

        env = self._call()
        paths = [
            w["path"]
            for w in env["structuredContent"]["walnuts"]
        ]
        # The bait walnut must NOT appear in the listing.
        self.assertNotIn("04_Ventures/bait", paths)

    def test_permission_error_on_root_returns_permission_denied(self) -> None:
        """A World root that can't be listed surfaces as ERR_PERMISSION_DENIED.

        Previously the traversal silently returned an empty list,
        masking the permission failure as ``total=0``. Now the error
        propagates to the envelope so clients can see it and react.
        """
        import os as _os

        # Use a temp dir and strip read permission. On macOS, 0o000
        # still allows the owner to re-chmod, so cleanup is safe.
        restricted = tempfile.mkdtemp(prefix="alive-mcp-noread-")
        self.addCleanup(lambda: (
            _os.chmod(restricted, 0o700),
            shutil.rmtree(restricted, ignore_errors=True),
        ))
        _os.chmod(restricted, 0o000)

        # Build a ctx pointing at the restricted dir. We can't use
        # self.world here because setUp already set up a valid world.
        ctx = _fake_ctx(restricted)
        env = _run(walnut_tools.list_walnuts(ctx))
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "PERMISSION_DENIED"
        )

    def test_under_1s_at_43_walnut_scale(self) -> None:
        # The epic's acceptance target: <1s at 43 walnuts. Build that
        # many in the fixture and time the list call.
        for i in range(40):  # plus the 4 set up in setUp -> 44 total
            _make_walnut(
                self.world,
                "04_Ventures/walnut-{:02d}".format(i),
                goal="w{}".format(i),
                rhythm="weekly",
                updated="2026-04-15T12:00:00Z",
            )
        import time
        start = time.monotonic()
        env = self._call(limit=100)
        duration = time.monotonic() - start
        self.assertFalse(env["isError"])
        self.assertGreaterEqual(env["structuredContent"]["total"], 43)
        self.assertLess(duration, 1.0, "list_walnuts took {:.3f}s".format(duration))


# ---------------------------------------------------------------------------
# get_walnut_state tests.
# ---------------------------------------------------------------------------


class GetWalnutStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(
            self.world,
            "04_Ventures/alive",
            goal="Build ALIVE",
            rhythm="weekly",
            updated="2026-04-15T12:00:00Z",
            phase="building",
        )

    def _call(self, walnut: str) -> dict[str, Any]:
        ctx = _fake_ctx(str(self.world.root))
        return _run(walnut_tools.get_walnut_state(ctx, walnut))

    def test_reads_v3_now_json(self) -> None:
        env = self._call("04_Ventures/alive")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(env["structuredContent"]["phase"], "building")
        self.assertEqual(env["structuredContent"]["updated"], "2026-04-15T12:00:00Z")

    def test_v2_fallback(self) -> None:
        # Make a walnut with ONLY the v2 layout (no v3 now.json).
        _make_walnut(self.world, "04_Ventures/legacy", goal="x")
        self.world.write_v2_now(
            "04_Ventures/legacy",
            {"phase": "maintaining", "updated": "2026-03-01T00:00:00Z"},
        )
        env = self._call("04_Ventures/legacy")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(env["structuredContent"]["phase"], "maintaining")

    def test_v3_wins_over_v2(self) -> None:
        # Walnut has both; v3 takes precedence.
        _make_walnut(
            self.world,
            "04_Ventures/dual",
            goal="x",
            updated="2026-04-01T00:00:00Z",
            phase="v3-win",
        )
        self.world.write_v2_now(
            "04_Ventures/dual",
            {"phase": "v2-lose", "updated": "2026-03-01T00:00:00Z"},
        )
        env = self._call("04_Ventures/dual")
        self.assertEqual(env["structuredContent"]["phase"], "v3-win")

    def test_missing_both_returns_kernel_file_missing(self) -> None:
        # Walnut exists but has no now.json in either layout.
        _make_walnut(self.world, "04_Ventures/fresh", goal="brand new")
        env = self._call("04_Ventures/fresh")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"],
            "KERNEL_FILE_MISSING",
        )

    def test_unknown_walnut_returns_walnut_not_found(self) -> None:
        env = self._call("04_Ventures/does-not-exist")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"],
            "WALNUT_NOT_FOUND",
        )

    def test_path_escape_returns_error(self) -> None:
        env = self._call("../outside")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"],
            "PATH_ESCAPE",
        )

    def test_malformed_now_json_returns_kernel_file_missing(self) -> None:
        import warnings as _warnings

        _make_walnut(self.world, "04_Ventures/corrupt", goal="x")
        # Write intentionally invalid JSON.
        corrupt = (
            self.world.walnut_path("04_Ventures/corrupt")
            / "_kernel"
            / "now.json"
        )
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("{ not valid json", encoding="utf-8")
        with _warnings.catch_warnings():
            # parse_now_json emits MalformedYAMLWarning on bad JSON --
            # that's the expected/correct behavior here, so silence
            # it to keep unittest -v output clean.
            _warnings.simplefilter("ignore")
            env = self._call("04_Ventures/corrupt")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"],
            "KERNEL_FILE_MISSING",
        )

    def test_malformed_v3_falls_through_to_v2(self) -> None:
        """A corrupt v3 now.json must NOT mask a valid v2 fallback.

        Mirrors the posture of
        ``project_pure.scan_nested_walnuts``: if the v3 path exists but
        is malformed, the tool tries the v2 _generated fallback before
        giving up. This fixes a nitpick from codex round 1.
        """
        import warnings as _warnings

        _make_walnut(self.world, "04_Ventures/split", goal="x")
        # v3 is corrupt JSON.
        bad_v3 = (
            self.world.walnut_path("04_Ventures/split")
            / "_kernel"
            / "now.json"
        )
        bad_v3.parent.mkdir(parents=True, exist_ok=True)
        bad_v3.write_text("{ not valid json", encoding="utf-8")
        # v2 is valid.
        self.world.write_v2_now(
            "04_Ventures/split",
            {"phase": "maintaining", "updated": "2026-03-01T00:00:00Z"},
        )
        # Silence the MalformedYAMLWarning emitted by parse_now_json;
        # it's expected given the malformed v3 above.
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            env = self._call("04_Ventures/split")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(
            env["structuredContent"]["phase"], "maintaining"
        )

    def test_now_json_non_dict_root_returns_missing(self) -> None:
        import warnings as _warnings

        _make_walnut(self.world, "04_Ventures/weird", goal="x")
        weird = (
            self.world.walnut_path("04_Ventures/weird")
            / "_kernel"
            / "now.json"
        )
        weird.parent.mkdir(parents=True, exist_ok=True)
        weird.write_text("[1, 2, 3]", encoding="utf-8")
        with _warnings.catch_warnings():
            # parse_now_json emits MalformedYAMLWarning on a non-dict
            # root -- expected here, silence for clean test output.
            _warnings.simplefilter("ignore")
            env = self._call("04_Ventures/weird")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"],
            "KERNEL_FILE_MISSING",
        )

    def test_no_world(self) -> None:
        ctx = _fake_ctx(None)
        env = _run(walnut_tools.get_walnut_state(ctx, "04_Ventures/alive"))
        self.assertTrue(env["isError"])
        self.assertEqual(env["structuredContent"]["error"], "NO_WORLD")

    def test_unknown_walnut_includes_fuzzy_suggestions(self) -> None:
        """ERR_WALNUT_NOT_FOUND includes fuzzy-matched paths in suggestions.

        Acceptance criterion from the task spec: "Unknown walnut path ->
        ERR_WALNUT_NOT_FOUND with suggestions populated from fuzzy
        match on path tail."
        """
        env = self._call("04_Ventures/aliv")  # tail typo
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "WALNUT_NOT_FOUND"
        )
        suggestions = env["structuredContent"]["suggestions"]
        # First suggestion is the "Did you mean" hint with near-matches.
        self.assertGreaterEqual(len(suggestions), 1)
        self.assertIn("Did you mean", suggestions[0])
        self.assertIn("04_Ventures/alive", suggestions[0])


# ---------------------------------------------------------------------------
# read_walnut_kernel tests.
# ---------------------------------------------------------------------------


class ReadWalnutKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(
            self.world,
            "04_Ventures/alive",
            goal="Build ALIVE",
            rhythm="weekly",
            updated="2026-04-15T12:00:00Z",
            include_log=True,
            include_insights=True,
        )

    def _call(self, walnut: str, file: str) -> dict[str, Any]:
        ctx = _fake_ctx(str(self.world.root))
        return _run(walnut_tools.read_walnut_kernel(ctx, walnut, file))

    def test_read_key_returns_markdown(self) -> None:
        env = self._call("04_Ventures/alive", "key")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(env["structuredContent"]["mime"], "text/markdown")
        self.assertIn("Build ALIVE", env["structuredContent"]["content"])

    def test_read_log_returns_markdown_whole_file(self) -> None:
        env = self._call("04_Ventures/alive", "log")
        self.assertFalse(env["isError"], msg=env)
        content = env["structuredContent"]["content"]
        # Log has frontmatter + one entry; both must be present verbatim.
        self.assertIn("## 2026-04-16 squirrel:abcdef01", content)
        self.assertIn("signed: squirrel:abcdef01", content)
        self.assertEqual(env["structuredContent"]["mime"], "text/markdown")

    def test_read_insights_returns_markdown(self) -> None:
        env = self._call("04_Ventures/alive", "insights")
        self.assertFalse(env["isError"], msg=env)
        self.assertIn("Standing knowledge", env["structuredContent"]["content"])

    def test_read_now_returns_json(self) -> None:
        env = self._call("04_Ventures/alive", "now")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(env["structuredContent"]["mime"], "application/json")
        # The raw text round-trips through json.loads.
        parsed = json.loads(env["structuredContent"]["content"])
        self.assertEqual(parsed["phase"], "building")

    def test_read_now_falls_back_to_v2(self) -> None:
        _make_walnut(self.world, "04_Ventures/legacy", goal="x")
        self.world.write_v2_now(
            "04_Ventures/legacy",
            {"phase": "maintaining", "updated": "2026-03-01T00:00:00Z"},
        )
        env = self._call("04_Ventures/legacy", "now")
        self.assertFalse(env["isError"], msg=env)
        parsed = json.loads(env["structuredContent"]["content"])
        self.assertEqual(parsed["phase"], "maintaining")

    def test_missing_kernel_file(self) -> None:
        # Walnut exists but has no insights.md.
        _make_walnut(self.world, "04_Ventures/bare", goal="x")
        env = self._call("04_Ventures/bare", "insights")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"],
            "KERNEL_FILE_MISSING",
        )

    def test_unknown_walnut(self) -> None:
        env = self._call("04_Ventures/does-not-exist", "key")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"],
            "WALNUT_NOT_FOUND",
        )

    def test_no_world(self) -> None:
        ctx = _fake_ctx(None)
        env = _run(
            walnut_tools.read_walnut_kernel(ctx, "04_Ventures/alive", "key")
        )
        self.assertTrue(env["isError"])
        self.assertEqual(env["structuredContent"]["error"], "NO_WORLD")

    def test_unknown_walnut_includes_fuzzy_suggestions(self) -> None:
        """``read_walnut_kernel`` mirrors ``get_walnut_state``'s suggestion wiring.

        Same acceptance requirement applies to every read tool that can
        emit ``ERR_WALNUT_NOT_FOUND``.
        """
        env = self._call("04_Ventures/aliv", "key")  # tail typo
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "WALNUT_NOT_FOUND"
        )
        suggestions = env["structuredContent"]["suggestions"]
        self.assertGreaterEqual(len(suggestions), 1)
        self.assertIn("Did you mean", suggestions[0])
        self.assertIn("04_Ventures/alive", suggestions[0])

    def test_symlinked_insights_pointing_outside_is_rejected(self) -> None:
        """A symlinked kernel file escaping the World is filtered as missing.

        Security regression guard: without the containment check, a
        walnut owner pointing ``_kernel/insights.md`` at
        ``/etc/passwd`` could serve the file via read_walnut_kernel.
        The tool must refuse to read kernel files whose realpath
        leaves the World -- indistinguishable from "missing" at the
        client layer by design (we do NOT leak that the file exists
        but escapes).
        """
        # /04_Ventures/alive is the valid walnut from setUp, with
        # insights.md already written. Replace that file with a
        # symlink to an out-of-world target.
        good_path = (
            self.world.walnut_path("04_Ventures/alive")
            / "_kernel"
            / "insights.md"
        )
        good_path.unlink()
        outside_target = pathlib.Path(
            tempfile.mkstemp(prefix="alive-mcp-escape-insights-")[1]
        )
        outside_target.write_text("secret content\n")
        self.addCleanup(lambda: outside_target.unlink(missing_ok=True))
        good_path.symlink_to(outside_target)

        env = self._call("04_Ventures/alive", "insights")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "KERNEL_FILE_MISSING"
        )

    def test_whole_file_not_paginated(self) -> None:
        # Write a long log and verify we get every byte back. Pagination
        # is read_log's job (T9), not this tool.
        long_body = "\n".join("line {:04d}".format(i) for i in range(2_000))
        log_text = (
            "---\nwalnut: big\n---\n\n## 2026-04-16 squirrel:abcdef01\n\n"
            + long_body
            + "\n\nsigned: squirrel:abcdef01\n"
        )
        _make_walnut(self.world, "04_Ventures/big", goal="x")
        self.world.write_kernel_file("04_Ventures/big", "log.md", log_text)
        env = self._call("04_Ventures/big", "log")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(env["structuredContent"]["content"], log_text)


# ---------------------------------------------------------------------------
# Registration / annotations tests.
# ---------------------------------------------------------------------------


class ToolRegistrationTests(unittest.TestCase):
    """``build_server`` registers the three walnut tools with readOnlyHint=True.

    Uses the real FastMCP instance so any SDK drift in the tool
    registration path surfaces here.
    """

    def test_walnut_tools_are_registered_with_readonly_hint(self) -> None:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations

        server = FastMCP(name="test-registration")
        walnut_tools.register(server)

        # FastMCP exposes the tool registry via list_tools (async) and
        # via its internal _tool_manager. We read the internal map
        # because it's the simplest way to inspect annotations without
        # running the full stdio stack.
        registry = server._tool_manager._tools  # type: ignore[attr-defined]
        expected = {"list_walnuts", "get_walnut_state", "read_walnut_kernel"}
        self.assertLessEqual(expected, set(registry.keys()))

        for name in expected:
            with self.subTest(tool=name):
                tool = registry[name]
                ann = getattr(tool, "annotations", None)
                self.assertIsNotNone(ann, msg="tool {} has no annotations".format(name))
                assert ann is not None  # for type-checker
                self.assertTrue(ann.readOnlyHint)
                self.assertFalse(ann.destructiveHint)
                self.assertFalse(ann.openWorldHint)

    def test_full_build_server_registers_walnut_tools(self) -> None:
        """End-to-end: the real build_server() wires the walnut tools up.

        The bootstrap test (test_server_bootstrap.py) covers the
        capabilities + lifespan side; this one proves T6's tools land
        on the instance the CLI ``alive-mcp`` actually returns.
        """
        from alive_mcp.server import build_server

        server = build_server()
        registry = server._tool_manager._tools  # type: ignore[attr-defined]
        expected = {"list_walnuts", "get_walnut_state", "read_walnut_kernel"}
        self.assertLessEqual(expected, set(registry.keys()))

    def test_read_walnut_kernel_file_enum_is_schema_enforced(self) -> None:
        """Invalid ``file`` values are rejected at the FastMCP schema boundary.

        Acceptance criterion: "Invalid `file` (e.g. `"foo"`) ->
        ERR_VALIDATION (schema-enforced literal)". FastMCP translates
        the pydantic literal violation into a ``ToolError`` before the
        tool handler is invoked, which is the MCP equivalent of
        schema-level validation failure -- the handler never sees the
        bad value.
        """
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        server = FastMCP(name="test-schema-enforcement")
        walnut_tools.register(server)

        # Assert the schema itself carries the enum literal so the
        # contract is visible to clients' schema inspection too.
        tool = server._tool_manager._tools["read_walnut_kernel"]  # type: ignore[attr-defined]
        file_prop = tool.parameters["properties"]["file"]
        self.assertEqual(
            set(file_prop["enum"]),
            {"key", "log", "insights", "now"},
        )

        async def _call_bad() -> Any:
            return await server._tool_manager.call_tool(  # type: ignore[attr-defined]
                "read_walnut_kernel",
                {"walnut": "04_Ventures/anything", "file": "foo"},
                context=None,
            )

        with self.assertRaises(ToolError):
            _run(_call_bad())


if __name__ == "__main__":
    unittest.main()
