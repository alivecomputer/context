#!/usr/bin/env python3
"""LD22 tar-safety acceptance suite (fn-7-7cw.12).

Hostile-tar coverage for ``safe_tar_extract`` (the LD22-conformant entry point
used by the receive pipeline). All 10 LD22 rejection cases plus the PAX header
pass-through and a regular file/dir baseline. Each rejection case asserts:

1. ``ValueError`` is raised
2. ``os.listdir(dest)`` is empty after the exception (zero filesystem writes)

Fixture tars are built programmatically with ``tarfile.TarInfo`` + ``BytesIO``
so the test suite stays hermetic and offline. Stdlib only.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_tar_safety -v
"""

import importlib.util
import io
import os
import sys
import tarfile
import tempfile
import unittest
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Module loading -- alive-p2p.py has a hyphen so import via importlib.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import walnut_paths  # noqa: E402,F401  -- pre-cache for the loader

_AP2P_PATH = os.path.join(_SCRIPTS, "alive-p2p.py")
_spec = importlib.util.spec_from_file_location("alive_p2p", _AP2P_PATH)
ap2p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap2p)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _build_tar(members):
    # type: (List[Dict[str, Any]]) -> bytes
    """Build a gzipped tar in memory from a list of member specs.

    Each spec dict supports keys:
        name      -- tar member name (str, required)
        type      -- tarfile.<TYPE> constant (default REGTYPE)
        data      -- file body bytes (default b"")
        linkname  -- link target (for SYMTYPE / LNKTYPE)
        size      -- override the size header (default len(data))
        mode      -- file mode (default 0o644 for files, 0o755 for dirs)
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for spec in members:
            ti = tarfile.TarInfo(name=spec["name"])
            ti.type = spec.get("type", tarfile.REGTYPE)
            # Directory members need the execute bit set or the extractor
            # produces an unreadable subdir. File members default to 0o644.
            default_mode = 0o755 if ti.type == tarfile.DIRTYPE else 0o644
            ti.mode = spec.get("mode", default_mode)
            if "linkname" in spec:
                ti.linkname = spec["linkname"]
            data = spec.get("data", b"")
            ti.size = spec.get("size", len(data))
            payload = io.BytesIO(data) if ti.type == tarfile.REGTYPE else None
            tar.addfile(ti, payload)
    return buf.getvalue()


def _write_tar(parent_dir, members, name="evil.tar.gz"):
    # type: (str, List[Dict[str, Any]], str) -> str
    """Write a fixture tar to ``parent_dir`` and return its absolute path."""
    archive = os.path.join(parent_dir, name)
    with open(archive, "wb") as f:
        f.write(_build_tar(members))
    return archive


# ---------------------------------------------------------------------------
# LD22 rejection cases (10) -- each must raise ValueError and leave dest empty
# ---------------------------------------------------------------------------


class LD22RejectionTests(unittest.TestCase):
    """LD22 acceptance contract: 10 rejection cases.

    Each test crafts a hostile tar in memory, attempts extraction, and
    asserts ``ValueError`` AND ``os.listdir(dest) == []`` post-exception.
    """

    def _assert_rejected(self, members, expected_substring=None):
        # type: (List[Dict[str, Any]], Optional[str]) -> None
        with tempfile.TemporaryDirectory() as parent:
            archive = _write_tar(parent, members)
            dest = os.path.join(parent, "out")
            os.makedirs(dest)
            with self.assertRaises(ValueError) as ctx:
                ap2p.safe_tar_extract(archive, dest)
            if expected_substring is not None:
                self.assertIn(
                    expected_substring, str(ctx.exception),
                    "expected {0!r} in error, got: {1!r}".format(
                        expected_substring, str(ctx.exception),
                    ),
                )
            # LD22 hard guarantee: zero filesystem writes on rejection.
            self.assertEqual(
                os.listdir(dest), [],
                "dest directory not empty after rejection: {0}".format(
                    os.listdir(dest)
                ),
            )

    def test_rejects_path_traversal(self):
        """Case 1: ``../etc/passwd`` rejected with ValueError, dest empty."""
        self._assert_rejected(
            [{"name": "../etc/passwd", "data": b"hostile"}],
            expected_substring="Parent-dir segment",
        )

    def test_rejects_absolute_posix(self):
        """Case 2: absolute POSIX path ``/etc/passwd`` rejected."""
        self._assert_rejected(
            [{"name": "/etc/passwd", "data": b"hostile"}],
            expected_substring="Absolute path member",
        )

    def test_rejects_windows_drive_letter(self):
        """Case 3: Windows drive letter ``C:foo`` rejected."""
        self._assert_rejected(
            [{"name": "C:foo", "data": b"hostile"}],
            expected_substring="Absolute path member",
        )

    def test_rejects_symlink_member(self):
        """Case 4: ANY symlink member rejected outright (LD22 v10)."""
        self._assert_rejected(
            [
                {
                    "name": "link.md",
                    "type": tarfile.SYMTYPE,
                    "linkname": "target.md",
                },
            ],
            expected_substring="Symlink/hardlink not allowed",
        )

    def test_rejects_symlink_member_escaping(self):
        """Symlinks pointing outside dest also rejected outright."""
        self._assert_rejected(
            [
                {
                    "name": "evil-link.md",
                    "type": tarfile.SYMTYPE,
                    "linkname": "../../../etc/passwd",
                },
            ],
            expected_substring="Symlink/hardlink not allowed",
        )

    def test_rejects_hardlink_member(self):
        """Case 5: ANY hardlink member rejected outright."""
        self._assert_rejected(
            [
                {
                    "name": "hard.md",
                    "type": tarfile.LNKTYPE,
                    "linkname": "target.md",
                },
            ],
            expected_substring="Symlink/hardlink not allowed",
        )

    def test_rejects_device_member(self):
        """Case 6: character / block / fifo device members rejected."""
        self._assert_rejected(
            [{"name": "evil-device", "type": tarfile.CHRTYPE}],
            expected_substring="Device or fifo member",
        )

    def test_rejects_block_device_member(self):
        """Block device variant of LD22 case 6."""
        self._assert_rejected(
            [{"name": "evil-block", "type": tarfile.BLKTYPE}],
            expected_substring="Device or fifo member",
        )

    def test_rejects_fifo_member(self):
        """FIFO variant of LD22 case 6."""
        self._assert_rejected(
            [{"name": "evil-fifo", "type": tarfile.FIFOTYPE}],
            expected_substring="Device or fifo member",
        )

    def test_rejects_size_bomb(self):
        """Case 7: cumulative tar member size exceeds the LD22 cap.

        Patches ``_LD22_MAX_TOTAL_BYTES`` down to a small value during the
        test so we don't have to write half a gigabyte of zeros to the
        fixture. The pre-validation logic is identical at any cap value;
        the spec contract is "reject when the sum exceeds the cap" and
        500 MB is just the production constant.
        """
        from unittest import mock
        with tempfile.TemporaryDirectory() as parent:
            # Three regular 1 KB files; total 3 KB. Patched cap is 2 KB so
            # the validator will reject before extraction.
            members = [
                {"name": "f-{0}.md".format(i), "data": b"x" * 1024}
                for i in range(3)
            ]
            archive = _write_tar(parent, members)
            dest = os.path.join(parent, "out")
            os.makedirs(dest)
            with mock.patch.object(ap2p, "_LD22_MAX_TOTAL_BYTES", 2 * 1024):
                with self.assertRaises(ValueError) as ctx:
                    ap2p.safe_tar_extract(archive, dest)
            self.assertIn("expands to >", str(ctx.exception))
            self.assertEqual(os.listdir(dest), [])

    def test_rejects_backslash_in_name(self):
        """Case 8: ``foo\\bar.md`` (backslash in member name) rejected."""
        self._assert_rejected(
            [{"name": "foo\\bar.md", "data": b"x"}],
            expected_substring="Backslash in member name",
        )

    def test_rejects_duplicate_effective_path(self):
        """Case 9: ``foo`` and ``./foo`` are the same effective path."""
        self._assert_rejected(
            [
                {"name": "foo", "data": b"first"},
                {"name": "./foo", "data": b"second"},
            ],
            expected_substring="Duplicate effective member path",
        )

    def test_rejects_intermediate_dot_segment(self):
        """Case 10a: intermediate ``.`` segments are rejected.

        ``foo/./bar.md`` is technically equivalent to ``foo/bar.md`` but
        the LD22 spec rejects intermediate dot segments to keep the
        normalisation contract simple and predictable.
        """
        self._assert_rejected(
            [{"name": "foo/./bar.md", "data": b"x"}],
            expected_substring="Intermediate dot-segment",
        )

    def test_rejects_unsupported_member_type(self):
        """Case 10b: member-type rejection for non-file/non-dir/non-metadata.

        Crafted via tarfile.CONTTYPE (contiguous file -- valid POSIX tar
        type but not in our regular-file allowlist).
        """
        # CONTTYPE is rare; tarfile.is_file() actually returns True for
        # CONTTYPE so it would slip through the allowlist. To get a member
        # type that fails BOTH ``isfile()`` AND ``isdir()`` AND is not a
        # link/device/fifo/metadata type, we need to fake it. The cleanest
        # path is to invent a custom type byte.
        with tempfile.TemporaryDirectory() as parent:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                ti = tarfile.TarInfo(name="weird.bin")
                # Use a type byte that no tar method classifies as file,
                # dir, link, sym, chr, blk, or fifo. ``b"Z"`` is unused.
                ti.type = b"Z"
                ti.size = 0
                tar.addfile(ti, io.BytesIO(b""))
            archive = os.path.join(parent, "weird.tar.gz")
            with open(archive, "wb") as f:
                f.write(buf.getvalue())
            dest = os.path.join(parent, "out")
            os.makedirs(dest)
            with self.assertRaises(ValueError) as ctx:
                ap2p.safe_tar_extract(archive, dest)
            # The exact phrase depends on which check fires first. Both
            # ``Unsupported tar member type`` and the symlink/device
            # branches are acceptable -- LD22 just requires SOME ValueError
            # before any write.
            self.assertEqual(os.listdir(dest), [])


# ---------------------------------------------------------------------------
# LD22 pass-through cases -- benign tars must extract cleanly
# ---------------------------------------------------------------------------


class LD22PassThroughTests(unittest.TestCase):
    """LD22 acceptance: benign cases extract without error."""

    def test_accepts_pax_header_members(self):
        """Tars built with PAX headers (the modern POSIX default since
        Python 3.0) MUST pass through. The pax_headers carrier member is
        not a file write, just metadata, and the LD22 validator skips it.
        """
        with tempfile.TemporaryDirectory() as parent:
            # Force pax_format and attach pax_headers to the file member.
            buf = io.BytesIO()
            with tarfile.open(
                fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
            ) as tar:
                ti = tarfile.TarInfo(name="paxified.md")
                ti.size = 5
                ti.pax_headers = {"path": "paxified.md", "comment": "hi"}
                tar.addfile(ti, io.BytesIO(b"hello"))
            archive = os.path.join(parent, "pax.tar.gz")
            with open(archive, "wb") as f:
                f.write(buf.getvalue())
            dest = os.path.join(parent, "out")
            os.makedirs(dest)
            ap2p.safe_tar_extract(archive, dest)
            self.assertEqual(os.listdir(dest), ["paxified.md"])
            with open(os.path.join(dest, "paxified.md"), "rb") as f:
                self.assertEqual(f.read(), b"hello")

    def test_accepts_regular_file_baseline(self):
        """A plain regular file extracts and the dest contains it."""
        with tempfile.TemporaryDirectory() as parent:
            archive = _write_tar(
                parent, [{"name": "hello.md", "data": b"hi"}],
            )
            dest = os.path.join(parent, "out")
            os.makedirs(dest)
            ap2p.safe_tar_extract(archive, dest)
            self.assertEqual(os.listdir(dest), ["hello.md"])

    def test_accepts_directory_member(self):
        """A directory member extracts and contents are accessible."""
        with tempfile.TemporaryDirectory() as parent:
            archive = _write_tar(
                parent,
                [
                    {"name": "subdir", "type": tarfile.DIRTYPE},
                    {"name": "subdir/file.md", "data": b"x"},
                ],
            )
            dest = os.path.join(parent, "out")
            os.makedirs(dest)
            ap2p.safe_tar_extract(archive, dest)
            self.assertTrue(os.path.isdir(os.path.join(dest, "subdir")))
            self.assertTrue(
                os.path.isfile(os.path.join(dest, "subdir", "file.md"))
            )

    def test_accepts_member_count_below_cap(self):
        """A tar with many regular files below the 10000 cap extracts."""
        with tempfile.TemporaryDirectory() as parent:
            members = [
                {"name": "f-{0:04d}.md".format(i), "data": b"x"}
                for i in range(50)
            ]
            archive = _write_tar(parent, members)
            dest = os.path.join(parent, "out")
            os.makedirs(dest)
            ap2p.safe_tar_extract(archive, dest)
            self.assertEqual(len(os.listdir(dest)), 50)


# ---------------------------------------------------------------------------
# LD22 cap edge cases
# ---------------------------------------------------------------------------


class LD22CapTests(unittest.TestCase):
    """Member-count and size caps fire deterministically."""

    def test_rejects_member_count_above_cap(self):
        """A tar with member count above the LD22 cap is rejected.

        Patches ``_LD22_MAX_MEMBERS`` down to a small value during the test
        to keep the fixture small. The pre-validation contract is "reject
        when len(members) > cap" regardless of the cap value; 10000 is the
        production constant.
        """
        from unittest import mock
        with tempfile.TemporaryDirectory() as parent:
            members = [
                {"name": "f-{0:03d}.md".format(i), "data": b"x"}
                for i in range(11)
            ]
            archive = _write_tar(parent, members)
            dest = os.path.join(parent, "out")
            os.makedirs(dest)
            with mock.patch.object(ap2p, "_LD22_MAX_MEMBERS", 10):
                with self.assertRaises(ValueError) as ctx:
                    ap2p.safe_tar_extract(archive, dest)
            self.assertIn("members", str(ctx.exception))
            self.assertEqual(os.listdir(dest), [])


# ---------------------------------------------------------------------------
# Public LD22 alias
# ---------------------------------------------------------------------------


class LD22AliasTests(unittest.TestCase):
    """``safe_extractall`` is the LD22 spec name; it must alias the
    hardened extractor so external callers can rely on either name.
    """

    def test_safe_extractall_aliases_safe_tar_extract(self):
        self.assertIs(ap2p.safe_extractall, ap2p.safe_tar_extract)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
