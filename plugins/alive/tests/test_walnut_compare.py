#!/usr/bin/env python3
"""Tests for the LD13 walnut comparator (fn-7-7cw.11).

Verifies the canonical comparator's default ignore rules, log entry filtering,
manifest timestamp tolerance, and CRLF normalisation.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_walnut_compare -v
"""

import os
import shutil
import sys
import tempfile
import unittest


_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from walnut_builder import build_walnut  # noqa: E402
from walnut_compare import (  # noqa: E402
    walnut_equal,
    assert_walnut_equal,
)


def _write(path, content):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class WalnutEqualBasicTests(unittest.TestCase):

    def test_equal_identical_walnuts(self):
        with tempfile.TemporaryDirectory() as td:
            a = build_walnut(td, name="a", layout="v3",
                             bundles=[{"name": "b1", "goal": "g"}])
            b_dir = os.path.join(td, "b")
            shutil.copytree(a, b_dir)
            match, diffs = walnut_equal(a, b_dir)
            self.assertTrue(match, diffs)
            self.assertEqual(diffs, [])

    def test_equal_ignores_now_json(self):
        with tempfile.TemporaryDirectory() as td:
            a = build_walnut(td, name="a", layout="v3")
            b_dir = os.path.join(td, "b")
            shutil.copytree(a, b_dir)
            # Add a now.json + _generated/ + imports.json to b. These should
            # all be ignored by default.
            _write(os.path.join(b_dir, "_kernel", "now.json"),
                   '{"phase": "active"}\n')
            _write(os.path.join(b_dir, "_kernel", "_generated", "x.json"),
                   '{"x": 1}\n')
            _write(os.path.join(b_dir, "_kernel", "imports.json"),
                   '{"imports": []}\n')
            match, diffs = walnut_equal(a, b_dir)
            self.assertTrue(match, diffs)

    def test_equal_ignores_import_log_entries_with_param(self):
        with tempfile.TemporaryDirectory() as td:
            # ``a`` is the sender-side walnut with one baseline entry.
            # ``b`` is the receiver-side walnut: same baseline entry plus
            # one new import entry on top. ``ignore_log_entries=1`` drops
            # the top entry from b only and the rest matches.
            a = build_walnut(
                td, name="alpha", layout="v3",
                log_entries=[
                    {"timestamp": "2026-01-01T00:00:00Z",
                     "session_id": "baseline", "body": "Baseline entry."},
                ],
            )
            b_dir = os.path.join(td, "alpha-receiver")
            shutil.copytree(a, b_dir)
            log_b = os.path.join(b_dir, "_kernel", "log.md")
            with open(log_b, "r", encoding="utf-8") as f:
                content = f.read()
            # Insert an extra entry just after the frontmatter (logs are
            # prepend-only).
            split_at = content.index("\n---\n") + len("\n---\n")
            new_entry = (
                "\n## 2026-04-07T10:00:00Z - squirrel:receiver\n\n"
                "Receiver import entry.\n\nsigned: squirrel:receiver\n\n"
            )
            mutated = content[:split_at] + new_entry + content[split_at:]
            with open(log_b, "w", encoding="utf-8") as f:
                f.write(mutated)
            # Without ignore_log_entries, the trees differ.
            match, _ = walnut_equal(a, b_dir)
            self.assertFalse(match)
            # With ignore_log_entries=1, the trees match.
            match, diffs = walnut_equal(a, b_dir, ignore_log_entries=1)
            self.assertTrue(match, diffs)


class WalnutUnequalTests(unittest.TestCase):

    def test_unequal_on_different_bundle_content(self):
        with tempfile.TemporaryDirectory() as td:
            a = build_walnut(
                td, name="a", layout="v3",
                bundles=[{"name": "b1", "goal": "g",
                          "files": {"draft.md": "# draft v1"}}],
            )
            b_dir = os.path.join(td, "b")
            shutil.copytree(a, b_dir)
            # Mutate b's draft.md
            _write(os.path.join(b_dir, "b1", "draft.md"), "# draft v2\n")
            match, diffs = walnut_equal(a, b_dir)
            self.assertFalse(match)
            self.assertTrue(any("b1/draft.md" in d for d in diffs), diffs)


class WalnutNormalisationTests(unittest.TestCase):

    def test_normalisation_strips_crlf(self):
        with tempfile.TemporaryDirectory() as td:
            a = build_walnut(td, name="a", layout="v3",
                             bundles=[{"name": "b1", "goal": "g",
                                       "files": {"draft.md": "line1\nline2\n"}}])
            b_dir = os.path.join(td, "b")
            shutil.copytree(a, b_dir)
            # Convert b's draft.md to CRLF
            with open(os.path.join(b_dir, "b1", "draft.md"), "wb") as f:
                f.write(b"line1\r\nline2\r\n")
            match, diffs = walnut_equal(a, b_dir)
            self.assertTrue(match, diffs)


class AssertWalnutEqualHelperTests(unittest.TestCase):

    def test_assert_passes_on_match(self):
        with tempfile.TemporaryDirectory() as td:
            a = build_walnut(td, name="a", layout="v3")
            b = os.path.join(td, "b")
            shutil.copytree(a, b)
            assert_walnut_equal(self, a, b)  # should not fail

    def test_assert_fails_with_pretty_diff(self):
        with tempfile.TemporaryDirectory() as td:
            a = build_walnut(td, name="a", layout="v3",
                             bundles=[{"name": "b1", "goal": "g"}])
            b = os.path.join(td, "b")
            shutil.copytree(a, b)
            # Drop a file from b
            os.unlink(os.path.join(b, "b1", "context.manifest.yaml"))
            with self.assertRaises(self.failureException) as ctx:
                assert_walnut_equal(self, a, b)
            msg = str(ctx.exception)
            self.assertIn("walnut trees differ", msg)
            self.assertIn("b1/context.manifest.yaml", msg)


if __name__ == "__main__":
    unittest.main()
