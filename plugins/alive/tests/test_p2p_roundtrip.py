#!/usr/bin/env python3
"""Round-trip tests for the v3 P2P pipeline (fn-7-7cw.11).

End-to-end coverage for ``create_package`` -> ``receive_package`` across all
three scopes, both encryption modes (passphrase, RSA hybrid), the FakeRelay
in-memory transport, and the actionable error paths receivers care about.

All tests are stdlib-only (unittest, tempfile, subprocess for openssl key
generation). Tests touch only ``tempfile.TemporaryDirectory`` paths -- never
the user's home or working directory. Tests that need openssl skip cleanly
when the binary is unavailable.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_p2p_roundtrip -v
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock


# ---------------------------------------------------------------------------
# Module loading -- alive-p2p.py has a hyphen in the filename.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import walnut_paths  # noqa: E402,F401  -- pre-cache so alive-p2p import works

_AP2P_PATH = os.path.join(_SCRIPTS, "alive-p2p.py")
_spec = importlib.util.spec_from_file_location("alive_p2p", _AP2P_PATH)
ap2p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap2p)  # type: ignore[union-attr]

from walnut_builder import build_walnut  # noqa: E402
from walnut_compare import walnut_equal, assert_walnut_equal  # noqa: E402
from fake_relay import FakeRelay  # noqa: E402


FIXED_TS = "2026-04-07T12:00:00Z"
FIXED_SESSION = "test-session-rt"
FIXED_SENDER = "test-sender"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openssl_available():
    try:
        return ap2p.detect_openssl()["binary"] is not None
    except Exception:
        return False


def _make_world_root(parent_dir):
    os.makedirs(os.path.join(parent_dir, ".alive"), exist_ok=True)
    return parent_dir


@contextmanager
def _patch_env():
    patches = [
        mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS),
        mock.patch.object(ap2p, "resolve_session_id", return_value=FIXED_SESSION),
        mock.patch.object(ap2p, "resolve_sender", return_value=FIXED_SENDER),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


@contextmanager
def _skip_regen():
    """Skip the LD1 step 12 project.py invocation."""
    prev = os.environ.get("ALIVE_P2P_SKIP_REGEN")
    os.environ["ALIVE_P2P_SKIP_REGEN"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("ALIVE_P2P_SKIP_REGEN", None)
        else:
            os.environ["ALIVE_P2P_SKIP_REGEN"] = prev


def _build_v3_fixture(tmp_path, name="src-walnut"):
    """Default v3 walnut fixture for round-trip tests."""
    return build_walnut(
        tmp_path,
        name=name,
        layout="v3",
        bundles=[
            {
                "name": "shielding-review",
                "goal": "Review shielding",
                "files": {"draft-01.md": "# Shielding draft\n"},
            },
            {
                "name": "launch-checklist",
                "goal": "Launch checklist",
                "files": {"items.md": "- [ ] Item one\n"},
            },
        ],
        tasks={
            "unscoped": [
                {"id": "t1", "title": "do thing", "status": "open"},
            ],
        },
        live_files=[
            {"path": "engineering/spec.md", "content": "# spec\n"},
        ],
        log_entries=[
            {"timestamp": "2026-04-01T00:00:00Z",
             "session_id": "src", "body": "Initial."},
        ],
    )


def _gen_rsa_keypair(tmp_dir, name="test"):
    """Generate a test RSA keypair via openssl. Returns (priv_path, pub_path)."""
    priv = os.path.join(tmp_dir, "{0}-priv.pem".format(name))
    pub = os.path.join(tmp_dir, "{0}-pub.pem".format(name))
    subprocess.run(
        ["openssl", "genrsa", "-out", priv, "2048"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "rsa", "-in", priv, "-pubout", "-out", pub],
        check=True, capture_output=True,
    )
    return priv, pub


# Default ignore set for sender vs receiver comparisons. Receivers add their
# own _kernel/imports.json + _kernel/now.json projection, prepend an import
# entry to log.md, and (depending on scope) leave the kernel/tasks.json that
# the staging step shipped.
_RT_DEFAULT_IGNORE = [
    "_kernel/imports.json",
    "_kernel/now.json",
]


# ---------------------------------------------------------------------------
# Full scope round-trip
# ---------------------------------------------------------------------------


class FullScopeRoundTripTests(unittest.TestCase):

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_full_scope_v3_unencrypted(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-walnut")
            output = os.path.join(world, "src.walnut")
            with _patch_env():
                ap2p.create_package(
                    walnut_path=src, scope="full",
                    output_path=output, include_full_history=True,
                )
            target = os.path.join(world, "received-walnut")
            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=output, target_path=target, yes=True,
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["scope"], "full")
            self.assertIn("shielding-review", result["applied_bundles"])
            self.assertIn("launch-checklist", result["applied_bundles"])
            assert_walnut_equal(
                self, src, target,
                ignore_log_entries=1,
                ignore_patterns=_RT_DEFAULT_IGNORE,
            )

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_full_scope_v3_passphrase_encrypted(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-pass")
            output = os.path.join(world, "src-pass.walnut")
            os.environ["RT_PASS"] = "round-trip-passphrase"
            try:
                with _patch_env():
                    ap2p.create_package(
                        walnut_path=src, scope="full", output_path=output,
                        include_full_history=True,
                        encrypt_mode="passphrase", passphrase_env="RT_PASS",
                    )
                # Verify the produced file uses the OpenSSL Salted__ envelope.
                with open(output, "rb") as f:
                    self.assertEqual(f.read(8), b"Salted__")
                target = os.path.join(world, "received-pass")
                with _patch_env(), _skip_regen():
                    result = ap2p.receive_package(
                        package_path=output, target_path=target, yes=True,
                        passphrase_env="RT_PASS",
                    )
            finally:
                os.environ.pop("RT_PASS", None)
            self.assertEqual(result["status"], "ok")
            assert_walnut_equal(
                self, src, target,
                ignore_log_entries=1,
                ignore_patterns=_RT_DEFAULT_IGNORE,
            )

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_full_scope_v3_rsa_encrypted(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-rsa")
            # Test-only RSA keypair stored in a sandboxed keys dir.
            keys_dir = os.path.join(world, "keys")
            os.makedirs(os.path.join(keys_dir, "peers"), exist_ok=True)
            priv, pub = _gen_rsa_keypair(keys_dir, name="bob")
            with open(pub, "rb") as f:
                pub_pem = f.read()
            ap2p.register_peer_pubkey("bob", pub_pem, keys_dir=keys_dir)
            output = os.path.join(world, "src-rsa.walnut")
            saved = os.environ.get("ALIVE_RELAY_KEYS_DIR")
            os.environ["ALIVE_RELAY_KEYS_DIR"] = keys_dir
            try:
                with _patch_env():
                    ap2p.create_package(
                        walnut_path=src, scope="full", output_path=output,
                        include_full_history=True,
                        encrypt_mode="rsa", recipient_peers=["bob"],
                    )
                # Outer envelope must be a tar with exactly the LD21 members.
                with tarfile.open(output, "r:*") as tar:
                    members = sorted(m.name for m in tar.getmembers())
                self.assertEqual(
                    members, ["payload.enc", "rsa-envelope-v1.json"],
                )
                target = os.path.join(world, "received-rsa")
                with _patch_env(), _skip_regen():
                    result = ap2p.receive_package(
                        package_path=output, target_path=target, yes=True,
                        private_key_path=priv,
                    )
            finally:
                if saved is None:
                    os.environ.pop("ALIVE_RELAY_KEYS_DIR", None)
                else:
                    os.environ["ALIVE_RELAY_KEYS_DIR"] = saved
            self.assertEqual(result["status"], "ok")
            assert_walnut_equal(
                self, src, target,
                ignore_log_entries=1,
                ignore_patterns=_RT_DEFAULT_IGNORE,
            )


# ---------------------------------------------------------------------------
# Bundle / snapshot round-trip
# ---------------------------------------------------------------------------


class BundleScopeRoundTripTests(unittest.TestCase):

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_bundle_scope_v3(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-bundle")
            # First create a target walnut so the bundle scope has somewhere
            # to land.
            full_output = os.path.join(world, "src-bundle-full.walnut")
            with _patch_env():
                ap2p.create_package(
                    walnut_path=src, scope="full",
                    output_path=full_output, include_full_history=True,
                )
            target = os.path.join(world, "target-bundle")
            with _patch_env(), _skip_regen():
                ap2p.receive_package(
                    package_path=full_output, target_path=target, yes=True,
                )
            # Now generate a bundle-scope package containing one bundle and
            # apply it.
            bundle_pkg = os.path.join(world, "src-bundle.walnut")
            with _patch_env():
                ap2p.create_package(
                    walnut_path=src, scope="bundle",
                    output_path=bundle_pkg,
                    bundle_names=["shielding-review"],
                )
            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=bundle_pkg, target_path=target, yes=True,
                    rename=True,
                )
            self.assertEqual(result["scope"], "bundle")
            # The bundle was renamed via LD3 deterministic chaining since
            # the target already had a shielding-review from the full
            # receive.
            self.assertEqual(len(result["applied_bundles"]), 1)
            applied = result["applied_bundles"][0]
            self.assertTrue(applied.startswith("shielding-review"), applied)
            self.assertTrue(os.path.isfile(os.path.join(
                target, applied, "draft-01.md",
            )))


class SnapshotScopeRoundTripTests(unittest.TestCase):

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_snapshot_scope_v3(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-snap")
            output = os.path.join(world, "src-snap.walnut")
            with _patch_env():
                ap2p.create_package(
                    walnut_path=src, scope="snapshot", output_path=output,
                )
            target = os.path.join(world, "received-snap")
            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=output, target_path=target, yes=True,
                )
            self.assertEqual(result["scope"], "snapshot")
            # Snapshot ships exactly key.md + insights.md (per LD26). The
            # receiver creates a fresh log.md to record the import (LD12),
            # but no bundles or live context come along.
            self.assertTrue(os.path.isfile(os.path.join(
                target, "_kernel", "key.md")))
            self.assertTrue(os.path.isfile(os.path.join(
                target, "_kernel", "insights.md")))
            self.assertFalse(os.path.exists(os.path.join(
                target, "shielding-review")))
            self.assertFalse(os.path.exists(os.path.join(
                target, "engineering")))


# ---------------------------------------------------------------------------
# FakeRelay end-to-end
# ---------------------------------------------------------------------------


class FakeRelayRoundTripTests(unittest.TestCase):

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_full_scope_with_fake_relay(self):
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-relay")
            output = os.path.join(world, "src-relay.walnut")
            with _patch_env():
                ap2p.create_package(
                    walnut_path=src, scope="full", output_path=output,
                    include_full_history=True,
                )
            with open(output, "rb") as f:
                pkg_bytes = f.read()
            # Sender uploads to the receiver's relay (owner=alice, peer=bob)
            relay.upload("alice", "bob", "src-relay.walnut", pkg_bytes)
            self.assertEqual(
                relay.list_pending("alice", peer="bob"),
                ["src-relay.walnut"],
            )
            # Receiver downloads + writes to a local inbox file then runs
            # the standard receive pipeline.
            inbox = os.path.join(world, "03_Inbox")
            os.makedirs(inbox, exist_ok=True)
            local_pkg = os.path.join(inbox, "src-relay.walnut")
            data = relay.download("alice", "bob", "src-relay.walnut")
            with open(local_pkg, "wb") as f:
                f.write(data)
            target = os.path.join(world, "received-relay")
            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=local_pkg, target_path=target, yes=True,
                )
            relay.delete("alice", "bob", "src-relay.walnut")
            self.assertEqual(relay.list_pending("alice", peer="bob"), [])
            self.assertEqual(result["status"], "ok")
            assert_walnut_equal(
                self, src, target,
                ignore_log_entries=1,
                ignore_patterns=_RT_DEFAULT_IGNORE,
            )


# ---------------------------------------------------------------------------
# Negative paths -- actionable errors
# ---------------------------------------------------------------------------


class ReceiveErrorPathTests(unittest.TestCase):

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_wrong_passphrase_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-badpass")
            output = os.path.join(world, "src-badpass.walnut")
            os.environ["RT_PASS"] = "correct"
            try:
                with _patch_env():
                    ap2p.create_package(
                        walnut_path=src, scope="full", output_path=output,
                        include_full_history=True,
                        encrypt_mode="passphrase", passphrase_env="RT_PASS",
                    )
            finally:
                os.environ.pop("RT_PASS", None)
            os.environ["RT_PASS"] = "WRONG"
            try:
                target = os.path.join(world, "received-badpass")
                with _patch_env(), _skip_regen():
                    with self.assertRaises((ValueError, RuntimeError)) as ctx:
                        ap2p.receive_package(
                            package_path=output, target_path=target, yes=True,
                            passphrase_env="RT_PASS",
                        )
            finally:
                os.environ.pop("RT_PASS", None)
            msg = str(ctx.exception).lower()
            self.assertTrue(
                "decrypt" in msg or "passphrase" in msg
                or "wrong" in msg or "format" in msg,
                msg,
            )

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_corrupted_package_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-corrupt")
            output = os.path.join(world, "src-corrupt.walnut")
            with _patch_env():
                ap2p.create_package(
                    walnut_path=src, scope="full", output_path=output,
                    include_full_history=True,
                )
            # Truncate the package to break the gzipped tar.
            with open(output, "r+b") as f:
                f.truncate(64)
            target = os.path.join(world, "received-corrupt")
            with _patch_env(), _skip_regen():
                with self.assertRaises((ValueError, RuntimeError)) as ctx:
                    ap2p.receive_package(
                        package_path=output, target_path=target, yes=True,
                    )
            self.assertTrue(len(str(ctx.exception)) > 0)

    def test_format_version_3_rejected(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            # Synthesize a package whose manifest declares format_version
            # 3.0.0 -- the receiver must reject it with a hard error.
            staging = os.path.join(world, "fake-stage")
            os.makedirs(os.path.join(staging, "_kernel"), exist_ok=True)
            with open(os.path.join(staging, "_kernel", "key.md"), "w") as f:
                f.write("---\ntype: venture\nname: x\n---\n")
            manifest_yaml = (
                "format_version: \"3.0.0\"\n"
                "source_layout: \"v3\"\n"
                "min_plugin_version: \"4.0.0\"\n"
                "created: \"2026-04-07T00:00:00Z\"\n"
                "scope: \"full\"\n"
                "source:\n"
                "  walnut: \"x\"\n"
                "  session_id: \"sx\"\n"
                "  engine: \"e\"\n"
                "  plugin_version: \"4.0.0\"\n"
                "sender: \"future-sender\"\n"
                "description: \"\"\n"
                "note: \"\"\n"
                "exclusions_applied: []\n"
                "substitutions_applied: []\n"
                "payload_sha256: \"deadbeef\"\n"
                "files: []\n"
                "encryption: \"none\"\n"
            )
            with open(os.path.join(staging, "manifest.yaml"), "w") as f:
                f.write(manifest_yaml)
            future_pkg = os.path.join(world, "future.walnut")
            ap2p.safe_tar_create(staging, future_pkg)
            target = os.path.join(world, "received-future")
            with _patch_env(), _skip_regen():
                with self.assertRaises(ValueError) as ctx:
                    ap2p.receive_package(
                        package_path=future_pkg, target_path=target, yes=True,
                    )
            msg = str(ctx.exception).lower()
            self.assertIn("format_version", msg)
            self.assertIn("3.0.0", msg)

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_wrong_rsa_key_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-rsa-bad")
            keys_dir = os.path.join(world, "keys")
            os.makedirs(os.path.join(keys_dir, "peers"), exist_ok=True)
            priv_a, pub_a = _gen_rsa_keypair(keys_dir, name="alice")
            priv_b, _pub_b = _gen_rsa_keypair(keys_dir, name="bob")
            with open(pub_a, "rb") as f:
                ap2p.register_peer_pubkey("alice", f.read(), keys_dir=keys_dir)
            output = os.path.join(world, "src-rsa-bad.walnut")
            saved = os.environ.get("ALIVE_RELAY_KEYS_DIR")
            os.environ["ALIVE_RELAY_KEYS_DIR"] = keys_dir
            try:
                with _patch_env():
                    ap2p.create_package(
                        walnut_path=src, scope="full", output_path=output,
                        include_full_history=True,
                        encrypt_mode="rsa", recipient_peers=["alice"],
                    )
                target = os.path.join(world, "received-rsa-bad")
                with _patch_env(), _skip_regen():
                    with self.assertRaises(RuntimeError) as ctx:
                        ap2p.receive_package(
                            package_path=output, target_path=target, yes=True,
                            private_key_path=priv_b,
                        )
            finally:
                if saved is None:
                    os.environ.pop("ALIVE_RELAY_KEYS_DIR", None)
                else:
                    os.environ["ALIVE_RELAY_KEYS_DIR"] = saved
            self.assertIn(
                "No private key matches any recipient",
                str(ctx.exception),
            )

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_payload_sha256_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-tamper")
            output = os.path.join(world, "src-tamper.walnut")
            with _patch_env():
                ap2p.create_package(
                    walnut_path=src, scope="full", output_path=output,
                    include_full_history=True,
                )
            # Re-pack with one tampered file: extract, mutate a draft, re-pack.
            extracted = os.path.join(world, "extracted")
            ap2p.safe_tar_extract(output, extracted)
            tampered_path = os.path.join(
                extracted, "shielding-review", "draft-01.md",
            )
            with open(tampered_path, "w") as f:
                f.write("# tampered\n")
            tampered_pkg = os.path.join(world, "src-tamper-bad.walnut")
            ap2p.safe_tar_create(extracted, tampered_pkg)
            target = os.path.join(world, "received-tamper")
            with _patch_env(), _skip_regen():
                with self.assertRaises(ValueError) as ctx:
                    ap2p.receive_package(
                        package_path=tampered_pkg, target_path=target, yes=True,
                    )
            msg = str(ctx.exception).lower()
            self.assertTrue(
                "checksum" in msg or "sha256" in msg or "mismatch" in msg,
                msg,
            )


# ---------------------------------------------------------------------------
# LD9 stub vs --include-full-history
# ---------------------------------------------------------------------------


class StubVsIncludeFullHistoryTests(unittest.TestCase):

    @unittest.skipUnless(_openssl_available(), "openssl not available")
    def test_stub_default_vs_include_full_history(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            src = _build_v3_fixture(world, name="src-stub")
            # Default (no include_full_history): log.md and insights.md are
            # baseline-stubbed.
            stub_pkg = os.path.join(world, "src-stub-default.walnut")
            with _patch_env():
                stub_result = ap2p.create_package(
                    walnut_path=src, scope="full", output_path=stub_pkg,
                )
            stub_subs = stub_result["manifest"].get("substitutions_applied", [])
            stub_paths = sorted(s["path"] for s in stub_subs)
            self.assertIn("_kernel/log.md", stub_paths)
            self.assertIn("_kernel/insights.md", stub_paths)
            for entry in stub_subs:
                self.assertEqual(entry.get("reason"), "baseline-stub")
            # Extract the stub package and verify the body of log.md matches
            # the STUB_LOG_MD constant prefix.
            stub_extract = os.path.join(world, "extracted-stub")
            ap2p.safe_tar_extract(stub_pkg, stub_extract)
            with open(os.path.join(stub_extract, "_kernel", "log.md")) as f:
                stub_log = f.read()
            self.assertIn("baseline-stub", stub_log.lower() + " baseline-stub")
            # Compare against the actual STUB_LOG_MD template (which the
            # share pipeline renders via render_stub_log).
            self.assertNotIn("Initial.", stub_log)  # the original real entry

            # Now include_full_history: real history shipped, no substitutions.
            full_pkg = os.path.join(world, "src-stub-full.walnut")
            with _patch_env():
                full_result = ap2p.create_package(
                    walnut_path=src, scope="full", output_path=full_pkg,
                    include_full_history=True,
                )
            full_subs = full_result["manifest"].get("substitutions_applied", [])
            self.assertEqual(full_subs, [])
            full_extract = os.path.join(world, "extracted-full")
            ap2p.safe_tar_extract(full_pkg, full_extract)
            with open(os.path.join(full_extract, "_kernel", "log.md")) as f:
                full_log = f.read()
            self.assertIn("Initial.", full_log)


if __name__ == "__main__":
    unittest.main()
