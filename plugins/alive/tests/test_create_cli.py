#!/usr/bin/env python3
"""Unit tests for the LD11 share CLI in ``alive-p2p.py``.

Covers:
- ``create_package`` orchestrator: full / bundle / snapshot smoke tests
- LD11 flag validation rules
- Exclusion application + manifest audit trail
- LD17 preferences loading + share preset application
- ``list-bundles`` JSON output

Each test builds a fresh fixture walnut in a ``tempfile.TemporaryDirectory``,
calls into the CLI via ``argparse`` (without spawning a subprocess) so the
test can assert on the resulting package contents directly. Stdlib only.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_create_cli -v
"""

import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock


# ---------------------------------------------------------------------------
# Module loading -- alive-p2p.py has a hyphen in the filename, so use
# importlib to bind the module under a Python-friendly name.
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


FIXED_TS = "2026-04-07T12:00:00Z"
FIXED_SESSION = "test-session-abc"
FIXED_SENDER = "test-sender"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_v3_walnut(walnut, name="test-walnut"):
    """Build a minimal v3 walnut: kernel files, two flat bundles, live ctx."""
    _write(
        os.path.join(walnut, "_kernel", "key.md"),
        "---\ntype: venture\nname: {0}\n---\n".format(name),
    )
    _write(
        os.path.join(walnut, "_kernel", "log.md"),
        "---\nwalnut: {0}\nentry-count: 2\n---\n\nreal log\n".format(name),
    )
    _write(
        os.path.join(walnut, "_kernel", "insights.md"),
        "---\nwalnut: {0}\n---\n\nreal insights\n".format(name),
    )
    _write(
        os.path.join(walnut, "_kernel", "tasks.json"),
        '{"tasks": []}\n',
    )
    _write(
        os.path.join(walnut, "_kernel", "completed.json"),
        '{"completed": []}\n',
    )
    # Two flat bundles.
    _write(
        os.path.join(walnut, "shielding-review", "context.manifest.yaml"),
        "goal: Review shielding\nstatus: active\n",
    )
    _write(
        os.path.join(walnut, "shielding-review", "draft-01.md"),
        "# Shielding draft\n",
    )
    _write(
        os.path.join(walnut, "shielding-review", "observations.md"),
        "# Shielding observations\n",
    )
    _write(
        os.path.join(walnut, "launch-checklist", "context.manifest.yaml"),
        "goal: Launch checklist\nstatus: draft\n",
    )
    _write(
        os.path.join(walnut, "launch-checklist", "items.md"),
        "- [ ] Item 1\n",
    )
    # A live context dir.
    _write(
        os.path.join(walnut, "engineering", "spec.md"),
        "# spec\n",
    )
    # A nested bundle that should NOT be shareable (top_level: false).
    _write(
        os.path.join(walnut, "archive", "old", "bundle-x", "context.manifest.yaml"),
        "goal: Old archived bundle\n",
    )
    # A .tmp file that should be excluded by --exclude '**/*.tmp'.
    _write(
        os.path.join(walnut, "engineering", "scratch.tmp"),
        "scratch\n",
    )


def _make_world_root(parent_dir, with_prefs=False, prefs_yaml=None):
    """Create a ``.alive`` marker dir + optional preferences file.

    Returns the world-root directory (== ``parent_dir``).
    """
    os.makedirs(os.path.join(parent_dir, ".alive"), exist_ok=True)
    if with_prefs:
        if prefs_yaml is None:
            prefs_yaml = (
                "discovery_hints: true\n"
                "p2p:\n"
                "  share_presets:\n"
                "    external:\n"
                "      exclude_patterns:\n"
                "        - \"**/observations.md\"\n"
                "        - \"**/pricing*\"\n"
                "  auto_receive: false\n"
            )
        _write(os.path.join(parent_dir, ".alive", "preferences.yaml"), prefs_yaml)
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


def _read_manifest_from_package(package_path):
    """Extract ``manifest.yaml`` from a .walnut package and return its bytes."""
    with tarfile.open(package_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "manifest.yaml" or member.name.endswith("/manifest.yaml"):
                f = tar.extractfile(member)
                if f is None:
                    return b""
                return f.read()
    return b""


def _list_package_paths(package_path):
    """Return the sorted list of file paths inside a .walnut package."""
    out = []
    with tarfile.open(package_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                out.append(member.name.lstrip("./"))
    return sorted(out)


# ---------------------------------------------------------------------------
# create_package smoke tests
# ---------------------------------------------------------------------------


class CreatePackageFullScopeTests(unittest.TestCase):

    def test_create_full_scope_smoke(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            output = os.path.join(world, "out.walnut")

            with _patch_env():
                result = ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=output,
                )

            self.assertTrue(os.path.isfile(result["package_path"]))
            self.assertEqual(result["package_path"], output)
            self.assertGreater(result["size_bytes"], 0)
            self.assertEqual(len(result["import_id"]), 64)  # sha256 hex

            manifest_bytes = _read_manifest_from_package(output)
            self.assertIn(b'format_version: "2.1.0"', manifest_bytes)
            self.assertIn(b'source_layout: "v3"', manifest_bytes)
            self.assertIn(b'scope: "full"', manifest_bytes)

            # Both flat bundles should be present.
            paths = _list_package_paths(output)
            self.assertIn("manifest.yaml", paths)
            self.assertIn("_kernel/key.md", paths)
            self.assertIn("_kernel/log.md", paths)
            self.assertIn("_kernel/insights.md", paths)
            self.assertTrue(
                any(p.startswith("shielding-review/") for p in paths),
                "shielding-review/ bundle missing from package",
            )
            self.assertTrue(
                any(p.startswith("launch-checklist/") for p in paths),
                "launch-checklist/ bundle missing from package",
            )

    def test_full_scope_stubs_log_and_insights_by_default(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            output = os.path.join(world, "out.walnut")

            with _patch_env():
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=output,
                )

            with tarfile.open(output, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.endswith("_kernel/log.md"):
                        body = tar.extractfile(member).read().decode("utf-8")
                        self.assertIn("Default share exclusion", body)
                        return
            self.fail("_kernel/log.md not found in package")

    def test_full_scope_include_full_history(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            output = os.path.join(world, "out.walnut")

            with _patch_env():
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=output,
                    include_full_history=True,
                )

            with tarfile.open(output, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.endswith("_kernel/log.md"):
                        body = tar.extractfile(member).read().decode("utf-8")
                        self.assertIn("real log", body)
                        return
            self.fail("_kernel/log.md not found in package")


class CreatePackageBundleScopeTests(unittest.TestCase):

    def test_create_bundle_scope(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            output = os.path.join(world, "out.walnut")

            with _patch_env():
                result = ap2p.create_package(
                    walnut_path=walnut,
                    scope="bundle",
                    output_path=output,
                    bundle_names=["shielding-review"],
                )

            self.assertTrue(os.path.isfile(result["package_path"]))
            paths = _list_package_paths(output)
            self.assertIn("manifest.yaml", paths)
            self.assertIn("_kernel/key.md", paths)
            self.assertTrue(
                any(p.startswith("shielding-review/") for p in paths)
            )
            # launch-checklist should NOT be in the package
            self.assertFalse(
                any(p.startswith("launch-checklist/") for p in paths)
            )

    def test_create_rejects_missing_bundle_for_bundle_scope(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            with self.assertRaises(ValueError) as cm:
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="bundle",
                    output_path=os.path.join(world, "out.walnut"),
                )
            self.assertIn("requires at least one --bundle", str(cm.exception))


class CreatePackageSnapshotScopeTests(unittest.TestCase):

    def test_create_snapshot_scope(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            output = os.path.join(world, "out.walnut")

            with _patch_env():
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="snapshot",
                    output_path=output,
                )

            paths = _list_package_paths(output)
            self.assertIn("manifest.yaml", paths)
            self.assertIn("_kernel/key.md", paths)
            self.assertIn("_kernel/insights.md", paths)
            # No log.md, no tasks.json, no bundles.
            self.assertNotIn("_kernel/log.md", paths)
            self.assertNotIn("_kernel/tasks.json", paths)
            self.assertFalse(
                any(p.startswith("shielding-review/") for p in paths)
            )


# ---------------------------------------------------------------------------
# LD11 flag validation
# ---------------------------------------------------------------------------


class FlagValidationTests(unittest.TestCase):

    def test_create_rejects_bundle_flag_with_full_scope(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            with self.assertRaises(ValueError) as cm:
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=os.path.join(world, "out.walnut"),
                    bundle_names=["shielding-review"],
                )
            self.assertIn(
                "--bundle is only valid with --scope bundle",
                str(cm.exception),
            )

    def test_create_rejects_bundle_flag_with_snapshot_scope(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            with self.assertRaises(ValueError):
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="snapshot",
                    output_path=os.path.join(world, "out.walnut"),
                    bundle_names=["shielding-review"],
                )

    def test_create_rejects_passphrase_without_env(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            with self.assertRaises(ValueError) as cm:
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=os.path.join(world, "out.walnut"),
                    encrypt_mode="passphrase",
                )
            self.assertIn("--passphrase-env", str(cm.exception))

    def test_create_rejects_rsa_without_recipient(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            with self.assertRaises(ValueError) as cm:
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=os.path.join(world, "out.walnut"),
                    encrypt_mode="rsa",
                )
            self.assertIn("--recipient", str(cm.exception))

    def test_create_rejects_sign_without_signing_key(self):
        with tempfile.TemporaryDirectory() as world:
            # World root with empty preferences -- no signing_key_path.
            _make_world_root(world, with_prefs=True)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            with self.assertRaises(ValueError) as cm:
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=os.path.join(world, "out.walnut"),
                    sign=True,
                )
            self.assertIn("p2p.signing_key_path", str(cm.exception))

    def test_create_rejects_unknown_encrypt_mode(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            with self.assertRaises(ValueError):
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=os.path.join(world, "out.walnut"),
                    encrypt_mode="bogus",
                )

    def test_create_rejects_unknown_scope(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            with self.assertRaises(ValueError):
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="bogus",
                    output_path=os.path.join(world, "out.walnut"),
                )


# ---------------------------------------------------------------------------
# Exclusions + audit trail
# ---------------------------------------------------------------------------


class ExclusionsTests(unittest.TestCase):

    def test_create_applies_exclusions(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            output = os.path.join(world, "out.walnut")

            with _patch_env():
                result = ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=output,
                    exclusions=["**/*.tmp"],
                )

            # Manifest records the exclusion.
            self.assertIn("**/*.tmp", result["exclusions_applied"])
            # The .tmp file should not be in the package.
            paths = _list_package_paths(output)
            self.assertFalse(
                any(p.endswith(".tmp") for p in paths),
                "scratch.tmp not removed by exclusion: {0}".format(paths),
            )
            # And the manifest YAML body should also include the audit field.
            manifest_bytes = _read_manifest_from_package(output)
            self.assertIn(b"exclusions_applied", manifest_bytes)
            self.assertIn(b"**/*.tmp", manifest_bytes)

    def test_exclusions_cannot_remove_protected_paths(self):
        # Even if a user excludes ``_kernel/key.md``, the LD26 protected-path
        # rule keeps it in the package.
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            output = os.path.join(world, "out.walnut")

            with _patch_env():
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=output,
                    exclusions=["_kernel/key.md", "_kernel/log.md"],
                )

            paths = _list_package_paths(output)
            self.assertIn("_kernel/key.md", paths)
            self.assertIn("_kernel/log.md", paths)


# ---------------------------------------------------------------------------
# Preset loading from preferences.yaml
# ---------------------------------------------------------------------------


class PresetLoadingTests(unittest.TestCase):

    def test_create_preset_loading(self):
        prefs_yaml = (
            "discovery_hints: true\n"
            "p2p:\n"
            "  share_presets:\n"
            "    external:\n"
            "      exclude_patterns:\n"
            "        - \"**/observations.md\"\n"
            "        - \"**/scratch.tmp\"\n"
            "  auto_receive: false\n"
        )
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world, with_prefs=True, prefs_yaml=prefs_yaml)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            output = os.path.join(world, "out.walnut")

            with _patch_env():
                result = ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=output,
                    preset="external",
                )

            self.assertTrue(result["preferences_found"])
            self.assertIn("**/observations.md", result["exclusions_applied"])
            paths = _list_package_paths(output)
            self.assertFalse(
                any(p.endswith("observations.md") for p in paths),
                "observations.md should be excluded by external preset",
            )

    def test_create_unknown_preset_errors(self):
        prefs_yaml = (
            "p2p:\n"
            "  share_presets:\n"
            "    external:\n"
            "      exclude_patterns:\n"
            "        - \"**/observations.md\"\n"
        )
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world, with_prefs=True, prefs_yaml=prefs_yaml)
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            with self.assertRaises(KeyError):
                ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=os.path.join(world, "out.walnut"),
                    preset="not-real",
                )

    def test_no_preferences_warning(self):
        with tempfile.TemporaryDirectory() as world:
            # No .alive marker dir at all -- find_world_root returns None.
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)
            output = os.path.join(world, "out.walnut")

            with _patch_env():
                result = ap2p.create_package(
                    walnut_path=walnut,
                    scope="full",
                    output_path=output,
                )

            self.assertFalse(result["preferences_found"])
            self.assertTrue(
                any("No p2p preferences" in w for w in result["warnings"]),
                "expected baseline-stubs warning, got {0}".format(
                    result["warnings"]
                ),
            )


# ---------------------------------------------------------------------------
# list-bundles CLI
# ---------------------------------------------------------------------------


class ListBundlesTests(unittest.TestCase):

    def test_list_bundles_json_output(self):
        with tempfile.TemporaryDirectory() as world:
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)

            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                with self.assertRaises(SystemExit) as cm:
                    ap2p._cli(["list-bundles", "--walnut", walnut, "--json"])
                self.assertEqual(cm.exception.code, 0)

            data = json.loads(buf.getvalue())
            self.assertIsInstance(data, list)
            names = sorted(b["name"] for b in data)
            self.assertIn("shielding-review", names)
            self.assertIn("launch-checklist", names)

    def test_list_bundles_excludes_nested_from_top_level(self):
        with tempfile.TemporaryDirectory() as world:
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)

            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                with self.assertRaises(SystemExit):
                    ap2p._cli(["list-bundles", "--walnut", walnut, "--json"])

            data = json.loads(buf.getvalue())
            # The nested bundle is in the result but flagged top_level=False.
            nested = [b for b in data if b["name"] == "bundle-x"]
            self.assertEqual(len(nested), 1)
            self.assertFalse(nested[0]["top_level"])
            # The top-level bundles are flagged True.
            top = [b for b in data if b["top_level"]]
            top_names = sorted(b["name"] for b in top)
            self.assertIn("shielding-review", top_names)
            self.assertIn("launch-checklist", top_names)
            self.assertNotIn("bundle-x", top_names)

    def test_list_bundles_returns_two_for_real_walnut_format(self):
        # The schema fields are stable: name, relpath, abs_path, top_level.
        with tempfile.TemporaryDirectory() as world:
            walnut = os.path.join(world, "test-walnut")
            _make_v3_walnut(walnut)

            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                with self.assertRaises(SystemExit):
                    ap2p._cli(["list-bundles", "--walnut", walnut, "--json"])

            data = json.loads(buf.getvalue())
            for entry in data:
                self.assertIn("name", entry)
                self.assertIn("relpath", entry)
                self.assertIn("abs_path", entry)
                self.assertIn("top_level", entry)


# ---------------------------------------------------------------------------
# find_world_root + preferences loader
# ---------------------------------------------------------------------------


class FindWorldRootTests(unittest.TestCase):

    def test_walks_up_to_alive_marker(self):
        with tempfile.TemporaryDirectory() as world:
            os.makedirs(os.path.join(world, ".alive"))
            walnut = os.path.join(world, "ventures", "test-walnut")
            os.makedirs(walnut)
            self.assertEqual(
                os.path.abspath(ap2p.find_world_root(walnut)),
                os.path.abspath(world),
            )

    def test_returns_none_when_no_marker(self):
        with tempfile.TemporaryDirectory() as parent:
            walnut = os.path.join(parent, "test-walnut")
            os.makedirs(walnut)
            # No ``.alive`` marker anywhere up the chain (tempdir parents
            # almost certainly don't have one). Should return None.
            result = ap2p.find_world_root(walnut)
            # On a dev machine the user's home tree may itself contain a
            # ``.alive`` directory, so accept either None OR an ancestor of
            # the tempdir (never the walnut path itself).
            if result is not None:
                self.assertNotEqual(
                    os.path.abspath(result), os.path.abspath(walnut)
                )


class LoadP2pPreferencesTests(unittest.TestCase):

    def test_loads_share_presets(self):
        prefs_yaml = (
            "p2p:\n"
            "  share_presets:\n"
            "    internal:\n"
            "      exclude_patterns:\n"
            "        - \"**/observations.md\"\n"
            "    external:\n"
            "      exclude_patterns:\n"
            "        - \"**/pricing*\"\n"
            "        - \"**/strategy*\"\n"
            "  auto_receive: false\n"
        )
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world, with_prefs=True, prefs_yaml=prefs_yaml)
            walnut = os.path.join(world, "test-walnut")
            os.makedirs(walnut)
            prefs = ap2p._load_p2p_preferences(walnut)
            self.assertTrue(prefs["_preferences_found"])
            self.assertIn("internal", prefs["share_presets"])
            self.assertIn("external", prefs["share_presets"])
            self.assertEqual(
                prefs["share_presets"]["internal"]["exclude_patterns"],
                ["**/observations.md"],
            )
            self.assertEqual(
                sorted(prefs["share_presets"]["external"]["exclude_patterns"]),
                ["**/pricing*", "**/strategy*"],
            )

    def test_safe_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as world:
            walnut = os.path.join(world, "test-walnut")
            os.makedirs(walnut)
            prefs = ap2p._load_p2p_preferences(walnut)
            # _preferences_found should be False (no .alive marker found
            # within the tempdir; ancestor world roots are out of scope).
            self.assertFalse(
                prefs["_preferences_found"]
                and prefs.get("share_presets")
            )
            self.assertEqual(prefs["share_presets"], {})

    def test_discovery_hints_default_true(self):
        with tempfile.TemporaryDirectory() as world:
            _make_world_root(world, with_prefs=True, prefs_yaml="")
            walnut = os.path.join(world, "test-walnut")
            os.makedirs(walnut)
            prefs = ap2p._load_p2p_preferences(walnut)
            self.assertTrue(prefs["discovery_hints"])


if __name__ == "__main__":
    unittest.main()
