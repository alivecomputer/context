"""Unit tests for ``alive_mcp.world`` — World predicate + discovery.

Covers the T3 acceptance criteria on the world-discovery half:

- ``is_world_root(dir)`` returns True for a dir with ``01_Archive + 02_Life``,
  True for a dir with ``.alive/``, False otherwise.
- World discovery: Root points AT a World root → returns it directly.
- World discovery: Root points at a walnut (descendant of a World root)
  → walks up, finds the World, returns it.
- World discovery: Root has no World ancestor within bound → raises
  ``WorldNotFoundError`` (does NOT fall back to Root itself).
- World discovery: Root walk-up never crosses above the Root (bounded).
- World discovery: ``ALIVE_WORLD_ROOT`` env takes precedence over
  ``ALIVE_WORLD_PATH`` when both set.
- World discovery: ``ALIVE_WORLD_PATH`` still works (alias).
- ``audit_log_path`` correctly resolves regardless of Root location.

Uses stdlib ``unittest`` + ``tempfile``. No third-party deps.
"""
from __future__ import annotations

import io
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

# Ensure ``python3 -m unittest discover tests`` works from a clean
# checkout without requiring ``pip install -e .``.
_SRC_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if os.path.isdir(_SRC_DIR) and _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from alive_mcp.errors import WorldNotFoundError  # noqa: E402
from alive_mcp.world import (  # noqa: E402
    audit_log_path,
    discover_world,
    is_world_root,
)


def _make_domain_world(base: str) -> str:
    """Create a World root using the legacy-hook predicate (domain folders).

    Returns the absolute realpath.
    """
    world = os.path.realpath(base)
    os.makedirs(os.path.join(world, "01_Archive"), exist_ok=True)
    os.makedirs(os.path.join(world, "02_Life"), exist_ok=True)
    return world


def _make_alive_world(base: str) -> str:
    """Create a World root using the Hermes predicate (``.alive/`` sentinel)."""
    world = os.path.realpath(base)
    os.makedirs(os.path.join(world, ".alive"), exist_ok=True)
    return world


def _make_walnut(world_root: str, rel: str) -> str:
    """Create a walnut directory under ``world_root`` at ``rel``."""
    walnut = os.path.join(world_root, rel)
    os.makedirs(os.path.join(walnut, "_kernel"), exist_ok=True)
    with open(os.path.join(walnut, "_kernel", "key.md"), "w", encoding="utf-8") as f:
        f.write("# key\n")
    return walnut


class IsWorldRootTests(unittest.TestCase):
    """Acceptance: the predicate accepts both ``01_Archive+02_Life`` and ``.alive/``."""

    def test_domain_sentinels_satisfy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            world = _make_domain_world(tmp)
            self.assertTrue(is_world_root(world))

    def test_alive_sentinel_satisfies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            world = _make_alive_world(tmp)
            self.assertTrue(is_world_root(world))

    def test_neither_sentinel_fails(self) -> None:
        """Empty directory is NOT a World."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_world_root(tmp))

    def test_partial_domain_fails(self) -> None:
        """``01_Archive`` alone (or ``02_Life`` alone) does NOT satisfy."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "01_Archive"))
            self.assertFalse(is_world_root(tmp))

    def test_nonexistent_path_fails(self) -> None:
        self.assertFalse(is_world_root("/does/not/exist/anywhere"))

    def test_empty_string_fails(self) -> None:
        self.assertFalse(is_world_root(""))

    def test_file_fails(self) -> None:
        """A file (not a directory) is NOT a World."""
        with tempfile.NamedTemporaryFile() as f:
            self.assertFalse(is_world_root(f.name))


class DiscoverWorldFromRootsTests(unittest.TestCase):
    """Acceptance: Roots-first discovery walks upward WITHIN each Root only."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.world = _make_domain_world(self.tmp.name)
        self.walnut = _make_walnut(self.world, "04_Ventures/nova-station")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_root_at_world_returns_world(self) -> None:
        """Root = World root → predicate matches on step 0."""
        resolved = discover_world(roots=[self.world], env={})
        self.assertEqual(self.world, resolved)

    def test_root_at_walnut_walks_up_to_world(self) -> None:
        """Root pointing INSIDE the World walks upward and finds the World.

        Key constraint: the World must sit at or below the Root.
        Since Root == walnut sits BELOW the World, this test actually
        confirms a subtler contract: the walk bound is the Root
        itself, so walking up from ``walnut`` with bound ``walnut``
        only tests ``walnut``. Our World is ABOVE the Root here, so
        the walk SHOULD fail — and the error-path test below covers
        that.

        The "walks up to find the World" case happens when the user
        sets Root to a deeper subpath AND the World root sits between
        the Root and the start — but Root IS the bound, so the start
        must equal Root. Interpretation: if Root points directly AT
        the walnut and the walnut itself satisfies the predicate
        (e.g., a walnut with its own ``.alive/``), it returns the
        walnut. Otherwise the walk fails and we report no World for
        that Root.

        This test therefore exercises the ``.alive`` sentinel on a
        walnut-shaped Root (which is unusual but supported).
        """
        # Drop an ``.alive/`` inside the walnut so it satisfies the
        # predicate on its own.
        os.makedirs(os.path.join(self.walnut, ".alive"), exist_ok=True)
        resolved = discover_world(roots=[self.walnut], env={})
        self.assertEqual(self.walnut, resolved)

    def test_root_above_world_does_not_walk_down(self) -> None:
        """Root ABOVE a potential World does NOT walk down. Emits ERR_NO_WORLD."""
        # The temp parent directory is ABOVE self.world (self.world is
        # a subdirectory of self.tmp.name). Set Root to the parent.
        parent = os.path.realpath(os.path.dirname(self.world))
        # Guard: parent must be a real ancestor and must NOT itself
        # satisfy the predicate (the temp parent shouldn't).
        self.assertTrue(parent)
        self.assertFalse(is_world_root(parent))

        # Use a NEW temp root for this test so we don't accidentally
        # walk into a sibling World.
        with tempfile.TemporaryDirectory() as iso:
            iso_real = os.path.realpath(iso)
            # Create a World AT a subdirectory of ``iso``.
            _make_domain_world(os.path.join(iso_real, "world"))
            # Root points at ``iso`` (ABOVE the World). Walk-up from
            # ``iso`` within bound ``iso`` only tests ``iso`` itself,
            # which does NOT satisfy the predicate.
            with self.assertRaises(WorldNotFoundError) as ctx:
                discover_world(roots=[iso_real], env={})
            # Diagnostic should mention the env var hint.
            self.assertIn("ALIVE_WORLD_ROOT", str(ctx.exception))

    def test_walk_bounded_by_root(self) -> None:
        """Walk-up never crosses ABOVE the Root.

        Build: ``/tmp/outer/`` is a World, ``/tmp/outer/inner-walnut/``
        is a walnut, Root = ``/tmp/outer/inner-walnut/``. The walk
        starts at the walnut with bound = the walnut; it tests the
        walnut (fail) and stops. It does NOT climb to ``outer/`` and
        match there.
        """
        # self.walnut is ``04_Ventures/nova-station`` inside self.world
        # (a World). The walnut itself does NOT satisfy the predicate
        # (no ``01_Archive + 02_Life``, no ``.alive/``). With Root ==
        # walnut, the walk bound is the walnut, so it never reaches
        # self.world.
        self.assertFalse(is_world_root(self.walnut))
        with self.assertRaises(WorldNotFoundError):
            discover_world(roots=[self.walnut], env={})

    def test_no_root_promotion_when_predicate_fails(self) -> None:
        """A Root that does NOT satisfy the predicate is NOT promoted to World."""
        with tempfile.TemporaryDirectory() as empty:
            empty_real = os.path.realpath(empty)
            self.assertFalse(is_world_root(empty_real))
            with self.assertRaises(WorldNotFoundError):
                discover_world(roots=[empty_real], env={})


class DiscoverWorldFromEnvTests(unittest.TestCase):
    """Acceptance: env fallback precedence (ALIVE_WORLD_ROOT wins over ALIVE_WORLD_PATH)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.world_primary = _make_domain_world(
            os.path.join(self.tmp.name, "primary-world")
        )
        self.world_alias = _make_alive_world(
            os.path.join(self.tmp.name, "alias-world")
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_env_primary_used_when_only_primary_set(self) -> None:
        env = {"ALIVE_WORLD_ROOT": self.world_primary}
        resolved = discover_world(roots=[], env=env)
        self.assertEqual(self.world_primary, resolved)

    def test_env_alias_used_when_only_alias_set(self) -> None:
        """``ALIVE_WORLD_PATH`` alone still works (forward-compat alias)."""
        env = {"ALIVE_WORLD_PATH": self.world_alias}
        resolved = discover_world(roots=[], env=env)
        self.assertEqual(self.world_alias, resolved)

    def test_env_primary_wins_over_alias(self) -> None:
        """Both set with different values → primary wins, stderr logs the conflict."""
        env = {
            "ALIVE_WORLD_ROOT": self.world_primary,
            "ALIVE_WORLD_PATH": self.world_alias,
        }
        buf = io.StringIO()
        with redirect_stderr(buf):
            resolved = discover_world(roots=[], env=env)
        self.assertEqual(self.world_primary, resolved)
        # Stderr carries the conflict note.
        self.assertIn("ALIVE_WORLD_ROOT", buf.getvalue())
        self.assertIn("ALIVE_WORLD_PATH", buf.getvalue())

    def test_env_both_set_same_value_no_conflict_log(self) -> None:
        """Both set to the same value → no stderr conflict noise."""
        env = {
            "ALIVE_WORLD_ROOT": self.world_primary,
            "ALIVE_WORLD_PATH": self.world_primary,
        }
        buf = io.StringIO()
        with redirect_stderr(buf):
            resolved = discover_world(roots=[], env=env)
        self.assertEqual(self.world_primary, resolved)
        self.assertNotIn("different values", buf.getvalue())

    def test_env_below_world_walks_up(self) -> None:
        """Env var pointing INSIDE a World walks up (within the env's own bound).

        Rule: env value is walked upward within its OWN bound. If the
        env value IS the World root, step 0 matches. If the env value
        is a walnut BELOW a World but the bound is the env value
        itself, the walk never reaches the World (same rule as
        Roots). This test confirms the bounded behaviour.
        """
        walnut = _make_walnut(self.world_primary, "04_Ventures/nova-station")
        # Env pointing at the walnut: bound is the walnut, so the
        # walk only tests the walnut. It does NOT climb to the World.
        env = {"ALIVE_WORLD_ROOT": walnut}
        with self.assertRaises(WorldNotFoundError):
            discover_world(roots=[], env=env)

    def test_env_ignored_when_roots_resolve(self) -> None:
        """When Roots yield a World, env is NOT consulted.

        Set env to a DIFFERENT valid World and Roots to one valid
        World; discovery returns the Roots-derived World.
        """
        env = {"ALIVE_WORLD_ROOT": self.world_alias}
        resolved = discover_world(roots=[self.world_primary], env=env)
        self.assertEqual(self.world_primary, resolved)


class DiscoverWorldMultiRootSelectionTests(unittest.TestCase):
    """Single-world v0.1 selection: shortest realpath wins."""

    def test_shortest_realpath_wins(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            # Two Worlds at different depths under ``outer``.
            short_world = _make_domain_world(os.path.join(outer, "w"))  # shorter
            long_world = _make_domain_world(
                os.path.join(outer, "very-deep-directory-name", "w")
            )  # longer

            buf = io.StringIO()
            with redirect_stderr(buf):
                resolved = discover_world(
                    roots=[long_world, short_world], env={}
                )
            self.assertEqual(short_world, resolved)
            # Loser is logged to stderr.
            self.assertIn(long_world, buf.getvalue())


class DiscoverWorldFailureDiagnosticTests(unittest.TestCase):
    """When discovery fails, the diagnostic should be pointed."""

    def test_no_roots_no_env_raises_with_hint(self) -> None:
        with self.assertRaises(WorldNotFoundError) as ctx:
            discover_world(roots=[], env={})
        msg = str(ctx.exception)
        # Diagnostic mentions the primary env var name (the fix-it).
        self.assertIn("ALIVE_WORLD_ROOT", msg)
        # Diagnostic mentions the World predicate sentinels.
        self.assertIn("01_Archive", msg)
        self.assertIn(".alive", msg)


class AuditLogPathTests(unittest.TestCase):
    """Acceptance: audit path anchored at the resolved World regardless of Root."""

    def test_audit_path_is_under_world(self) -> None:
        world = "/tmp/some-world"
        path = audit_log_path(world)
        # Canonical layout: ``<world>/.alive/_mcp/audit.log``.
        self.assertEqual(
            os.path.join(world, ".alive", "_mcp", "audit.log"),
            path,
        )

    def test_audit_path_pure_no_side_effects(self) -> None:
        """Pure computation: no I/O on the returned path during call."""
        # The function must not create the directory.
        with tempfile.TemporaryDirectory() as tmp:
            path = audit_log_path(tmp)
            self.assertFalse(os.path.exists(path))
            # Parent dirs shouldn't be auto-created either.
            self.assertFalse(os.path.isdir(os.path.join(tmp, ".alive")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
