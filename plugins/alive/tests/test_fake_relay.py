#!/usr/bin/env python3
"""Tests for the FakeRelay in-memory abstraction (fn-7-7cw.11).

Verifies the LD25 wire-protocol surface (upload, download, list_pending,
delete, register_peer) without touching the network or disk.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_fake_relay -v
"""

import os
import sys
import unittest


_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fake_relay import FakeRelay, FakeRelayError  # noqa: E402


class FakeRelayUploadDownloadTests(unittest.TestCase):

    def test_upload_download_round_trip(self):
        relay = FakeRelay()
        path = relay.upload("alice", "bob", "pkg.walnut", b"hello world")
        self.assertEqual(path, "inbox/bob/pkg.walnut")
        data = relay.download("alice", "bob", "pkg.walnut")
        self.assertEqual(data, b"hello world")

    def test_download_missing_raises(self):
        relay = FakeRelay()
        with self.assertRaises(FakeRelayError):
            relay.download("alice", "bob", "missing.walnut")

    def test_upload_rejects_non_bytes(self):
        relay = FakeRelay()
        with self.assertRaises(TypeError):
            relay.upload("alice", "bob", "pkg.walnut", "string-data")

    def test_upload_rejects_path_separator(self):
        relay = FakeRelay()
        with self.assertRaises(ValueError):
            relay.upload("alice", "bob", "../escape.walnut", b"data")
        with self.assertRaises(ValueError):
            relay.upload("alice", "bob", "sub/file.walnut", b"data")


class FakeRelayListPendingTests(unittest.TestCase):

    def test_list_pending_filters_by_peer(self):
        relay = FakeRelay()
        relay.upload("alice", "bob", "one.walnut", b"1")
        relay.upload("alice", "bob", "two.walnut", b"2")
        relay.upload("alice", "carol", "three.walnut", b"3")
        # Filter to bob only
        bob_files = relay.list_pending("alice", peer="bob")
        self.assertEqual(bob_files, ["one.walnut", "two.walnut"])
        # Without peer filter we see everything for alice
        all_alice = relay.list_pending("alice")
        self.assertEqual(all_alice, ["one.walnut", "three.walnut", "two.walnut"])
        # An owner with no blobs returns []
        self.assertEqual(relay.list_pending("dave"), [])

    def test_list_pending_sorted(self):
        relay = FakeRelay()
        for fname in ("zeta.walnut", "alpha.walnut", "mid.walnut"):
            relay.upload("alice", "bob", fname, b"x")
        result = relay.list_pending("alice", peer="bob")
        self.assertEqual(result, ["alpha.walnut", "mid.walnut", "zeta.walnut"])


class FakeRelayDeleteTests(unittest.TestCase):

    def test_delete_removes_blob(self):
        relay = FakeRelay()
        relay.upload("alice", "bob", "pkg.walnut", b"data")
        relay.delete("alice", "bob", "pkg.walnut")
        with self.assertRaises(FakeRelayError):
            relay.download("alice", "bob", "pkg.walnut")
        self.assertEqual(relay.list_pending("alice", peer="bob"), [])

    def test_delete_missing_raises(self):
        relay = FakeRelay()
        with self.assertRaises(FakeRelayError):
            relay.delete("alice", "bob", "ghost.walnut")


class FakeRelayPeerTests(unittest.TestCase):

    def test_register_peer_stores_pubkey(self):
        relay = FakeRelay()
        pem = b"-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----\n"
        relay.register_peer("alice", "bob", pem)
        self.assertTrue(relay.has_peer("alice", "bob"))
        self.assertEqual(relay.get_peer_pubkey("alice", "bob"), pem)
        self.assertFalse(relay.has_peer("alice", "carol"))
        with self.assertRaises(FakeRelayError):
            relay.get_peer_pubkey("alice", "carol")

    def test_register_peer_rejects_non_bytes(self):
        relay = FakeRelay()
        with self.assertRaises(TypeError):
            relay.register_peer("alice", "bob", "not-bytes")


if __name__ == "__main__":
    unittest.main()
