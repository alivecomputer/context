#!/usr/bin/env python3
"""Unit tests for ``plugins/alive/scripts/gh_client.py`` (LD17, fn-7-7cw).

Pins the wrapper contract over ``gh api`` and ``gh auth status``. The
real ``gh`` binary is never invoked -- every test mocks
``subprocess.run`` so the suite is hermetic and stdlib-only.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_gh_client -v
"""

import base64
import json
import os
import subprocess
import sys
import unittest
from unittest import mock


_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import gh_client  # noqa: E402


def _completed(returncode, stdout="", stderr=""):
    # type: (int, str, str) -> subprocess.CompletedProcess
    """Build a fake ``subprocess.CompletedProcess`` for mocked gh calls."""
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# repo_exists
# ---------------------------------------------------------------------------


class RepoExistsTests(unittest.TestCase):
    def test_repo_exists_success(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stdout='{"name":"foo"}')):
            self.assertTrue(gh_client.repo_exists("alpha", "alpha-relay"))

    def test_repo_exists_404(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(1, stderr="HTTP 404")):
            self.assertFalse(gh_client.repo_exists("ghost", "ghost-relay"))

    def test_repo_exists_passes_args(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0)) as m:
            gh_client.repo_exists("a", "b", timeout=15)
            args, kwargs = m.call_args
            cmd = args[0]
            self.assertEqual(cmd[0], "gh")
            self.assertIn("api", cmd)
            self.assertIn("repos/a/b", cmd)
            self.assertEqual(kwargs.get("timeout"), 15)
            self.assertTrue(kwargs.get("capture_output"))
            self.assertTrue(kwargs.get("text"))
            self.assertFalse(kwargs.get("check", True))


# ---------------------------------------------------------------------------
# list_inbox_files
# ---------------------------------------------------------------------------


class ListInboxFilesTests(unittest.TestCase):
    """``list_inbox_files`` parses the gh api JSON array shape."""

    def _payload(self, names):
        return json.dumps([
            {
                "name": n,
                "sha": "sha-{0}".format(n),
                "size": len(n),
                "path": "inbox/me/{0}".format(n),
                "type": "file",
                "download_url": "https://example/{0}".format(n),
            }
            for n in names
        ])

    def test_list_inbox_files_json_parsing(self):
        names = ["a.walnut", "README.md", "b.walnut", "key.pem"]
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stdout=self._payload(names))):
            files = gh_client.list_inbox_files("alpha", "alpha-relay", "me")
        # Only .walnut files survive the filter.
        self.assertEqual(sorted(f["name"] for f in files), ["a.walnut", "b.walnut"])
        for f in files:
            self.assertIn("sha", f)
            self.assertIn("size", f)
            self.assertIn("path", f)

    def test_list_inbox_files_empty_array(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stdout="[]")):
            files = gh_client.list_inbox_files("a", "b", "me")
        self.assertEqual(files, [])

    def test_list_inbox_files_404_raises(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(1, stderr="HTTP 404")):
            with self.assertRaises(gh_client.GhClientError) as ctx:
                gh_client.list_inbox_files("a", "b", "me")
            self.assertIn("404", str(ctx.exception))

    def test_list_inbox_files_bad_json_raises(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stdout="not-json{")):
            with self.assertRaises(gh_client.GhClientError):
                gh_client.list_inbox_files("a", "b", "me")

    def test_list_inbox_files_dict_payload_treated_empty(self):
        # gh sometimes returns a dict {message: ...} on edge cases that
        # squeak past the returncode check; treat as empty inbox.
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stdout='{"message":"weird"}')):
            files = gh_client.list_inbox_files("a", "b", "me")
        self.assertEqual(files, [])

    def test_list_inbox_files_path_construction(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stdout="[]")) as m:
            gh_client.list_inbox_files("alpha", "alpha-relay", "benflint")
            cmd = m.call_args[0][0]
            self.assertIn("repos/alpha/alpha-relay/contents/inbox/benflint", cmd)


# ---------------------------------------------------------------------------
# fetch_public_key
# ---------------------------------------------------------------------------


class FetchPublicKeyTests(unittest.TestCase):
    PEM = (
        "-----BEGIN PUBLIC KEY-----\n"
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxxxxx\n"
        "-----END PUBLIC KEY-----\n"
    )

    def _content_payload(self, content_str):
        encoded = base64.b64encode(content_str.encode("utf-8")).decode("ascii")
        return json.dumps({
            "name": "alpha.pem",
            "path": "keys/alpha.pem",
            "sha": "deadbeef",
            "size": len(content_str),
            "encoding": "base64",
            "content": encoded,
        })

    def test_fetch_public_key(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stdout=self._content_payload(self.PEM))):
            pem = gh_client.fetch_public_key("alpha", "alpha-relay", "alpha")
        self.assertEqual(pem, self.PEM)
        self.assertIn("BEGIN PUBLIC KEY", pem)

    def test_fetch_public_key_404_raises(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(1, stderr="HTTP 404 Not Found")):
            with self.assertRaises(gh_client.GhClientError) as ctx:
                gh_client.fetch_public_key("alpha", "alpha-relay", "alpha")
            self.assertIn("404", str(ctx.exception))

    def test_fetch_public_key_missing_content_raises(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stdout='{"name":"alpha.pem"}')):
            with self.assertRaises(gh_client.GhClientError):
                gh_client.fetch_public_key("alpha", "alpha-relay", "alpha")

    def test_fetch_public_key_bad_base64_raises(self):
        bad = json.dumps({
            "name": "alpha.pem",
            "encoding": "base64",
            "content": "%%%not-base64%%%",
        })
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stdout=bad)):
            with self.assertRaises(gh_client.GhClientError):
                gh_client.fetch_public_key("alpha", "alpha-relay", "alpha")


# ---------------------------------------------------------------------------
# check_auth
# ---------------------------------------------------------------------------


class CheckAuthTests(unittest.TestCase):
    def test_auth_check_success(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0, stderr="Logged in to github.com as foo")):
            self.assertTrue(gh_client.check_auth())

    def test_auth_check_failure(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(1, stderr="not logged in")):
            self.assertFalse(gh_client.check_auth())

    def test_auth_check_gh_missing(self):
        with mock.patch("gh_client.subprocess.run",
                        side_effect=FileNotFoundError("gh: command not found")):
            self.assertFalse(gh_client.check_auth())


# ---------------------------------------------------------------------------
# Internal _run_gh contract
# ---------------------------------------------------------------------------


class RunGhContractTests(unittest.TestCase):
    """The internal helper must always pass capture_output, text, check=False."""

    def test_run_gh_kwargs(self):
        with mock.patch("gh_client.subprocess.run",
                        return_value=_completed(0)) as m:
            gh_client._run_gh(["api", "user"], timeout=7)
            args, kwargs = m.call_args
            self.assertEqual(args[0], ["gh", "api", "user"])
            self.assertTrue(kwargs.get("capture_output"))
            self.assertTrue(kwargs.get("text"))
            self.assertFalse(kwargs.get("check", True))
            self.assertEqual(kwargs.get("timeout"), 7)


if __name__ == "__main__":
    unittest.main()
