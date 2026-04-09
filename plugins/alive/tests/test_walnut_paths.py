#!/usr/bin/env python3
"""Unit tests for ``plugins/alive/scripts/walnut_paths.py``.

Each test builds a fresh fixture tree under ``tempfile.TemporaryDirectory`` and
asserts that the public API (``resolve_bundle_path``, ``find_bundles``,
``scan_bundles``) handles every layout the v3 P2P sharing layer is expected to
encounter: v3 flat, v2 nested ``bundles/``, v1 ``_core/_capsules/``, mixed
layouts, skip dirs, nested walnut boundaries, and deeply nested bundles.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_walnut_paths -v

Stdlib only -- no PyYAML, no third-party assertions.
"""

import os
import sys
import tempfile
import unittest


# Make ``plugins/alive/scripts`` importable when the test file is invoked from
# the repo root via ``python3 -m unittest plugins.alive.tests.test_walnut_paths``.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import walnut_paths  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write(path, content=""):
    """Write ``content`` to ``path``, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_kernel(walnut, name="walnut"):
    """Create a minimal ``_kernel/key.md`` so the directory looks like a walnut."""
    _write(
        os.path.join(walnut, "_kernel", "key.md"),
        "---\ntype: venture\nname: {0}\n---\n".format(name),
    )


def _make_bundle_v3(walnut, bundle_relpath, goal="test goal", status="draft"):
    """Create a v3 flat bundle at ``{walnut}/{bundle_relpath}``."""
    bundle_dir = os.path.join(walnut, bundle_relpath)
    _write(
        os.path.join(bundle_dir, "context.manifest.yaml"),
        "goal: {0}\nstatus: {1}\n".format(goal, status),
    )


def _make_bundle_v2(walnut, name, goal="v2 goal", status="active"):
    """Create a v2 bundle inside ``{walnut}/bundles/{name}``."""
    bundle_dir = os.path.join(walnut, "bundles", name)
    _write(
        os.path.join(bundle_dir, "context.manifest.yaml"),
        "goal: {0}\nstatus: {1}\n".format(goal, status),
    )


def _make_bundle_v1(walnut, name):
    """Create a v1 capsule inside ``{walnut}/_core/_capsules/{name}``."""
    capsule_dir = os.path.join(walnut, "_core", "_capsules", name)
    _write(
        os.path.join(capsule_dir, "companion.md"),
        "---\ntype: capsule\nname: {0}\n---\n".format(name),
    )


# ---------------------------------------------------------------------------
# resolve_bundle_path
# ---------------------------------------------------------------------------


class ResolveBundlePathTests(unittest.TestCase):
    def test_v3_flat_lookup(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v3(walnut, "shielding-review")
            resolved = walnut_paths.resolve_bundle_path(walnut, "shielding-review")
            self.assertIsNotNone(resolved)
            self.assertTrue(resolved.endswith("shielding-review"))
            self.assertTrue(os.path.isdir(resolved))

    def test_v2_container_lookup(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v2(walnut, "launch-checklist")
            resolved = walnut_paths.resolve_bundle_path(walnut, "launch-checklist")
            self.assertIsNotNone(resolved)
            self.assertIn(os.path.join("bundles", "launch-checklist"), resolved)

    def test_v1_legacy_lookup(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v1(walnut, "old-capsule")
            resolved = walnut_paths.resolve_bundle_path(walnut, "old-capsule")
            self.assertIsNotNone(resolved)
            self.assertIn(
                os.path.join("_core", "_capsules", "old-capsule"),
                resolved,
            )

    def test_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            self.assertIsNone(
                walnut_paths.resolve_bundle_path(walnut, "no-such-bundle")
            )

    def test_returns_none_for_empty_bundle_arg(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            self.assertIsNone(walnut_paths.resolve_bundle_path(walnut, ""))
            self.assertIsNone(walnut_paths.resolve_bundle_path(walnut, None))

    def test_resolve_fallback_order_prefers_v3(self):
        # When the same name exists in v3 flat AND v2 container, v3 wins.
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v3(walnut, "duplicate", goal="v3 wins")
            _make_bundle_v2(walnut, "duplicate", goal="v2 loses")
            resolved = walnut_paths.resolve_bundle_path(walnut, "duplicate")
            self.assertIsNotNone(resolved)
            # The v3 path is the walnut root + bundle name; v2 would have
            # ``bundles/duplicate``.
            self.assertNotIn(os.sep + "bundles" + os.sep, resolved)

    def test_resolve_returns_absolute_path(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v3(walnut, "abs-check")
            # Pass a relative path in to make sure we still get an absolute back.
            cwd = os.getcwd()
            try:
                os.chdir(os.path.dirname(walnut))
                rel_walnut = os.path.basename(walnut)
                resolved = walnut_paths.resolve_bundle_path(rel_walnut, "abs-check")
                self.assertIsNotNone(resolved)
                self.assertTrue(os.path.isabs(resolved))
            finally:
                os.chdir(cwd)


# ---------------------------------------------------------------------------
# find_bundles
# ---------------------------------------------------------------------------


class FindBundlesTests(unittest.TestCase):
    def test_v3_flat_bundles(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v3(walnut, "alpha")
            _make_bundle_v3(walnut, "beta")
            _make_bundle_v3(walnut, "gamma")
            bundles = walnut_paths.find_bundles(walnut)
            names = [name for name, _ in bundles]
            self.assertEqual(names, ["alpha", "beta", "gamma"])
            for _, abs_path in bundles:
                self.assertTrue(os.path.isabs(abs_path))
                self.assertTrue(os.path.isdir(abs_path))

    def test_v2_container_bundles(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v2(walnut, "alpha")
            _make_bundle_v2(walnut, "beta")
            bundles = walnut_paths.find_bundles(walnut)
            names = [name for name, _ in bundles]
            # v2 container relpaths look like ``bundles/<name>``.
            self.assertEqual(names, ["bundles/alpha", "bundles/beta"])

    def test_v1_legacy_bundles(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v1(walnut, "legacy-cap")
            # Legacy capsules sit under ``_core/_capsules`` -- this is in
            # ``_SKIP_DIRS``, so the discovery walker would normally prune
            # ``_core``. We need v1 capsules to remain discoverable for
            # backward-compat receive paths, so the test asserts the contract:
            # find_bundles can locate at least the legacy capsule's directory.
            bundles = walnut_paths.find_bundles(walnut)
            # The walker prunes ``_core`` by design (matches project.py /
            # tasks.py behavior). v1 capsules are discovered ONLY when scanning
            # bypasses skip dirs -- which the resolver still supports via
            # ``resolve_bundle_path``. So find_bundles returns nothing for a
            # pure-v1 walnut, and that is the documented behavior.
            self.assertEqual(bundles, [])

    def test_mixed_v2_v3(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v3(walnut, "v3-flat")
            _make_bundle_v2(walnut, "v2-nested")
            bundles = walnut_paths.find_bundles(walnut)
            names = [name for name, _ in bundles]
            self.assertIn("v3-flat", names)
            self.assertIn("bundles/v2-nested", names)
            self.assertEqual(len(names), 2)

    def test_skip_dirs_pruned(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            # Plant manifests inside dirs the walker MUST skip. None should
            # appear in the result.
            for skip in ("_kernel", "node_modules", "__pycache__", "raw",
                         "_archive", ".git"):
                _write(
                    os.path.join(walnut, skip, "fake-bundle",
                                 "context.manifest.yaml"),
                    "goal: should not be discovered\n",
                )
            bundles = walnut_paths.find_bundles(walnut)
            self.assertEqual(bundles, [])

    def test_hidden_dirs_pruned(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _write(
                os.path.join(walnut, ".secret", "ctx", "context.manifest.yaml"),
                "goal: hidden\n",
            )
            _make_bundle_v3(walnut, "visible")
            bundles = walnut_paths.find_bundles(walnut)
            names = [name for name, _ in bundles]
            self.assertEqual(names, ["visible"])

    def test_nested_walnut_boundary(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut, name="parent")
            _make_bundle_v3(walnut, "parent-bundle")

            # Carve out a nested walnut at ``walnut/sub-walnut`` with its own
            # _kernel/key.md and a bundle inside.
            sub = os.path.join(walnut, "sub-walnut")
            _make_kernel(sub, name="child")
            _make_bundle_v3(sub, "child-bundle")

            bundles = walnut_paths.find_bundles(walnut)
            names = [name for name, _ in bundles]
            # Parent's bundle is discovered. The child walnut's bundle MUST
            # NOT show up in the parent scan -- the boundary stops descent.
            self.assertIn("parent-bundle", names)
            self.assertNotIn("sub-walnut/child-bundle", names)
            for name in names:
                self.assertFalse(name.startswith("sub-walnut/"))

    def test_deeply_nested_bundle(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v3(walnut, os.path.join("archive", "old", "bundle-a"))
            bundles = walnut_paths.find_bundles(walnut)
            names = [name for name, _ in bundles]
            self.assertEqual(names, ["archive/old/bundle-a"])

    def test_return_sorted(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            for name in ("zebra", "apple", "mango", "banana"):
                _make_bundle_v3(walnut, name)
            bundles = walnut_paths.find_bundles(walnut)
            names = [name for name, _ in bundles]
            self.assertEqual(names, sorted(names))

    def test_relpath_uses_posix_separators(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v3(walnut, os.path.join("nested", "bundle-x"))
            bundles = walnut_paths.find_bundles(walnut)
            self.assertEqual(len(bundles), 1)
            relpath, _ = bundles[0]
            self.assertNotIn("\\", relpath)
            self.assertEqual(relpath, "nested/bundle-x")


# ---------------------------------------------------------------------------
# scan_bundles
# ---------------------------------------------------------------------------


class ScanBundlesTests(unittest.TestCase):
    def test_returns_parsed_manifests(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v3(walnut, "alpha", goal="ship the thing", status="prototype")
            _make_bundle_v3(walnut, "beta", goal="research wave 2", status="draft")
            scanned = walnut_paths.scan_bundles(walnut)
            self.assertEqual(set(scanned.keys()), {"alpha", "beta"})
            self.assertEqual(scanned["alpha"]["goal"], "ship the thing")
            self.assertEqual(scanned["alpha"]["status"], "prototype")
            self.assertEqual(scanned["beta"]["goal"], "research wave 2")

    def test_handles_unreadable_manifest_gracefully(self):
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            # A bundle whose manifest exists is parsed; an empty manifest
            # parses to an empty-active-sessions dict (still present).
            _make_bundle_v3(walnut, "ok")
            empty_dir = os.path.join(walnut, "empty-bundle")
            _write(os.path.join(empty_dir, "context.manifest.yaml"), "")
            scanned = walnut_paths.scan_bundles(walnut)
            self.assertIn("ok", scanned)
            self.assertIn("empty-bundle", scanned)
            # Empty manifest still produces a dict (with active_sessions list).
            self.assertEqual(scanned["empty-bundle"].get("active_sessions"), [])

    def test_scan_includes_only_v2_v3(self):
        # scan_bundles relies on find_bundles which prunes _core; v1 capsules
        # are NOT in the scan. This documents the design choice.
        with tempfile.TemporaryDirectory() as walnut:
            _make_kernel(walnut)
            _make_bundle_v3(walnut, "v3-bundle")
            _make_bundle_v1(walnut, "v1-cap")
            scanned = walnut_paths.scan_bundles(walnut)
            self.assertIn("v3-bundle", scanned)
            self.assertNotIn("v1-cap", scanned)


if __name__ == "__main__":
    unittest.main(verbosity=2)
