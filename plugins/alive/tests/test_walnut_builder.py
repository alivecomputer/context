#!/usr/bin/env python3
"""Tests for the synthetic walnut builder fixture (fn-7-7cw.11).

Verifies that ``walnut_builder.build_walnut`` produces structurally valid
v2 and v3 walnut trees, with sub-walnut nesting that the scan logic in
``walnut_paths.find_bundles`` recognises as a boundary.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_walnut_builder -v
"""

import json
import os
import sys
import tempfile
import unittest


_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import walnut_paths  # noqa: E402
from walnut_builder import build_walnut  # noqa: E402


class BuildV3MinimalTests(unittest.TestCase):

    def test_build_v3_walnut_minimal(self):
        with tempfile.TemporaryDirectory() as td:
            walnut = build_walnut(td, name="alpha", layout="v3")
            self.assertTrue(os.path.isdir(walnut))
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "_kernel", "key.md")))
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "_kernel", "log.md")))
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "_kernel", "insights.md")))
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "_kernel", "tasks.json")))
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "_kernel", "completed.json")))
            # v3 walnuts never get a stored now.json
            self.assertFalse(os.path.exists(
                os.path.join(walnut, "_kernel", "now.json")))
            with open(os.path.join(walnut, "_kernel", "tasks.json")) as f:
                tasks = json.load(f)
            self.assertEqual(tasks, {"tasks": []})


class BuildV3WithBundlesTests(unittest.TestCase):

    def test_build_v3_walnut_with_bundles_and_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            walnut = build_walnut(
                td,
                name="beta",
                layout="v3",
                bundles=[
                    {
                        "name": "shielding-review",
                        "goal": "Review shielding",
                        "files": {"draft-01.md": "# draft"},
                    },
                    {
                        "name": "launch-checklist",
                        "goal": "Launch",
                        "files": {"items.md": "- [ ] do thing"},
                        "raw_files": {"raw-1.txt": "raw content"},
                    },
                ],
                tasks={
                    "unscoped": [
                        {"id": "t1", "title": "task one", "status": "open"},
                    ],
                    "shielding-review": [
                        {"id": "t2", "title": "review draft", "status": "open"},
                    ],
                },
                live_files=[{"path": "engineering/spec.md", "content": "# spec"}],
            )
            # Bundles should be flat under the walnut root.
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "shielding-review", "context.manifest.yaml")))
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "shielding-review", "draft-01.md")))
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "launch-checklist", "raw", "raw-1.txt")))
            # Live context lives at walnut root.
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "engineering", "spec.md")))
            with open(os.path.join(walnut, "_kernel", "tasks.json")) as f:
                data = json.load(f)
            self.assertEqual(len(data["tasks"]), 2)
            # Bundle-scoped task carries its bundle field.
            scoped = [t for t in data["tasks"] if t.get("bundle")]
            self.assertEqual(len(scoped), 1)
            self.assertEqual(scoped[0]["bundle"], "shielding-review")
            # walnut_paths.find_bundles sees both bundles.
            bundles = walnut_paths.find_bundles(walnut)
            leaves = sorted(rel for rel, _ in bundles)
            self.assertEqual(leaves, ["launch-checklist", "shielding-review"])


class BuildV2LayoutTests(unittest.TestCase):

    def test_build_v2_walnut_layout(self):
        with tempfile.TemporaryDirectory() as td:
            walnut = build_walnut(
                td,
                name="gamma",
                layout="v2",
                bundles=[
                    {"name": "alpha", "goal": "alpha goal"},
                    {"name": "beta", "goal": "beta goal"},
                ],
                tasks={
                    "alpha": [
                        {"title": "task 1"},
                        {"title": "task 2", "status": "done"},
                    ],
                },
            )
            # v2 uses bundles/<name>/ container
            self.assertTrue(os.path.isdir(os.path.join(walnut, "bundles")))
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "bundles", "alpha", "context.manifest.yaml")))
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "bundles", "alpha", "tasks.md")))
            # _generated/now.json is the v2 projection
            self.assertTrue(os.path.isfile(
                os.path.join(walnut, "_kernel", "_generated", "now.json")))
            # v2 walnuts MUST NOT have tasks.json at the kernel root
            self.assertFalse(os.path.exists(
                os.path.join(walnut, "_kernel", "tasks.json")))
            # find_bundles sees v2 paths
            bundles = walnut_paths.find_bundles(walnut)
            relpaths = sorted(rel for rel, _ in bundles)
            self.assertEqual(relpaths, ["bundles/alpha", "bundles/beta"])


class BuildSubWalnutTests(unittest.TestCase):

    def test_build_walnut_with_nested_sub_walnut(self):
        with tempfile.TemporaryDirectory() as td:
            walnut = build_walnut(
                td,
                name="parent",
                layout="v3",
                bundles=[{"name": "outer-bundle", "goal": "outer"}],
                sub_walnuts=[
                    {
                        "name": "child",
                        "layout": "v3",
                        "bundles": [{"name": "inner-bundle", "goal": "inner"}],
                    },
                ],
            )
            child = os.path.join(walnut, "child")
            self.assertTrue(os.path.isfile(
                os.path.join(child, "_kernel", "key.md")))
            self.assertTrue(os.path.isfile(
                os.path.join(child, "inner-bundle", "context.manifest.yaml")))
            # Parent's find_bundles MUST stop at the nested walnut boundary
            # so inner-bundle is NOT in the parent's bundle list.
            parent_bundles = sorted(
                rel for rel, _ in walnut_paths.find_bundles(walnut)
            )
            self.assertIn("outer-bundle", parent_bundles)
            self.assertNotIn("inner-bundle", parent_bundles)
            self.assertNotIn("child/inner-bundle", parent_bundles)
            # The child walnut's own scan finds inner-bundle.
            child_bundles = sorted(
                rel for rel, _ in walnut_paths.find_bundles(child)
            )
            self.assertEqual(child_bundles, ["inner-bundle"])


if __name__ == "__main__":
    unittest.main()
