#!/usr/bin/env python3
"""Unit tests for the LD27 glob matcher in ``alive-p2p.py``.

Pins the exact semantics of ``_glob_to_regex`` and ``matches_exclusion`` so
the receive-side parity test (task .8) can rely on them. Run from
``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_glob_matcher -v

Stdlib only -- no PyYAML, no third-party assertions.
"""

import importlib.util
import os
import sys
import unittest


# ---------------------------------------------------------------------------
# Module loading: alive-p2p.py has a hyphen in the filename so a plain
# ``import alive_p2p`` does not work. Load it via importlib.util from the
# scripts directory.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import walnut_paths  # noqa: E402,F401  -- pre-cache so alive-p2p import works

_AP2P_PATH = os.path.join(_SCRIPTS, "alive-p2p.py")
_spec = importlib.util.spec_from_file_location("alive_p2p", _AP2P_PATH)
ap2p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap2p)  # type: ignore[union-attr]


class GlobToRegexTests(unittest.TestCase):
    """LD27 anchored regex translation."""

    def setUp(self):
        # Drop the module-level cache so each test sees a clean compile path.
        ap2p._GLOB_REGEX_CACHE.clear()

    def test_basename_pattern_matches_at_any_depth(self):
        # ``*.tmp`` is a basename pattern (no slashes).
        rx = ap2p._glob_to_regex("*.tmp")
        self.assertTrue(rx.match("a.tmp"))
        self.assertTrue(rx.match("foo/a.tmp"))
        self.assertTrue(rx.match("a/b/c.tmp"))
        self.assertFalse(rx.match("a.txt"))
        self.assertFalse(rx.match("foo.tmp.bak"))

    def test_basename_observations_md(self):
        # ``**/observations.md`` is a slash pattern that should match at any
        # depth via the ``**`` collapse rule.
        rx = ap2p._glob_to_regex("**/observations.md")
        self.assertTrue(rx.match("observations.md"))
        self.assertTrue(rx.match("a/observations.md"))
        self.assertTrue(rx.match("foo/bar/observations.md"))
        self.assertFalse(rx.match("observations.markdown"))

    def test_anchored_full_path(self):
        # ``_kernel/log.md`` must NOT match ``foo/_kernel/log.md``.
        rx = ap2p._glob_to_regex("_kernel/log.md")
        self.assertTrue(rx.match("_kernel/log.md"))
        self.assertFalse(rx.match("foo/_kernel/log.md"))
        self.assertFalse(rx.match("_kernel/log.md.bak"))
        self.assertFalse(rx.match("_kernel/logXmd"))

    def test_single_segment_star(self):
        # ``bundles/*`` matches a single segment under bundles.
        rx = ap2p._glob_to_regex("bundles/*")
        self.assertTrue(rx.match("bundles/foo"))
        self.assertFalse(rx.match("bundles/foo/bar"))
        self.assertFalse(rx.match("a/bundles/foo"))

    def test_recursive_double_star(self):
        # ``bundles/**`` matches the entire subtree.
        rx = ap2p._glob_to_regex("bundles/**")
        self.assertTrue(rx.match("bundles/foo"))
        self.assertTrue(rx.match("bundles/foo/bar"))
        self.assertTrue(rx.match("bundles/foo/bar/baz"))
        self.assertFalse(rx.match("bundlesfoo"))
        # ``a/bundles/foo`` is anchored so it should NOT match.
        self.assertFalse(rx.match("a/bundles/foo"))

    def test_question_mark(self):
        rx = ap2p._glob_to_regex("a?.md")
        self.assertTrue(rx.match("ab.md"))
        self.assertFalse(rx.match("a.md"))
        self.assertFalse(rx.match("abc.md"))
        # ``?`` does NOT cross path segments.
        self.assertFalse(rx.match("a/b.md"))

    def test_character_class(self):
        rx = ap2p._glob_to_regex("file[123].md")
        self.assertTrue(rx.match("file1.md"))
        self.assertTrue(rx.match("file2.md"))
        self.assertTrue(rx.match("file3.md"))
        self.assertFalse(rx.match("file4.md"))

    def test_unterminated_character_class_falls_back_to_literal(self):
        # The parser should not crash on a stray ``[`` -- it falls back to
        # treating the bracket as a literal character.
        rx = ap2p._glob_to_regex("foo[bar.md")
        # Pattern is treated as basename literal "foo[bar.md".
        self.assertTrue(rx.match("foo[bar.md"))
        self.assertTrue(rx.match("nested/foo[bar.md"))

    def test_pattern_caching(self):
        # The cache should return the same compiled pattern object on a
        # second invocation with the same input.
        first = ap2p._glob_to_regex("*.cache")
        second = ap2p._glob_to_regex("*.cache")
        self.assertIs(first, second)

    def test_double_star_alone(self):
        rx = ap2p._glob_to_regex("**")
        # ``**`` with no slash collapses to ``.*`` and is basename-anchored.
        self.assertTrue(rx.match("foo"))
        self.assertTrue(rx.match("a/b/c"))


class MatchesExclusionTests(unittest.TestCase):
    """End-to-end exclusion checks against POSIX-normalized paths."""

    def setUp(self):
        ap2p._GLOB_REGEX_CACHE.clear()

    def test_empty_patterns_returns_false(self):
        self.assertFalse(ap2p.matches_exclusion("foo/bar.md", []))

    def test_normalizes_backslashes(self):
        # Defensive: a Windows-style backslash path still matches a forward-
        # slash pattern.
        self.assertTrue(
            ap2p.matches_exclusion("foo\\bar.md", ["foo/bar.md"])
        )

    def test_multiple_patterns_short_circuit(self):
        # First match wins; second pattern is never compiled.
        self.assertTrue(
            ap2p.matches_exclusion(
                "engineering/notes.md",
                ["**/notes.md", "**/observations.md"],
            )
        )

    def test_strip_leading_trailing_slashes(self):
        self.assertTrue(
            ap2p.matches_exclusion("/foo/bar.md/", ["foo/bar.md"])
        )

    def test_external_preset_observations(self):
        # The LD17 ``external`` preset includes ``**/observations.md``;
        # confirm the predicate fires for both flat and nested cases.
        patterns = ["**/observations.md", "**/pricing*", "**/strategy*"]
        self.assertTrue(ap2p.matches_exclusion("observations.md", patterns))
        self.assertTrue(
            ap2p.matches_exclusion("foo/observations.md", patterns)
        )
        self.assertTrue(ap2p.matches_exclusion("pricing-2026.md", patterns))
        self.assertTrue(ap2p.matches_exclusion("a/strategy-q1.md", patterns))
        self.assertFalse(ap2p.matches_exclusion("notes.md", patterns))


if __name__ == "__main__":
    unittest.main()
