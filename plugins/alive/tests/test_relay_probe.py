#!/usr/bin/env python3
"""Unit tests for ``plugins/alive/scripts/relay-probe.py`` (LD17, fn-7-7cw).

The probe is the read-only scan that the SessionStart hook runs every 10
minutes. These tests pin the LD17 contract:

* state.json gets written with the documented schema.
* relay.json is NEVER mutated -- the bytes are byte-identical before and
  after a probe runs.
* peer-level failures are recorded as DATA in state.json (not raised).
* missing ``gh`` CLI is the ONE failure that escalates to exit 1.
* ``--peer NAME`` only probes that peer and merges into existing state.

Stdlib only. Mocks ``gh_client.repo_exists`` /
``gh_client.list_inbox_files`` so no real network calls fire. Run from
``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_relay_probe -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


# ---------------------------------------------------------------------------
# Module loading -- relay-probe.py has a hyphen in the filename so a plain
# ``import relay_probe`` does not work. Load via importlib.util.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# gh_client must be importable BEFORE relay-probe.py executes (relay-probe
# imports it at module load). Pre-import to register in sys.modules.
import gh_client  # noqa: E402

_PROBE_PATH = os.path.join(_SCRIPTS, "relay-probe.py")
_spec = importlib.util.spec_from_file_location("relay_probe", _PROBE_PATH)
relay_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay_probe)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_relay_json(path, peers):
    # type: (str, dict) -> bytes
    """Write a relay.json with the given peers map. Return the bytes for
    later byte-identity comparison."""
    cfg = {
        "version": 1,
        "relay": {
            "url": "https://github.com/me/me-relay",
            "username": "me",
            "created_at": "2026-04-07T10:00:00Z",
        },
        "peers": peers,
    }
    body = json.dumps(cfg, indent=2, sort_keys=True) + "\n"
    raw = body.encode("utf-8")
    with open(path, "wb") as f:
        f.write(raw)
    return raw


def _peer_entry(url, accepted=True):
    return {
        "url": url,
        "added_at": "2026-04-07T10:05:00Z",
        "accepted": accepted,
        "exclude_patterns": [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class ProbeWritesStateJsonTests(unittest.TestCase):
    """state.json is created with the LD17 schema after a successful probe."""

    def test_probe_writes_state_json(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            _write_relay_json(relay_json, {
                "benflint": _peer_entry("https://github.com/benflint/benflint-relay"),
            })

            with mock.patch.object(relay_probe.gh_client, "repo_exists", return_value=True), \
                 mock.patch.object(relay_probe.gh_client, "list_inbox_files",
                                   return_value=[{"name": "a.walnut", "sha": "x", "size": 10, "path": "inbox/me/a.walnut"}]):
                rc = relay_probe.main([
                    "probe", "--all-peers",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])

            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(state_json))

            with open(state_json, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.assertEqual(state["version"], 1)
            self.assertIn("last_probe", state)
            self.assertIsNotNone(state["last_probe"])
            self.assertIn("benflint", state["peers"])
            entry = state["peers"]["benflint"]
            self.assertTrue(entry["reachable"])
            self.assertEqual(entry["pending_packages"], 1)
            self.assertIsNone(entry["error"])
            self.assertIn("last_probe", entry)

    def test_probe_state_json_schema_keys(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            _write_relay_json(relay_json, {
                "alpha": _peer_entry("https://github.com/alpha/alpha-relay"),
            })
            with mock.patch.object(relay_probe.gh_client, "repo_exists", return_value=True), \
                 mock.patch.object(relay_probe.gh_client, "list_inbox_files", return_value=[]):
                relay_probe.main([
                    "probe", "--all-peers",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])

            with open(state_json, "r", encoding="utf-8") as f:
                state = json.load(f)
            entry = state["peers"]["alpha"]
            self.assertEqual(
                sorted(entry.keys()),
                ["error", "last_probe", "pending_packages", "reachable"],
            )


class ProbeNeverWritesRelayJsonTests(unittest.TestCase):
    """LD17 -- ``relay.json`` is byte-identical before and after probe runs."""

    def test_probe_never_writes_relay_json_all_peers(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            before = _write_relay_json(relay_json, {
                "p1": _peer_entry("https://github.com/p1/p1-relay"),
                "p2": _peer_entry("https://github.com/p2/p2-relay"),
            })
            mtime_before = os.path.getmtime(relay_json)

            with mock.patch.object(relay_probe.gh_client, "repo_exists", return_value=True), \
                 mock.patch.object(relay_probe.gh_client, "list_inbox_files", return_value=[]):
                rc = relay_probe.main([
                    "probe", "--all-peers",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])
            self.assertEqual(rc, 0)

            with open(relay_json, "rb") as f:
                after = f.read()
            self.assertEqual(before, after, "relay.json bytes changed during probe")
            self.assertEqual(os.path.getmtime(relay_json), mtime_before)

    def test_probe_never_writes_relay_json_single_peer(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            before = _write_relay_json(relay_json, {
                "alpha": _peer_entry("https://github.com/alpha/alpha-relay"),
                "beta": _peer_entry("https://github.com/beta/beta-relay"),
            })

            with mock.patch.object(relay_probe.gh_client, "repo_exists", return_value=True), \
                 mock.patch.object(relay_probe.gh_client, "list_inbox_files", return_value=[]):
                rc = relay_probe.main([
                    "probe", "--peer", "alpha",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])
            self.assertEqual(rc, 0)

            with open(relay_json, "rb") as f:
                after = f.read()
            self.assertEqual(before, after)


class ProbeUnreachablePeerTests(unittest.TestCase):
    """LD17 -- peer-level failures are DATA in state.json, not exceptions."""

    def test_probe_handles_unreachable_peer(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            _write_relay_json(relay_json, {
                "ghost": _peer_entry("https://github.com/ghost/ghost-relay"),
            })

            with mock.patch.object(relay_probe.gh_client, "repo_exists", return_value=False), \
                 mock.patch.object(relay_probe.gh_client, "list_inbox_files",
                                   side_effect=AssertionError("must not be called when repo_exists=False")):
                rc = relay_probe.main([
                    "probe", "--all-peers",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])
            self.assertEqual(rc, 0)

            with open(state_json, "r", encoding="utf-8") as f:
                state = json.load(f)
            entry = state["peers"]["ghost"]
            self.assertFalse(entry["reachable"])
            self.assertEqual(entry["pending_packages"], 0)
            self.assertIsNotNone(entry["error"])
            self.assertIn("not found", entry["error"])

    def test_probe_handles_inbox_list_error(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            _write_relay_json(relay_json, {
                "halfdown": _peer_entry("https://github.com/halfdown/halfdown-relay"),
            })

            with mock.patch.object(relay_probe.gh_client, "repo_exists", return_value=True), \
                 mock.patch.object(relay_probe.gh_client, "list_inbox_files",
                                   side_effect=gh_client.GhClientError("404 inbox missing")):
                rc = relay_probe.main([
                    "probe", "--all-peers",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])
            self.assertEqual(rc, 0)

            with open(state_json, "r", encoding="utf-8") as f:
                state = json.load(f)
            entry = state["peers"]["halfdown"]
            # Repo exists, inbox list failed -- still reachable, 0 packages,
            # error explains why.
            self.assertTrue(entry["reachable"])
            self.assertEqual(entry["pending_packages"], 0)
            self.assertIn("404", entry["error"])

    def test_probe_handles_invalid_url(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            _write_relay_json(relay_json, {
                "weirdo": _peer_entry("not-a-real-url"),
            })

            with mock.patch.object(relay_probe.gh_client, "repo_exists",
                                   side_effect=AssertionError("must not call repo_exists for invalid url")), \
                 mock.patch.object(relay_probe.gh_client, "list_inbox_files",
                                   side_effect=AssertionError("must not call list_inbox_files for invalid url")):
                rc = relay_probe.main([
                    "probe", "--all-peers",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])
            self.assertEqual(rc, 0)

            with open(state_json, "r", encoding="utf-8") as f:
                state = json.load(f)
            entry = state["peers"]["weirdo"]
            self.assertFalse(entry["reachable"])
            self.assertIn("invalid", entry["error"])


class ProbeMissingGhCliTests(unittest.TestCase):
    """LD16 -- gh CLI missing escalates to exit 1, the only hard failure."""

    def test_probe_handles_missing_gh_cli(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            _write_relay_json(relay_json, {
                "any": _peer_entry("https://github.com/any/any-relay"),
            })

            with mock.patch.object(relay_probe.gh_client, "repo_exists",
                                   side_effect=FileNotFoundError("gh: command not found")):
                rc = relay_probe.main([
                    "probe", "--all-peers",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])
            self.assertEqual(rc, 1)


class ProbeSinglePeerTests(unittest.TestCase):
    """``--peer NAME`` only probes that peer and merges into existing state."""

    def test_probe_single_peer(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            _write_relay_json(relay_json, {
                "alpha": _peer_entry("https://github.com/alpha/alpha-relay"),
                "beta": _peer_entry("https://github.com/beta/beta-relay"),
            })

            # Pre-populate state.json with a stale beta entry
            stale = {
                "version": 1,
                "last_probe": "2026-04-01T00:00:00Z",
                "peers": {
                    "beta": {
                        "reachable": True,
                        "last_probe": "2026-04-01T00:00:00Z",
                        "pending_packages": 99,
                        "error": None,
                    },
                },
            }
            with open(state_json, "w", encoding="utf-8") as f:
                json.dump(stale, f)

            calls = []

            def fake_list(owner, repo, peer, timeout=10):
                calls.append((owner, repo, peer))
                return [{"name": "x.walnut", "sha": "1", "size": 5, "path": "p"}]

            with mock.patch.object(relay_probe.gh_client, "repo_exists", return_value=True), \
                 mock.patch.object(relay_probe.gh_client, "list_inbox_files", side_effect=fake_list):
                rc = relay_probe.main([
                    "probe", "--peer", "alpha",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])
            self.assertEqual(rc, 0)

            with open(state_json, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.assertIn("alpha", state["peers"])
            self.assertEqual(state["peers"]["alpha"]["pending_packages"], 1)
            # Stale beta should still be present (merge, not overwrite).
            self.assertIn("beta", state["peers"])
            self.assertEqual(state["peers"]["beta"]["pending_packages"], 99)
            # And gh_client must have been called only for alpha.
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2], "alpha")

    def test_probe_single_peer_unknown_name_fails(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            _write_relay_json(relay_json, {
                "alpha": _peer_entry("https://github.com/alpha/alpha-relay"),
            })
            rc = relay_probe.main([
                "probe", "--peer", "nobody",
                "--relay-config", relay_json,
                "--output", state_json,
            ])
            self.assertEqual(rc, 1)


class ProbeLastProbeTimestampTests(unittest.TestCase):
    """LD17 -- top-level ``last_probe`` is fresh after every probe run.

    The hook reads this field for the 10-minute cooldown decision; if it
    drifts the cooldown breaks.
    """

    def test_probe_updates_last_probe_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            _write_relay_json(relay_json, {
                "alpha": _peer_entry("https://github.com/alpha/alpha-relay"),
            })

            # Stale timestamp baseline
            stale_ts = "2020-01-01T00:00:00Z"
            with open(state_json, "w", encoding="utf-8") as f:
                json.dump({
                    "version": 1,
                    "last_probe": stale_ts,
                    "peers": {},
                }, f)

            with mock.patch.object(relay_probe.gh_client, "repo_exists", return_value=True), \
                 mock.patch.object(relay_probe.gh_client, "list_inbox_files", return_value=[]):
                rc = relay_probe.main([
                    "probe", "--all-peers",
                    "--relay-config", relay_json,
                    "--output", state_json,
                ])
            self.assertEqual(rc, 0)

            with open(state_json, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.assertNotEqual(state["last_probe"], stale_ts)
            self.assertTrue(state["last_probe"].endswith("Z"))


class ProbeMissingRelayJsonTests(unittest.TestCase):
    """``relay.json`` missing is exit 1 from the probe perspective.

    The HOOK script translates "missing relay.json" into exit 0 ("not
    configured"), but the underlying probe still fails because there is
    nothing to probe.
    """

    def test_probe_missing_relay_json_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            relay_json = os.path.join(td, "relay.json")
            state_json = os.path.join(td, "state.json")
            # Do NOT create relay.json.
            rc = relay_probe.main([
                "probe", "--all-peers",
                "--relay-config", relay_json,
                "--output", state_json,
            ])
            self.assertEqual(rc, 1)


class ProbeCliShapeTests(unittest.TestCase):
    """LD17 -- the canonical CLI is ``probe`` and only ``probe``."""

    def test_no_info_flag(self):
        # ``--info`` was a draft name, superseded. Verify it does not exist
        # by attempting to use it -- argparse must reject.
        with self.assertRaises(SystemExit):
            relay_probe.main(["--info"])

    def test_help_includes_probe(self):
        # ``--help`` exits 0 via SystemExit; capture stdout to check the
        # canonical subcommand name appears.
        from io import StringIO
        buf = StringIO()
        try:
            with mock.patch("sys.stdout", buf):
                with self.assertRaises(SystemExit) as ctx:
                    relay_probe.main(["--help"])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn("probe", buf.getvalue())
        finally:
            pass


if __name__ == "__main__":
    unittest.main()
