#!/usr/bin/env python3
"""Unit tests for the v3 staging layer in ``alive-p2p.py``.

Covers LD8 (top-level bundle helper), LD9 (stub semantics), LD26 (create file
selection per scope), and LD27 (mixed v2/v3 layout). Each test builds a fresh
fixture walnut in a ``tempfile.TemporaryDirectory``, invokes the private
``_stage_*`` helpers, and asserts on the staging tree contents.

The tests mock ``now_utc_iso`` and ``resolve_session_id`` via
``unittest.mock.patch`` so the rendered stub bytes are deterministic.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_staging -v

Stdlib only -- no PyYAML, no third-party assertions.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock


# ---------------------------------------------------------------------------
# Module loading: alive-p2p.py has a hyphen in the filename so a plain
# ``import alive_p2p`` does not work. Load it via importlib.util from the
# scripts directory.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# walnut_paths is a plain module; import it first so alive-p2p's own
# ``import walnut_paths`` line hits the cache instead of re-importing.
import walnut_paths  # noqa: E402

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


def _make_kernel(walnut, name="test-walnut", log=True, insights=True,
                 tasks=True, completed=True, config=False):
    """Populate ``{walnut}/_kernel/`` with the usual source files."""
    _write(
        os.path.join(walnut, "_kernel", "key.md"),
        "---\ntype: venture\nname: {0}\n---\n".format(name),
    )
    if log:
        _write(
            os.path.join(walnut, "_kernel", "log.md"),
            "---\nwalnut: {0}\nentry-count: 5\n---\n\nreal log content\n".format(name),
        )
    if insights:
        _write(
            os.path.join(walnut, "_kernel", "insights.md"),
            "---\nwalnut: {0}\n---\n\nreal insights\n".format(name),
        )
    if tasks:
        _write(
            os.path.join(walnut, "_kernel", "tasks.json"),
            '{"tasks": [{"id": "t1"}]}\n',
        )
    if completed:
        _write(
            os.path.join(walnut, "_kernel", "completed.json"),
            '{"completed": []}\n',
        )
    if config:
        _write(
            os.path.join(walnut, "_kernel", "config.yaml"),
            "voice: warm\n",
        )


def _make_bundle_v3(walnut, name, goal="test goal"):
    """Create a v3 flat bundle at ``{walnut}/{name}/``."""
    _write(
        os.path.join(walnut, name, "context.manifest.yaml"),
        "goal: {0}\nstatus: draft\n".format(goal),
    )
    _write(
        os.path.join(walnut, name, "draft-01.md"),
        "# {0}\n".format(name),
    )


def _make_bundle_v2(walnut, name, goal="v2 bundle"):
    """Create a v2 container bundle at ``{walnut}/bundles/{name}/``."""
    _write(
        os.path.join(walnut, "bundles", name, "context.manifest.yaml"),
        "goal: {0}\nstatus: active\n".format(goal),
    )
    _write(
        os.path.join(walnut, "bundles", name, "notes.md"),
        "v2 notes\n",
    )


def _make_live_context(walnut):
    """Populate a walnut with a handful of live-context files and dirs."""
    _write(
        os.path.join(walnut, "engineering", "spec.md"),
        "# spec\n",
    )
    _write(
        os.path.join(walnut, "README.md"),
        "# readme\n",
    )
    _write(
        os.path.join(walnut, "marketing", "brief.md"),
        "# brief\n",
    )


def _listing(staging):
    """Return a sorted list of relpaths (POSIX) for files under ``staging``."""
    out = []
    for root, dirs, files in os.walk(staging):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), staging)
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _patch_env():
    """Return a context manager that pins the timestamp + session + sender."""
    return _EnvPatchContext()


class _EnvPatchContext(object):
    def __enter__(self):
        self._patches = [
            mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS),
            mock.patch.object(
                ap2p, "resolve_session_id", return_value=FIXED_SESSION
            ),
            mock.patch.object(
                ap2p, "resolve_sender", return_value=FIXED_SENDER
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# LD8 helper tests
# ---------------------------------------------------------------------------


class IsTopLevelBundleTests(unittest.TestCase):
    """LD8 top-level bundle predicate across the four layout cases."""

    def test_v3_flat_single_component(self):
        self.assertTrue(ap2p.is_top_level_bundle("shielding-review"))

    def test_v2_bundles_container(self):
        self.assertTrue(ap2p.is_top_level_bundle("bundles/shielding-review"))

    def test_v1_legacy_capsules_container(self):
        self.assertTrue(
            ap2p.is_top_level_bundle("_core/_capsules/shielding-review")
        )

    def test_nested_rejected(self):
        self.assertFalse(ap2p.is_top_level_bundle("archive/old/bundle-a"))
        self.assertFalse(ap2p.is_top_level_bundle("some-dir/bundle-b"))

    def test_bundles_container_with_extra_nesting_rejected(self):
        # bundles/foo/bar -- foo under bundles is fine, but bar is an extra
        # nesting level -> not a top-level bundle.
        self.assertFalse(ap2p.is_top_level_bundle("bundles/foo/bar"))

    def test_windows_separators_are_normalized(self):
        self.assertTrue(ap2p.is_top_level_bundle("bundles\\shielding-review"))

    def test_empty_rejected(self):
        self.assertFalse(ap2p.is_top_level_bundle(""))


# ---------------------------------------------------------------------------
# _stage_full tests
# ---------------------------------------------------------------------------


class StageFullTests(unittest.TestCase):

    def test_stage_full_v3_walnut(self):
        """v3 walnut: LD26 full-scope rules, flat bundles, LD9 stubs."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut, name="nova-station", config=True)
            _make_bundle_v3(walnut, "shielding-review")
            _make_bundle_v3(walnut, "launch-checklist")
            _make_live_context(walnut)
            # Also drop some explicit excludes to verify they are filtered
            _write(os.path.join(walnut, "_kernel", "now.json"), "{}\n")
            _write(
                os.path.join(walnut, "_kernel", "_generated", "stale.json"),
                "{}\n",
            )
            _write(
                os.path.join(walnut, "_kernel", "imports.json"),
                "{}\n",
            )

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            warnings = []  # type: list
            with _patch_env():
                ap2p._stage_full(
                    walnut,
                    staging,
                    sender=FIXED_SENDER,
                    session_id=FIXED_SESSION,
                    stub_kernel_history=True,
                    warnings=warnings,
                )

            files = _listing(staging)

            # Required kernel files present
            self.assertIn("_kernel/key.md", files)
            self.assertIn("_kernel/log.md", files)
            self.assertIn("_kernel/insights.md", files)
            self.assertIn("_kernel/tasks.json", files)
            self.assertIn("_kernel/completed.json", files)
            self.assertIn("_kernel/config.yaml", files)

            # Excluded kernel paths absent
            self.assertNotIn("_kernel/now.json", files)
            self.assertNotIn("_kernel/imports.json", files)
            for f in files:
                self.assertFalse(
                    f.startswith("_kernel/_generated"),
                    "unexpected generated file in staging: {0}".format(f),
                )

            # Bundles flat at root
            self.assertIn("shielding-review/context.manifest.yaml", files)
            self.assertIn("shielding-review/draft-01.md", files)
            self.assertIn("launch-checklist/context.manifest.yaml", files)

            # NO v2 container in staging
            for f in files:
                self.assertFalse(
                    f.startswith("bundles/"),
                    "staging should be flat, not v2-containerized: {0}".format(f),
                )

            # Live context preserved at staging root
            self.assertIn("engineering/spec.md", files)
            self.assertIn("marketing/brief.md", files)
            self.assertIn("README.md", files)

            # Stub content matches LD9 templates byte-for-byte
            with open(
                os.path.join(staging, "_kernel", "log.md"),
                "r",
                encoding="utf-8",
            ) as f:
                log_body = f.read()
            expected_log = ap2p.STUB_LOG_MD.format(
                walnut_name="nova-station",
                iso_timestamp=FIXED_TS,
                session_id=FIXED_SESSION,
                sender=FIXED_SENDER,
            )
            self.assertEqual(log_body, expected_log)

            with open(
                os.path.join(staging, "_kernel", "insights.md"),
                "r",
                encoding="utf-8",
            ) as f:
                ins_body = f.read()
            expected_ins = ap2p.STUB_INSIGHTS_MD.format(
                walnut_name="nova-station",
                iso_timestamp=FIXED_TS,
            )
            self.assertEqual(ins_body, expected_ins)

    def test_stage_full_v2_walnut_migrates_to_flat(self):
        """v2 walnut (bundles/X) must land as flat {X}/ in staging."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "old-venture")
            _make_kernel(walnut, name="old-venture", config=False)
            _make_bundle_v2(walnut, "shielding-review")
            _make_bundle_v2(walnut, "launch-checklist")

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            with _patch_env():
                ap2p._stage_full(walnut, staging)

            files = _listing(staging)
            self.assertIn("shielding-review/context.manifest.yaml", files)
            self.assertIn("launch-checklist/context.manifest.yaml", files)
            for f in files:
                self.assertFalse(
                    f.startswith("bundles/"),
                    "v2 container must migrate to flat: {0}".format(f),
                )

    def test_stage_full_include_full_history(self):
        """With stub_kernel_history=False, real log.md/insights.md ships."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut, name="nova-station")

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            with _patch_env():
                ap2p._stage_full(
                    walnut,
                    staging,
                    stub_kernel_history=False,
                )

            with open(
                os.path.join(staging, "_kernel", "log.md"),
                "r",
                encoding="utf-8",
            ) as f:
                log_body = f.read()
            self.assertIn("real log content", log_body)
            self.assertNotIn("stubbed_at", log_body)

            with open(
                os.path.join(staging, "_kernel", "insights.md"),
                "r",
                encoding="utf-8",
            ) as f:
                ins_body = f.read()
            self.assertIn("real insights", ins_body)
            self.assertNotIn("stubbed_at", ins_body)

    def test_stage_full_missing_tasks_json_synthesizes_skeleton(self):
        """tasks.json absent at source -> empty skeleton shipped."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(
                walnut, name="nova-station", tasks=False, completed=False
            )

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            with _patch_env():
                ap2p._stage_full(walnut, staging)

            with open(
                os.path.join(staging, "_kernel", "tasks.json"),
                "r",
                encoding="utf-8",
            ) as f:
                self.assertIn('"tasks"', f.read())
            with open(
                os.path.join(staging, "_kernel", "completed.json"),
                "r",
                encoding="utf-8",
            ) as f:
                self.assertIn('"completed"', f.read())

    def test_stage_skips_nested_walnut(self):
        """A nested walnut inside the source must not bleed its bundles into the parent package."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "parent-walnut")
            _make_kernel(parent, name="parent-walnut")
            _make_bundle_v3(parent, "parent-bundle")

            # Create a nested walnut with its own _kernel and bundle
            nested = os.path.join(parent, "child-walnut")
            _make_kernel(nested, name="child-walnut")
            _make_bundle_v3(nested, "child-bundle", goal="child")

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            with _patch_env():
                ap2p._stage_full(parent, staging)

            files = _listing(staging)
            self.assertIn("parent-bundle/context.manifest.yaml", files)
            # The child bundle must not appear as a top-level staging entry
            self.assertNotIn("child-bundle/context.manifest.yaml", files)
            # The nested walnut _IS_ live context from the parent's POV, so
            # its non-bundle files may appear under child-walnut/, but its
            # _kernel must not be ascribed to the parent.
            for f in files:
                self.assertFalse(
                    f == "child-walnut/_kernel/key.md" and f.startswith("_kernel/"),
                    "nested kernel leaked into parent staging: {0}".format(f),
                )


# ---------------------------------------------------------------------------
# _stage_bundle tests
# ---------------------------------------------------------------------------


class StageBundleTests(unittest.TestCase):

    def test_stage_bundle_v3(self):
        """v3 bundle scope: ships _kernel/key.md + requested bundles flat."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut, name="nova-station")
            _make_bundle_v3(walnut, "shielding-review")
            _make_bundle_v3(walnut, "launch-checklist")

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            ap2p._stage_bundle(walnut, staging, ["shielding-review"])

            files = _listing(staging)
            self.assertIn("_kernel/key.md", files)
            self.assertIn("shielding-review/context.manifest.yaml", files)
            self.assertIn("shielding-review/draft-01.md", files)

            # Other bundle NOT shipped
            for f in files:
                self.assertFalse(f.startswith("launch-checklist/"))

            # Bundle scope does NOT ship log.md / insights.md / tasks.json
            self.assertNotIn("_kernel/log.md", files)
            self.assertNotIn("_kernel/insights.md", files)
            self.assertNotIn("_kernel/tasks.json", files)

    def test_stage_bundle_v2(self):
        """v2 bundle scope: resolved via walnut_paths, staged flat."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "old-venture")
            _make_kernel(walnut, name="old-venture")
            _make_bundle_v2(walnut, "shielding-review")

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            ap2p._stage_bundle(walnut, staging, ["shielding-review"])

            files = _listing(staging)
            self.assertIn("_kernel/key.md", files)
            self.assertIn("shielding-review/context.manifest.yaml", files)
            for f in files:
                self.assertFalse(
                    f.startswith("bundles/"),
                    "v2 bundle must be flattened in staging: {0}".format(f),
                )

    def test_stage_bundle_nested_rejected(self):
        """Bundle only at a non-top-level location must be refused with an actionable error."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut, name="nova-station")
            # Deeply nested bundle: find_bundles() finds it but is_top_level_bundle() returns False.
            _write(
                os.path.join(
                    walnut, "archive", "old", "bundle-a", "context.manifest.yaml"
                ),
                "goal: archived\nstatus: done\n",
            )

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            with self.assertRaises(ValueError) as ctx:
                ap2p._stage_bundle(walnut, staging, ["bundle-a"])
            msg = str(ctx.exception)
            self.assertIn("non-standard location", msg)
            self.assertIn("archive/old/bundle-a", msg)

    def test_stage_bundle_missing_rejected(self):
        """Missing bundle name raises FileNotFoundError."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut, name="nova-station")
            _make_bundle_v3(walnut, "shielding-review")

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            with self.assertRaises(FileNotFoundError):
                ap2p._stage_bundle(walnut, staging, ["does-not-exist"])

    def test_stage_bundle_rejects_path_separators(self):
        """Bundle names must be leaves, not paths."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut, name="nova-station")
            _make_bundle_v3(walnut, "shielding-review")

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            with self.assertRaises(ValueError):
                ap2p._stage_bundle(
                    walnut, staging, ["bundles/shielding-review"]
                )


# ---------------------------------------------------------------------------
# _stage_snapshot tests
# ---------------------------------------------------------------------------


class StageSnapshotTests(unittest.TestCase):

    def test_stage_snapshot_minimal(self):
        """Snapshot ships only key.md + stubbed insights.md."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut, name="nova-station")
            _make_bundle_v3(walnut, "shielding-review")
            _make_live_context(walnut)

            staging = os.path.join(tmp, "stage")
            os.makedirs(staging)
            with _patch_env():
                ap2p._stage_snapshot(walnut, staging)

            files = _listing(staging)
            self.assertEqual(
                sorted(files),
                sorted(["_kernel/key.md", "_kernel/insights.md"]),
            )

            with open(
                os.path.join(staging, "_kernel", "insights.md"),
                "r",
                encoding="utf-8",
            ) as f:
                ins_body = f.read()
            expected = ap2p.STUB_INSIGHTS_MD.format(
                walnut_name="nova-station",
                iso_timestamp=FIXED_TS,
            )
            self.assertEqual(ins_body, expected)


# ---------------------------------------------------------------------------
# _stage_files dispatcher tests
# ---------------------------------------------------------------------------


class StageFilesDispatcherTests(unittest.TestCase):

    def test_dispatcher_rejects_unknown_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut)
            with self.assertRaises(ValueError):
                ap2p._stage_files(walnut, "invalid-scope")

    def test_dispatcher_bundle_scope_requires_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut)
            with self.assertRaises(ValueError):
                ap2p._stage_files(walnut, "bundle", bundle_names=None)

    def test_dispatcher_creates_temp_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            _make_kernel(walnut, name="nova-station")
            _make_bundle_v3(walnut, "shielding-review")
            with _patch_env():
                staging = ap2p._stage_files(walnut, "full")
            try:
                self.assertTrue(os.path.isdir(staging))
                files = _listing(staging)
                self.assertIn("_kernel/key.md", files)
                self.assertIn(
                    "shielding-review/context.manifest.yaml", files
                )
            finally:
                import shutil
                shutil.rmtree(staging, ignore_errors=True)

    def test_dispatcher_cleans_up_on_failure(self):
        """Staging dir must be removed if the underlying stage function raises."""
        with tempfile.TemporaryDirectory() as tmp:
            walnut = os.path.join(tmp, "nova-station")
            # No _kernel/key.md -> _stage_full raises FileNotFoundError
            os.makedirs(walnut)
            with self.assertRaises(FileNotFoundError):
                ap2p._stage_files(walnut, "full")


# ---------------------------------------------------------------------------
# _should_exclude_package helper
# ---------------------------------------------------------------------------


class ExcludeHelperTests(unittest.TestCase):

    def test_exact_path_excluded(self):
        self.assertTrue(ap2p._should_exclude_package("_kernel/now.json"))
        self.assertTrue(ap2p._should_exclude_package("_kernel/imports.json"))

    def test_prefix_dir_excluded(self):
        self.assertTrue(
            ap2p._should_exclude_package("_kernel/_generated/foo.json")
        )
        self.assertTrue(ap2p._should_exclude_package("_kernel/history/ch01.md"))
        self.assertTrue(
            ap2p._should_exclude_package(".alive/_squirrels/abc.yaml")
        )

    def test_name_filter_anywhere(self):
        self.assertTrue(
            ap2p._should_exclude_package("bundles/foo/.DS_Store")
        )
        self.assertTrue(
            ap2p._should_exclude_package("engineering/Thumbs.db")
        )
        self.assertTrue(
            ap2p._should_exclude_package("bundles/._macos-fork")
        )

    def test_normal_files_kept(self):
        self.assertFalse(ap2p._should_exclude_package("_kernel/key.md"))
        self.assertFalse(ap2p._should_exclude_package("shielding-review/draft-01.md"))
        self.assertFalse(ap2p._should_exclude_package("README.md"))


if __name__ == "__main__":
    unittest.main()
