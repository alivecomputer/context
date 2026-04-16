"""Unit tests for ``alive_mcp.paths`` — path-safety containment helpers.

Covers the T3 acceptance criteria on the path-safety half:

1. ``commonpath``-based check rejects ``../etc/passwd``, ``/etc/passwd``,
   resolved-symlink-escape, and ``//double-slash`` paths.
2. ``commonpath`` check accepts relative paths inside the World.
3. Platform case-fold: on macOS/Windows, ``Foo/Bar`` and ``foo/bar`` are
   treated as equivalent at the boundary.
4. Symlink pointing inside World is allowed; symlink pointing outside
   World is rejected with ``PathEscapeError``.

Uses stdlib ``unittest`` + ``tempfile``. No third-party deps — matches
the T2 convention.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

# Ensure ``python3 -m unittest discover tests`` works from a clean
# checkout without requiring ``pip install -e .``. Mirrors the shim in
# ``test_vendor_smoke.py``.
_SRC_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if os.path.isdir(_SRC_DIR) and _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from alive_mcp.errors import ERR_PATH_ESCAPE, PathEscapeError  # noqa: E402
from alive_mcp.paths import (  # noqa: E402
    is_inside,
    resolve_under,
    safe_join,
)


class PathSafetyRejectsEscapeTests(unittest.TestCase):
    """Acceptance: ``commonpath`` rejects traversal / absolute / slash / symlink escapes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # Set up a fake World root ``root/`` with one walnut inside.
        self.root = os.path.realpath(self.tmp.name)
        self.walnut = os.path.join(self.root, "walnut-a")
        os.makedirs(self.walnut)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parent_traversal_rejected(self) -> None:
        """``walnut-a/../../etc/passwd`` escapes via ``..`` — rejected."""
        with self.assertRaises(PathEscapeError) as ctx:
            safe_join(self.root, "walnut-a", "..", "..", "etc", "passwd")
        # PathEscapeError carries the protocol error code.
        self.assertEqual(ctx.exception.code, ERR_PATH_ESCAPE)

    def test_absolute_path_replaces_root_rejected(self) -> None:
        """``os.path.join(root, "/etc/passwd")`` drops the root.

        realpath + commonpath catches this because ``/etc/passwd`` is
        not under the temp root.
        """
        with self.assertRaises(PathEscapeError):
            safe_join(self.root, "/etc/passwd")

    def test_resolve_under_rejects_absolute_escape(self) -> None:
        """``resolve_under`` on a fully-formed escape path raises."""
        with self.assertRaises(PathEscapeError):
            resolve_under(self.root, "/etc/passwd")

    def test_double_slash_rejected(self) -> None:
        """``//double-slash/etc/passwd`` normalizes to an absolute path.

        POSIX treats ``//etc/passwd`` as absolute. realpath normalizes
        and commonpath rejects.
        """
        with self.assertRaises(PathEscapeError):
            resolve_under(self.root, "//etc/passwd")

    @unittest.skipIf(sys.platform == "win32", "POSIX symlink semantics")
    def test_symlink_escape_rejected(self) -> None:
        """A symlink INSIDE the World that points OUTSIDE is rejected.

        This is the core "realpath both sides" case: a lexical child
        of ``root`` resolves via the symlink to ``/etc/passwd``, and
        the commonpath check operates on the resolved target.
        """
        link = os.path.join(self.walnut, "danger")
        os.symlink("/etc/passwd", link)

        with self.assertRaises(PathEscapeError):
            resolve_under(self.root, link)
        self.assertFalse(is_inside(self.root, link))

    def test_sibling_root_rejected_not_prefix_bug(self) -> None:
        """CVE-2025-53109 regression: ``<root>_sibling`` is NOT inside ``<root>``.

        If we used ``startswith``, a directory named ``<root>_sibling``
        would pass the prefix check. ``commonpath`` rejects because the
        two paths do not share a common path ancestor equal to the
        root.
        """
        # Make a sibling directory next to ``self.root`` that shares a
        # prefix but is NOT a descendant.
        sibling = self.root + "_sibling"
        os.makedirs(sibling)
        try:
            with self.assertRaises(PathEscapeError):
                resolve_under(self.root, sibling)
            self.assertFalse(is_inside(self.root, sibling))
        finally:
            os.rmdir(sibling)


class PathSafetyAcceptsInsideTests(unittest.TestCase):
    """Acceptance: ``commonpath`` accepts relative + absolute paths inside the World."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.walnut = os.path.join(self.root, "walnut-a")
        os.makedirs(os.path.join(self.walnut, "_kernel"))
        self.kernel_key = os.path.join(self.walnut, "_kernel", "key.md")
        with open(self.kernel_key, "w", encoding="utf-8") as f:
            f.write("# key\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_safe_join_relative_inside(self) -> None:
        """``safe_join(root, "walnut-a", "_kernel", "key.md")`` resolves cleanly."""
        resolved = safe_join(self.root, "walnut-a", "_kernel", "key.md")
        self.assertEqual(os.path.realpath(self.kernel_key), resolved)

    def test_resolve_under_absolute_inside(self) -> None:
        """A pre-built absolute path that lives inside the root is accepted."""
        resolved = resolve_under(self.root, self.kernel_key)
        self.assertEqual(os.path.realpath(self.kernel_key), resolved)

    def test_is_inside_boolean(self) -> None:
        """``is_inside`` returns True without raising on contained paths."""
        self.assertTrue(is_inside(self.root, self.kernel_key))
        self.assertTrue(is_inside(self.root, self.walnut))
        self.assertTrue(is_inside(self.root, self.root))

    def test_root_itself_is_inside(self) -> None:
        """Boundary case: the root IS contained by itself (commonpath == root)."""
        resolved = resolve_under(self.root, self.root)
        self.assertEqual(self.root, resolved)

    @unittest.skipIf(sys.platform == "win32", "POSIX symlink semantics")
    def test_symlink_inside_world_allowed(self) -> None:
        """Symlink whose target is INSIDE the World is allowed.

        Sibling walnut ``walnut-b`` gets a symlink ``alias`` that
        points at ``walnut-a/_kernel/key.md``. Resolving ``alias``
        ends at a path inside the root, so it's accepted.
        """
        walnut_b = os.path.join(self.root, "walnut-b")
        os.makedirs(walnut_b)
        alias = os.path.join(walnut_b, "alias")
        os.symlink(self.kernel_key, alias)

        resolved = resolve_under(self.root, alias)
        self.assertEqual(os.path.realpath(self.kernel_key), resolved)
        self.assertTrue(is_inside(self.root, alias))


class PathSafetyCaseFoldTests(unittest.TestCase):
    """Acceptance: darwin/win32 case-fold at the boundary.

    On macOS/Windows (the case-insensitive default filesystems), the
    containment check treats ``Foo/Bar`` and ``foo/bar`` as equivalent
    — ``os.path.normcase`` lowercases both sides before the
    ``commonpath`` compare. On Linux, the check is case-sensitive by
    policy (v0.1).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @unittest.skipUnless(
        sys.platform in ("darwin", "win32"),
        "case-fold policy only applies on HFS+/APFS/NTFS",
    )
    def test_case_variant_root_accepted(self) -> None:
        """A candidate whose case differs from the root is accepted on darwin/win32.

        We don't actually rename the directory on disk — ``realpath``
        would canonicalize the case back — so we construct the
        containment check by passing the ROOT with an altered case
        prefix. ``normcase`` lowercases both sides; commonpath then
        agrees.
        """
        # Build an inside path and then hand the SAME root with its case
        # flipped for the last segment. The temp path on macOS looks
        # like ``/private/var/folders/.../T/tmpXYZ`` — take the last
        # segment (``tmpXYZ``) and swap its case.
        parent, tail = os.path.split(self.root)
        self.assertTrue(tail)
        flipped = os.path.join(parent, tail.swapcase())

        # Sanity check: on case-insensitive FS, ``flipped`` resolves to
        # the same realpath as ``self.root``.
        self.assertEqual(
            os.path.realpath(flipped).lower(),
            self.root.lower(),
        )

        # The contained check should see them as equivalent.
        inside_path = os.path.join(self.root, "child")
        os.makedirs(inside_path)
        self.assertTrue(is_inside(flipped, inside_path))


class PathSafetyCommonpathValueErrorTests(unittest.TestCase):
    """``commonpath`` raises on cross-drive inputs — treat as ``not inside``.

    Hard to exercise on POSIX (all paths share ``/``), so this test is
    POSIX-skipped and kept as a docstring hint. The branch is still
    covered by the unit where we monkeypatch ``os.path.commonpath`` to
    raise.
    """

    def test_commonpath_valueerror_treated_as_outside(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as root:
            real_root = os.path.realpath(root)
            # Monkeypatch commonpath (on the module under test — it
            # imports ``os`` and calls ``os.path.commonpath``) to
            # simulate the Windows different-drives ValueError.
            with mock.patch(
                "alive_mcp.paths.os.path.commonpath",
                side_effect=ValueError("different drives"),
            ):
                self.assertFalse(is_inside(real_root, real_root))
                with self.assertRaises(PathEscapeError):
                    resolve_under(real_root, real_root)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
