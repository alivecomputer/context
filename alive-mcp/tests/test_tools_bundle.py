"""Unit tests for the bundle-centric MCP tools (fn-10-60k.7 / T7).

Covers the three bundle tools exposed in :mod:`alive_mcp.tools.bundle`:

* ``list_bundles`` -- walnut-scoped inventory, 8-key subset, layout
  tolerance (v3 flat / v2 nested / v1 legacy).
* ``get_bundle`` -- 9-key manifest plus derived counts, error paths.
* ``read_bundle_manifest`` -- manifest + warnings for missing keys.

Plus the registration contract: ``build_server`` wires the three
tools with ``readOnlyHint=True``.

Tests exercise handlers directly (not through stdio) so assertions
run against plain envelope dicts -- keeps the suite stdlib-only and
fast. Mirrors the pattern in ``test_tools_walnut.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock

# Make ``src/`` importable the same way tests/__init__.py does.
import tests  # noqa: F401

from alive_mcp import errors  # noqa: E402
from alive_mcp.tools import bundle as bundle_tools  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture world builder -- a walnut with a handful of bundles across
# the three supported layouts.
# ---------------------------------------------------------------------------


MANIFEST_FULL = textwrap.dedent(
    """\
    name: Shielding Review
    goal: Lock the vendor and produce the v1 brief
    outcome: v1 brief published
    status: draft
    phase: research
    updated: 2026-04-15
    due: 2026-04-30
    context: |
      Vendor shortlist narrowed to two options.
      Waiting on test telemetry from March 4 window.
    squirrels:
      - a8c95e9
      - bc96e49c
    """
)


MANIFEST_MINIMAL = textwrap.dedent(
    """\
    goal: One-off prototype
    status: prototype
    """
)


MANIFEST_EMPTY = ""


@dataclass
class FixtureWorld:
    """Temp ALIVE world + helpers for single-test construction."""

    root: pathlib.Path
    cleanup: Any

    def walnut_path(self, rel: str) -> pathlib.Path:
        return self.root / rel

    def write_kernel_file(self, walnut_rel: str, name: str, content: str) -> pathlib.Path:
        target = self.walnut_path(walnut_rel) / "_kernel" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_bundle(
        self,
        walnut_rel: str,
        bundle_rel: str,
        manifest_content: str,
        *,
        tasks: Optional[list[dict[str, Any]]] = None,
        raw_files: Optional[list[str]] = None,
    ) -> pathlib.Path:
        """Create a bundle directory with a manifest and optional extras.

        ``bundle_rel`` is walnut-relative (e.g. ``"bundles/foo"`` for
        v2 nested, ``"bar"`` for v3 flat, ``"_core/_capsules/baz"``
        for v1 legacy). Returns the bundle's absolute path.
        """
        bundle_dir = self.walnut_path(walnut_rel) / bundle_rel
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "context.manifest.yaml").write_text(
            manifest_content, encoding="utf-8"
        )
        if tasks is not None:
            (bundle_dir / "tasks.json").write_text(
                json.dumps({"tasks": tasks}), encoding="utf-8"
            )
        if raw_files:
            raw_dir = bundle_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            for fname in raw_files:
                (raw_dir / fname).write_text("raw", encoding="utf-8")
        return bundle_dir


def _make_walnut(world: FixtureWorld, rel: str) -> None:
    """Create a minimal walnut with just key.md so _resolve_walnut passes."""
    world.write_kernel_file(
        rel,
        "key.md",
        "---\ntype: venture\ngoal: test\n---\n\n# test walnut\n",
    )


def _new_world() -> FixtureWorld:
    tmpdir = tempfile.mkdtemp(prefix="alive-mcp-bundle-test-")
    root = pathlib.Path(tmpdir)
    # Mark as ALIVE world with the .alive sentinel.
    (root / ".alive").mkdir()
    return FixtureWorld(
        root=root,
        cleanup=lambda: shutil.rmtree(tmpdir, ignore_errors=True),
    )


class _FakeLifespan:
    def __init__(self, world_root: Optional[str]) -> None:
        self.world_root = world_root


class _FakeRequestContext:
    def __init__(self, world_root: Optional[str]) -> None:
        self.lifespan_context = _FakeLifespan(world_root=world_root)


def _fake_ctx(world_root: Optional[str]) -> Any:
    ctx = MagicMock()
    ctx.request_context = _FakeRequestContext(world_root)
    return ctx


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Normalization / projection helpers.
# ---------------------------------------------------------------------------


class NormalizeManifestTests(unittest.TestCase):
    def test_full_manifest_returns_all_nine_keys(self) -> None:
        parsed = {
            "name": "A",
            "goal": "G",
            "outcome": "O",
            "status": "draft",
            "phase": "research",
            "updated": "2026-04-15",
            "due": "2026-04-30",
            "context": "ctx",
            "active_sessions": ["abc123"],
        }
        out = bundle_tools._normalize_manifest(parsed)
        self.assertEqual(set(out.keys()), set(bundle_tools._MANIFEST_KEYS))
        self.assertEqual(out["name"], "A")
        self.assertEqual(out["active_sessions"], ["abc123"])

    def test_missing_scalars_backfilled_as_none(self) -> None:
        out = bundle_tools._normalize_manifest({"goal": "G"})
        self.assertEqual(out["goal"], "G")
        self.assertIsNone(out["name"])
        self.assertIsNone(out["status"])
        self.assertIsNone(out["updated"])
        # active_sessions defaults to empty list, not None.
        self.assertEqual(out["active_sessions"], [])

    def test_empty_dict_gives_all_null_scalars(self) -> None:
        out = bundle_tools._normalize_manifest({})
        for key in bundle_tools._MANIFEST_KEYS:
            if key == "active_sessions":
                self.assertEqual(out[key], [])
            else:
                self.assertIsNone(out[key])

    def test_none_input_treated_as_empty(self) -> None:
        out = bundle_tools._normalize_manifest(None)
        self.assertEqual(out["active_sessions"], [])
        self.assertIsNone(out["goal"])

    def test_empty_string_scalar_becomes_none(self) -> None:
        out = bundle_tools._normalize_manifest({"goal": ""})
        self.assertIsNone(out["goal"])


class ListSubsetTests(unittest.TestCase):
    def test_drops_context_and_active_sessions(self) -> None:
        nine = bundle_tools._normalize_manifest(
            {
                "name": "A",
                "goal": "G",
                "context": "ctx",
                "active_sessions": ["x"],
            }
        )
        subset = bundle_tools._list_subset(nine)
        self.assertEqual(set(subset.keys()), set(bundle_tools._LIST_BUNDLE_KEYS))
        self.assertNotIn("context", subset)
        self.assertNotIn("active_sessions", subset)
        self.assertEqual(subset["goal"], "G")


class CollectWarningsTests(unittest.TestCase):
    def test_empty_parser_result_warns_every_key(self) -> None:
        warnings = bundle_tools._collect_warnings({})
        # Every one of the 9 keys flagged.
        self.assertEqual(len(warnings), len(bundle_tools._MANIFEST_KEYS))
        for key in bundle_tools._MANIFEST_KEYS:
            self.assertIn("could not parse '{}'".format(key), warnings)

    def test_none_parser_result_warns_every_key(self) -> None:
        warnings = bundle_tools._collect_warnings(None)
        self.assertEqual(len(warnings), len(bundle_tools._MANIFEST_KEYS))

    def test_full_manifest_has_empty_warnings(self) -> None:
        parsed = {
            "name": "A",
            "goal": "G",
            "outcome": "O",
            "status": "draft",
            "phase": "research",
            "updated": "2026-04-15",
            "due": "2026-04-30",
            "context": "ctx",
            "active_sessions": ["abc"],
        }
        self.assertEqual(bundle_tools._collect_warnings(parsed), [])

    def test_missing_single_key_warns_that_key(self) -> None:
        parsed = {
            "name": "A",
            "goal": "G",
            "outcome": "O",
            "status": "draft",
            "phase": "research",
            "updated": "2026-04-15",
            "due": "2026-04-30",
            # context missing
            "active_sessions": ["abc"],
        }
        warnings = bundle_tools._collect_warnings(parsed)
        self.assertEqual(warnings, ["could not parse 'context'"])


# ---------------------------------------------------------------------------
# list_bundles tests.
# ---------------------------------------------------------------------------


class ListBundlesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(self.world, "04_Ventures/alive")
        # v3 flat bundle.
        self.world.write_bundle(
            "04_Ventures/alive",
            "alive-mcp-research",
            MANIFEST_FULL,
        )
        # v2 nested bundle.
        self.world.write_bundle(
            "04_Ventures/alive",
            "bundles/telemetry",
            MANIFEST_MINIMAL,
        )

    def _call(self, walnut: str) -> dict[str, Any]:
        ctx = _fake_ctx(str(self.world.root))
        return _run(bundle_tools.list_bundles(ctx, walnut))

    def test_returns_envelope_with_bundles(self) -> None:
        env = self._call("04_Ventures/alive")
        self.assertFalse(env["isError"], msg=env)
        bundles = env["structuredContent"]["bundles"]
        self.assertEqual(len(bundles), 2)

    def test_entry_shape_is_eight_keys(self) -> None:
        env = self._call("04_Ventures/alive")
        for b in env["structuredContent"]["bundles"]:
            self.assertEqual(
                set(b.keys()),
                {"path", "name", "goal", "status", "updated", "due",
                 "outcome", "phase"},
            )

    def test_list_view_drops_context_and_active_sessions(self) -> None:
        env = self._call("04_Ventures/alive")
        for b in env["structuredContent"]["bundles"]:
            self.assertNotIn("context", b)
            self.assertNotIn("active_sessions", b)

    def test_sorted_by_path(self) -> None:
        env = self._call("04_Ventures/alive")
        paths = [b["path"] for b in env["structuredContent"]["bundles"]]
        self.assertEqual(paths, sorted(paths))

    def test_layouts_surface_correctly(self) -> None:
        """v3 flat and v2 nested bundles both appear with correct paths.

        v1 legacy ``_core/_capsules/`` is NOT supported by the vendored
        ``find_bundles`` discovery (``_core`` is in its skip set -- an
        intentional alignment with the plugin's v3 migration posture).
        Callers needing v1-era capsules migrate them to v2/v3 first.
        """
        env = self._call("04_Ventures/alive")
        paths = {b["path"] for b in env["structuredContent"]["bundles"]}
        self.assertIn("alive-mcp-research", paths)  # v3 flat
        self.assertIn("bundles/telemetry", paths)   # v2 nested

    def test_values_populated_from_full_manifest(self) -> None:
        env = self._call("04_Ventures/alive")
        by_path = {
            b["path"]: b for b in env["structuredContent"]["bundles"]
        }
        entry = by_path["alive-mcp-research"]
        self.assertEqual(entry["name"], "Shielding Review")
        self.assertEqual(
            entry["goal"],
            "Lock the vendor and produce the v1 brief",
        )
        self.assertEqual(entry["status"], "draft")
        self.assertEqual(entry["updated"], "2026-04-15")
        self.assertEqual(entry["due"], "2026-04-30")
        self.assertEqual(entry["outcome"], "v1 brief published")
        self.assertEqual(entry["phase"], "research")

    def test_missing_values_are_null(self) -> None:
        env = self._call("04_Ventures/alive")
        by_path = {
            b["path"]: b for b in env["structuredContent"]["bundles"]
        }
        minimal = by_path["bundles/telemetry"]
        self.assertEqual(minimal["goal"], "One-off prototype")
        self.assertEqual(minimal["status"], "prototype")
        self.assertIsNone(minimal["name"])
        self.assertIsNone(minimal["updated"])
        self.assertIsNone(minimal["due"])
        self.assertIsNone(minimal["outcome"])
        self.assertIsNone(minimal["phase"])

    def test_walnut_not_found(self) -> None:
        env = self._call("04_Ventures/does-not-exist")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "WALNUT_NOT_FOUND"
        )

    def test_no_world(self) -> None:
        ctx = _fake_ctx(None)
        env = _run(bundle_tools.list_bundles(ctx, "04_Ventures/alive"))
        self.assertTrue(env["isError"])
        self.assertEqual(env["structuredContent"]["error"], "NO_WORLD")

    def test_path_escape(self) -> None:
        env = self._call("../outside")
        self.assertTrue(env["isError"])
        self.assertEqual(env["structuredContent"]["error"], "PATH_ESCAPE")

    def test_walnut_with_no_bundles_returns_empty_list(self) -> None:
        _make_walnut(self.world, "04_Ventures/bare")
        env = self._call("04_Ventures/bare")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(env["structuredContent"]["bundles"], [])

    def test_symlinked_bundle_escaping_world_is_dropped(self) -> None:
        """A bundle whose realpath leaves the World is filtered.

        Security regression guard: without the containment check, a
        walnut owner could symlink a bundle to
        ``/etc/some-directory`` and the tool would list it.
        """
        # Create a real bundle outside the World.
        outside = tempfile.mkdtemp(prefix="alive-mcp-outside-")
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        (pathlib.Path(outside) / "context.manifest.yaml").write_text(
            MANIFEST_MINIMAL, encoding="utf-8"
        )
        # Symlink it into the walnut.
        walnut_dir = self.world.walnut_path("04_Ventures/alive")
        link = walnut_dir / "escaping-bundle"
        link.symlink_to(outside)

        env = self._call("04_Ventures/alive")
        paths = {b["path"] for b in env["structuredContent"]["bundles"]}
        self.assertNotIn("escaping-bundle", paths)


# ---------------------------------------------------------------------------
# get_bundle tests.
# ---------------------------------------------------------------------------


class GetBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(self.world, "04_Ventures/alive")
        self.world.write_bundle(
            "04_Ventures/alive",
            "alive-mcp-research",
            MANIFEST_FULL,
            tasks=[
                {"title": "Scope the work", "status": "active"},
                {"title": "Urgent fix", "priority": "urgent", "status": "todo"},
                {"title": "Next task", "status": "todo"},
                {"title": "Blocked", "status": "blocked"},
                {"title": "Done", "status": "done"},
            ],
            raw_files=["source1.md", "source2.pdf"],
        )

    def _call(self, walnut: str, bundle: str) -> dict[str, Any]:
        ctx = _fake_ctx(str(self.world.root))
        return _run(bundle_tools.get_bundle(ctx, walnut, bundle))

    def test_returns_nine_key_manifest(self) -> None:
        env = self._call("04_Ventures/alive", "alive-mcp-research")
        self.assertFalse(env["isError"], msg=env)
        manifest = env["structuredContent"]["manifest"]
        self.assertEqual(
            set(manifest.keys()),
            {"name", "goal", "outcome", "status", "phase", "updated",
             "due", "context", "active_sessions"},
        )

    def test_manifest_values_populated(self) -> None:
        env = self._call("04_Ventures/alive", "alive-mcp-research")
        manifest = env["structuredContent"]["manifest"]
        self.assertEqual(manifest["name"], "Shielding Review")
        self.assertEqual(manifest["status"], "draft")
        self.assertEqual(manifest["phase"], "research")
        self.assertEqual(manifest["updated"], "2026-04-15")
        self.assertEqual(manifest["due"], "2026-04-30")
        self.assertIn("Vendor shortlist", manifest["context"])
        self.assertEqual(
            manifest["active_sessions"], ["a8c95e9", "bc96e49c"]
        )

    def test_derived_task_counts(self) -> None:
        env = self._call("04_Ventures/alive", "alive-mcp-research")
        counts = env["structuredContent"]["derived"]["task_counts"]
        self.assertEqual(counts["urgent"], 1)
        self.assertEqual(counts["active"], 1)
        self.assertEqual(counts["todo"], 2)  # Urgent-priority task + "Next task"
        self.assertEqual(counts["blocked"], 1)
        self.assertEqual(counts["done"], 1)

    def test_derived_raw_file_count(self) -> None:
        env = self._call("04_Ventures/alive", "alive-mcp-research")
        self.assertEqual(
            env["structuredContent"]["derived"]["raw_file_count"], 2
        )

    def test_derived_last_updated_uses_manifest(self) -> None:
        env = self._call("04_Ventures/alive", "alive-mcp-research")
        self.assertEqual(
            env["structuredContent"]["derived"]["last_updated"],
            "2026-04-15",
        )

    def test_derived_last_updated_falls_back_to_mtime(self) -> None:
        # Bundle with no `updated:` in manifest -> falls back to mtime.
        self.world.write_bundle(
            "04_Ventures/alive",
            "no-date",
            MANIFEST_MINIMAL,
        )
        env = self._call("04_Ventures/alive", "no-date")
        self.assertFalse(env["isError"], msg=env)
        last = env["structuredContent"]["derived"]["last_updated"]
        # Regex-looking ISO date string (YYYY-MM-DD) -- exact value
        # depends on test-run time, but it must not be the sentinel.
        self.assertRegex(last, r"^\d{4}-\d{2}-\d{2}$")
        self.assertNotEqual(last, "1970-01-01")

    def test_empty_bundle_has_zero_counts(self) -> None:
        self.world.write_bundle(
            "04_Ventures/alive",
            "empty",
            MANIFEST_MINIMAL,
        )
        env = self._call("04_Ventures/alive", "empty")
        counts = env["structuredContent"]["derived"]["task_counts"]
        self.assertEqual(counts,
                         {"urgent": 0, "active": 0, "todo": 0,
                          "blocked": 0, "done": 0})
        self.assertEqual(
            env["structuredContent"]["derived"]["raw_file_count"], 0
        )

    def test_unknown_bundle_returns_err_bundle_not_found(self) -> None:
        env = self._call("04_Ventures/alive", "nope")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "BUNDLE_NOT_FOUND"
        )

    def test_unknown_bundle_includes_fuzzy_suggestions(self) -> None:
        # Typo on the tail of an existing bundle.
        env = self._call("04_Ventures/alive", "alive-mcp-reserch")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "BUNDLE_NOT_FOUND"
        )
        suggestions = env["structuredContent"]["suggestions"]
        self.assertGreaterEqual(len(suggestions), 1)
        self.assertIn("Did you mean", suggestions[0])
        self.assertIn("alive-mcp-research", suggestions[0])

    def test_unknown_walnut_returns_walnut_not_found(self) -> None:
        env = self._call("04_Ventures/nope", "anything")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "WALNUT_NOT_FOUND"
        )

    def test_no_world(self) -> None:
        ctx = _fake_ctx(None)
        env = _run(bundle_tools.get_bundle(ctx, "04_Ventures/alive", "x"))
        self.assertTrue(env["isError"])
        self.assertEqual(env["structuredContent"]["error"], "NO_WORLD")

    def test_bundle_path_escape(self) -> None:
        env = self._call("04_Ventures/alive", "../../escape")
        self.assertTrue(env["isError"])
        # The path-escape happens at walnut resolution because
        # normalized strip trims leading .. -- but the resolver also
        # catches it via safe_join. Either PATH_ESCAPE or
        # BUNDLE_NOT_FOUND is acceptable; the important thing is no
        # crash and isError=True.
        self.assertIn(
            env["structuredContent"]["error"],
            {"PATH_ESCAPE", "BUNDLE_NOT_FOUND"},
        )

    def test_bare_name_matches_nested_bundle(self) -> None:
        """Callers can pass a bare bundle name when unambiguous."""
        self.world.write_bundle(
            "04_Ventures/alive",
            "bundles/my-bundle",
            MANIFEST_MINIMAL,
        )
        env = self._call("04_Ventures/alive", "my-bundle")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(
            env["structuredContent"]["manifest"]["goal"],
            "One-off prototype",
        )

    def test_ambiguous_bare_name_is_not_found(self) -> None:
        """When two bundles share a tail, bare-name lookup fails loudly.

        Uses two v2 nested sub-directories that share a tail name --
        the vendored scanner surfaces both since nothing in its skip
        set prevents nested v2 discovery. An ambiguous bare-name
        lookup must refuse to guess: callers get BUNDLE_NOT_FOUND so
        they re-query with the full relpath.
        """
        self.world.write_bundle(
            "04_Ventures/alive",
            "bundles/group-a/shared",
            MANIFEST_MINIMAL,
        )
        self.world.write_bundle(
            "04_Ventures/alive",
            "bundles/group-b/shared",
            MANIFEST_MINIMAL,
        )
        env = self._call("04_Ventures/alive", "shared")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "BUNDLE_NOT_FOUND"
        )


# ---------------------------------------------------------------------------
# read_bundle_manifest tests.
# ---------------------------------------------------------------------------


class ReadBundleManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(self.world, "04_Ventures/alive")
        self.world.write_bundle(
            "04_Ventures/alive",
            "full",
            MANIFEST_FULL,
        )
        self.world.write_bundle(
            "04_Ventures/alive",
            "minimal",
            MANIFEST_MINIMAL,
        )
        self.world.write_bundle(
            "04_Ventures/alive",
            "blank",
            MANIFEST_EMPTY,
        )

    def _call(self, walnut: str, bundle: str) -> dict[str, Any]:
        ctx = _fake_ctx(str(self.world.root))
        return _run(bundle_tools.read_bundle_manifest(ctx, walnut, bundle))

    def test_full_manifest_empty_warnings(self) -> None:
        env = self._call("04_Ventures/alive", "full")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(env["structuredContent"]["warnings"], [])

    def test_returns_nine_key_manifest(self) -> None:
        env = self._call("04_Ventures/alive", "full")
        manifest = env["structuredContent"]["manifest"]
        self.assertEqual(
            set(manifest.keys()),
            {"name", "goal", "outcome", "status", "phase", "updated",
             "due", "context", "active_sessions"},
        )

    def test_no_derived_field(self) -> None:
        """read_bundle_manifest is the narrow shape -- no derived counts."""
        env = self._call("04_Ventures/alive", "full")
        self.assertNotIn("derived", env["structuredContent"])
        self.assertNotIn("task_counts", env["structuredContent"])

    def test_minimal_manifest_warns_missing_keys(self) -> None:
        env = self._call("04_Ventures/alive", "minimal")
        warnings = env["structuredContent"]["warnings"]
        # Minimal has goal + status; other 7 (including active_sessions
        # WHICH the parser emits as [] when `squirrels:` is absent,
        # so it's not flagged) minus the ones the parser emits.
        # goal and status are present -> no warning.
        self.assertNotIn("could not parse 'goal'", warnings)
        self.assertNotIn("could not parse 'status'", warnings)
        # name, outcome, phase, updated, due, context are all missing.
        for key in ("name", "outcome", "phase", "updated", "due", "context"):
            self.assertIn(
                "could not parse '{}'".format(key),
                warnings,
                msg="expected warning for missing key {!r}".format(key),
            )

    def test_blank_manifest_gives_full_warnings_minus_parser_emissions(self) -> None:
        env = self._call("04_Ventures/alive", "blank")
        warnings = env["structuredContent"]["warnings"]
        # Parser emits active_sessions=[] always; it's present but
        # empty so _collect_warnings does NOT flag it.
        # The other 8 keys are missing.
        self.assertEqual(len(warnings), 8)
        for key in bundle_tools._MANIFEST_KEYS:
            if key == "active_sessions":
                self.assertNotIn(
                    "could not parse 'active_sessions'", warnings
                )
            else:
                self.assertIn(
                    "could not parse '{}'".format(key), warnings
                )

    def test_bundle_not_found(self) -> None:
        env = self._call("04_Ventures/alive", "nope")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "BUNDLE_NOT_FOUND"
        )

    def test_walnut_not_found(self) -> None:
        env = self._call("04_Ventures/nope", "full")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "WALNUT_NOT_FOUND"
        )

    def test_no_world(self) -> None:
        ctx = _fake_ctx(None)
        env = _run(
            bundle_tools.read_bundle_manifest(ctx, "04_Ventures/alive", "full")
        )
        self.assertTrue(env["isError"])
        self.assertEqual(env["structuredContent"]["error"], "NO_WORLD")


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


class BundleToolRegistrationTests(unittest.TestCase):
    """``build_server`` wires the three bundle tools with readOnlyHint=True."""

    def test_bundle_tools_registered_with_readonly_hint(self) -> None:
        from mcp.server.fastmcp import FastMCP

        server = FastMCP(name="test-bundle-registration")
        bundle_tools.register(server)

        registry = server._tool_manager._tools  # type: ignore[attr-defined]
        expected = {"list_bundles", "get_bundle", "read_bundle_manifest"}
        self.assertLessEqual(expected, set(registry.keys()))

        for name in expected:
            with self.subTest(tool=name):
                tool = registry[name]
                ann = getattr(tool, "annotations", None)
                self.assertIsNotNone(
                    ann, msg="tool {} has no annotations".format(name)
                )
                assert ann is not None
                self.assertTrue(ann.readOnlyHint)
                self.assertFalse(ann.destructiveHint)
                self.assertFalse(ann.openWorldHint)

    def test_full_build_server_registers_bundle_tools(self) -> None:
        """Real build_server() wires T7's tools onto the served instance."""
        from alive_mcp.server import build_server

        server = build_server()
        registry = server._tool_manager._tools  # type: ignore[attr-defined]
        expected = {"list_bundles", "get_bundle", "read_bundle_manifest"}
        self.assertLessEqual(expected, set(registry.keys()))


# ---------------------------------------------------------------------------
# No-YAML-dep invariant test -- catches regression if someone adds
# ``import yaml`` during v0.1.
# ---------------------------------------------------------------------------


class NoYamlDepInvariant(unittest.TestCase):
    """alive-mcp v0.1 must not import PyYAML anywhere.

    The task spec explicitly forbids adding a YAML dep; the regex-
    minimal parser in walnut_paths is the entire bundle-parsing
    surface. This test scans the ``alive_mcp`` package for any
    ``import yaml`` / ``from yaml`` usage so a regression surfaces
    at test time.
    """

    def test_no_yaml_imports_in_src(self) -> None:
        import alive_mcp
        src_root = pathlib.Path(alive_mcp.__file__).parent
        offenders: list[str] = []
        for path in src_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if (
                    stripped.startswith("import yaml")
                    or stripped.startswith("from yaml ")
                    or stripped.startswith("from yaml.")
                ):
                    offenders.append("{}:{}: {}".format(path, lineno, stripped))
        self.assertEqual(
            offenders,
            [],
            msg=(
                "alive-mcp v0.1 must not import PyYAML. Offenders:\n"
                + "\n".join(offenders)
            ),
        )


if __name__ == "__main__":
    unittest.main()
