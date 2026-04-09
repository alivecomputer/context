#!/usr/bin/env python3
"""v2 → v3 migration test matrix with golden fixtures (fn-7-7cw.12).

End-to-end coverage for the v2 → v3 layout migration path. Each case in the
matrix builds a v2 walnut via ``walnut_builder.build_walnut(layout="v2")``,
packages it through ``generate_manifest`` + ``safe_tar_create``, extracts via
``safe_tar_extract``, runs ``migrate_v2_layout`` against the staging tree,
and asserts the post-migration structure plus the migration result dict.

Uses ``unittest.subTest`` to keep parameterised cases visible in failure
output without depending on pytest. All tests are stdlib-only and run
offline. The walnut_compare helper from .11 is used where the migrated
shape can be compared against a builder-emitted v3 expected tree; for
cases where the v3 expected shape diverges from what walnut_builder emits
(e.g. per-bundle tasks.json vs the builder's unified _kernel/tasks.json),
explicit path-existence assertions take over.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_p2p_v2_migration -v
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from unittest import mock


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import walnut_paths  # noqa: E402,F401  -- pre-cache for the loader

_AP2P_PATH = os.path.join(_SCRIPTS, "alive-p2p.py")
_spec = importlib.util.spec_from_file_location("alive_p2p", _AP2P_PATH)
ap2p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap2p)  # type: ignore[union-attr]

from walnut_builder import build_walnut  # noqa: E402
from walnut_compare import walnut_equal  # noqa: E402


FIXED_TS = "2026-04-07T12:00:00Z"
FIXED_SESSION = "test-session-mig12"
FIXED_SENDER = "test-sender-mig12"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _patch_env():
    """Pin time/session/sender so manifests + emitted tasks are reproducible."""
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


def _package_v2_walnut(walnut_path, output_path, scope="full",
                       bundle_names=None):
    # type: (str, str, str, Optional[List[str]]) -> str
    """Stage + manifest + tar a v2 walnut into a .walnut package.

    The walnut is left untouched. A new staging tree is materialised under
    a sibling tempdir, the manifest is generated against it with
    ``source_layout="v2"``, and the tar is written to ``output_path``.

    The staging path is built by copying the walnut tree as-is (the v2
    layout is preserved verbatim, including the ``bundles/`` container and
    any ``_kernel/_generated/`` projection). This bypasses the v3 staging
    helpers entirely so we can preserve the v2 shape end-to-end.
    """
    parent = os.path.dirname(output_path)
    staging = tempfile.mkdtemp(prefix="v2-pkg-", dir=parent)
    # Mirror the walnut into staging.
    for entry in os.listdir(walnut_path):
        src = os.path.join(walnut_path, entry)
        dst = os.path.join(staging, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=False)
        else:
            shutil.copy2(src, dst)

    walnut_name = os.path.basename(os.path.abspath(walnut_path))

    with _patch_env():
        ap2p.generate_manifest(
            staging,
            scope,
            walnut_name,
            bundles=bundle_names if scope == "bundle" else None,
            description="v2 migration fixture",
            note="",
            session_id=FIXED_SESSION,
            engine="test-engine",
            plugin_version="3.1.0",
            sender=FIXED_SENDER,
            exclusions_applied=[],
            substitutions_applied=[],
            source_layout="v2",
        )

    ap2p.safe_tar_create(staging, output_path)
    shutil.rmtree(staging, ignore_errors=True)
    return output_path


def _extract_to_staging(package_path, parent_dir):
    # type: (str, str) -> str
    """Extract a package to a fresh staging dir under ``parent_dir``."""
    staging = tempfile.mkdtemp(prefix="recv-stage-", dir=parent_dir)
    ap2p.safe_tar_extract(package_path, staging)
    return staging


def _read_tasks_json(path):
    # type: (str) -> Dict[str, Any]
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _listing(root):
    # type: (str) -> List[str]
    """Return a sorted list of POSIX relpaths of every file under ``root``."""
    out = []
    root_abs = os.path.abspath(root)
    for dirpath, _dirs, files in os.walk(root_abs):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), root_abs)
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _snapshot_tree(root):
    # type: (str) -> Dict[str, bytes]
    """Snapshot every file in ``root`` as ``{relpath: bytes}``."""
    snap = {}  # type: Dict[str, bytes]
    root_abs = os.path.abspath(root)
    for dirpath, _dirs, files in os.walk(root_abs):
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root_abs).replace(os.sep, "/")
            with open(full, "rb") as fh:
                snap[rel] = fh.read()
    return snap


# ---------------------------------------------------------------------------
# Migration matrix -- LD8 cases (table from task .12 spec)
# ---------------------------------------------------------------------------


class V2ToV3MigrationMatrixTests(unittest.TestCase):
    """Parameterised v2 → v3 migration cases. Each subTest builds a v2
    walnut via the builder, packages it, extracts to staging, runs
    ``migrate_v2_layout``, and asserts post-shape + result counts.
    """

    # ----- Case 1: simple-single-bundle ---------------------------------

    def test_simple_single_bundle(self):
        with self.subTest(case="simple-single-bundle"):
            with tempfile.TemporaryDirectory() as parent:
                tasks_md = (
                    "## Active\n"
                    "- [ ] Task one\n"
                    "- [ ] Task two\n"
                    "- [x] Task three\n"
                )
                walnut = build_walnut(
                    parent, name="src-simple", layout="v2",
                    bundles=[{"name": "alpha", "goal": "Alpha goal"}],
                    tasks={
                        "alpha": [
                            {"title": "Task one"},
                            {"title": "Task two"},
                            {"title": "Task three", "status": "done"},
                        ],
                    },
                )
                pkg = os.path.join(parent, "simple.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(result["errors"], [])
                self.assertEqual(result["bundles_migrated"], ["alpha"])
                self.assertEqual(result["tasks_converted"], 3)

                # v3 flat shape: alpha at root, bundles/ gone.
                self.assertFalse(os.path.isdir(os.path.join(staging, "bundles")))
                self.assertTrue(os.path.isfile(
                    os.path.join(staging, "alpha", "context.manifest.yaml")
                ))
                self.assertTrue(os.path.isfile(
                    os.path.join(staging, "alpha", "tasks.json")
                ))
                tasks = _read_tasks_json(
                    os.path.join(staging, "alpha", "tasks.json")
                )
                self.assertEqual(len(tasks["tasks"]), 3)
                titles = [t["title"] for t in tasks["tasks"]]
                self.assertEqual(titles, ["Task one", "Task two", "Task three"])
                self.assertEqual(tasks["tasks"][2]["status"], "done")

    # ----- Case 2: multi-bundle ----------------------------------------

    def test_multi_bundle(self):
        with self.subTest(case="multi-bundle"):
            with tempfile.TemporaryDirectory() as parent:
                walnut = build_walnut(
                    parent, name="src-multi", layout="v2",
                    bundles=[
                        {"name": "alpha", "goal": "A"},
                        {"name": "beta", "goal": "B"},
                        {"name": "gamma", "goal": "G"},
                    ],
                    tasks={
                        "alpha": [{"title": "a1"}, {"title": "a2"}],
                        "beta": [{"title": "b1"}],
                        "gamma": [
                            {"title": "g1"},
                            {"title": "g2"},
                            {"title": "g3"},
                        ],
                    },
                )
                pkg = os.path.join(parent, "multi.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(result["errors"], [])
                self.assertEqual(
                    sorted(result["bundles_migrated"]),
                    ["alpha", "beta", "gamma"],
                )
                self.assertEqual(result["tasks_converted"], 6)

                for name, expected in [("alpha", 2), ("beta", 1), ("gamma", 3)]:
                    tj = os.path.join(staging, name, "tasks.json")
                    self.assertTrue(os.path.isfile(tj),
                                    "{0}/tasks.json missing".format(name))
                    self.assertEqual(
                        len(_read_tasks_json(tj)["tasks"]), expected,
                        "{0} task count mismatch".format(name),
                    )

    # ----- Case 3: bundle-with-raw -------------------------------------

    def test_bundle_with_raw_files(self):
        with self.subTest(case="bundle-with-raw"):
            with tempfile.TemporaryDirectory() as parent:
                walnut = build_walnut(
                    parent, name="src-raw", layout="v2",
                    bundles=[{
                        "name": "transcripts",
                        "goal": "Transcripts",
                        "files": {
                            "transcripts-draft-01.md": "# draft\n",
                        },
                        "raw_files": {
                            "2026-03-01-call.md": "raw call\n",
                            "2026-03-02-meeting.md": "raw meeting\n",
                        },
                    }],
                )
                pkg = os.path.join(parent, "raw.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(result["errors"], [])
                self.assertEqual(result["bundles_migrated"], ["transcripts"])

                # raw/ moved with the bundle and is intact.
                self.assertTrue(os.path.isdir(
                    os.path.join(staging, "transcripts", "raw")
                ))
                self.assertTrue(os.path.isfile(os.path.join(
                    staging, "transcripts", "raw", "2026-03-01-call.md"
                )))
                self.assertTrue(os.path.isfile(os.path.join(
                    staging, "transcripts", "raw", "2026-03-02-meeting.md"
                )))
                # Draft moved with the bundle.
                self.assertTrue(os.path.isfile(os.path.join(
                    staging, "transcripts", "transcripts-draft-01.md"
                )))

    # ----- Case 4: bundle-with-observations -----------------------------

    def test_bundle_with_observations(self):
        with self.subTest(case="bundle-with-observations"):
            with tempfile.TemporaryDirectory() as parent:
                walnut = build_walnut(
                    parent, name="src-obs", layout="v2",
                    bundles=[{
                        "name": "research",
                        "goal": "Research",
                        "files": {
                            "observations.md":
                                "## 2026-03-01\nimportant observation\n",
                        },
                    }],
                )
                pkg = os.path.join(parent, "obs.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(result["errors"], [])
                obs = os.path.join(staging, "research", "observations.md")
                self.assertTrue(os.path.isfile(obs))
                with open(obs) as f:
                    self.assertIn("important observation", f.read())

    # ----- Case 5: bundle-with-sub-walnut -------------------------------

    def test_bundle_with_sub_walnut_not_flattened(self):
        """A bundle that contains a nested walnut MUST preserve the
        sub-walnut intact and NOT flatten its contents into the parent.
        """
        with self.subTest(case="bundle-with-sub-walnut"):
            with tempfile.TemporaryDirectory() as parent:
                # Build the v2 walnut without the sub-walnut first
                walnut = build_walnut(
                    parent, name="src-sub", layout="v2",
                    bundles=[{"name": "parent-bundle", "goal": "p"}],
                )
                # Manually drop a sub-walnut INSIDE the v2 bundle dir.
                sub_kernel = os.path.join(
                    walnut, "bundles", "parent-bundle", "child-walnut", "_kernel"
                )
                os.makedirs(sub_kernel)
                with open(os.path.join(sub_kernel, "key.md"), "w") as f:
                    f.write("---\ntype: venture\nname: child-walnut\n---\n")
                with open(os.path.join(sub_kernel, "log.md"), "w") as f:
                    f.write(
                        "---\nwalnut: child-walnut\ncreated: 2026-01-01\n"
                        "last-entry: 2026-01-01\nentry-count: 0\n"
                        "summary: child\n---\n\n"
                    )
                with open(os.path.join(sub_kernel, "insights.md"), "w") as f:
                    f.write("---\nwalnut: child-walnut\n---\n")

                pkg = os.path.join(parent, "sub.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(result["errors"], [])
                self.assertEqual(result["bundles_migrated"], ["parent-bundle"])

                # Sub-walnut survives nested inside the flattened bundle.
                self.assertTrue(os.path.isfile(os.path.join(
                    staging, "parent-bundle",
                    "child-walnut", "_kernel", "key.md",
                )))
                self.assertTrue(os.path.isfile(os.path.join(
                    staging, "parent-bundle",
                    "child-walnut", "_kernel", "log.md",
                )))
                # Sub-walnut was NOT promoted to the staging root.
                self.assertFalse(os.path.exists(
                    os.path.join(staging, "child-walnut")
                ))

    # ----- Case 6: collision -------------------------------------------

    def test_bundle_name_collision_renames_to_imported(self):
        """v2 bundle ``alpha`` collides with an existing root dir
        ``alpha`` (live context) -- rename to ``alpha-imported``.
        """
        with self.subTest(case="collision"):
            with tempfile.TemporaryDirectory() as parent:
                walnut = build_walnut(
                    parent, name="src-coll", layout="v2",
                    bundles=[{"name": "alpha", "goal": "alpha"}],
                    live_files=[{
                        "path": "alpha/README.md",
                        "content": "# live alpha\n",
                    }],
                )
                pkg = os.path.join(parent, "coll.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(result["errors"], [])
                self.assertEqual(result["bundles_migrated"], ["alpha-imported"])
                # Original live alpha/ intact.
                self.assertTrue(os.path.isfile(
                    os.path.join(staging, "alpha", "README.md")
                ))
                # Bundle landed under the suffix.
                self.assertTrue(os.path.isfile(os.path.join(
                    staging, "alpha-imported", "context.manifest.yaml"
                )))
                self.assertTrue(any(
                    "collision suffix" in a for a in result["actions"]
                ))

    # ----- Case 7: tasks-md-with-assignments ----------------------------

    def test_tasks_md_with_session_assignments(self):
        with self.subTest(case="tasks-md-with-assignments"):
            with tempfile.TemporaryDirectory() as parent:
                walnut = build_walnut(
                    parent, name="src-assign", layout="v2",
                    bundles=[{"name": "alpha", "goal": "A"}],
                )
                # Overwrite the builder-emitted tasks.md with an assigned
                # version (the builder doesn't model @session).
                tasks_md_path = os.path.join(
                    walnut, "bundles", "alpha", "tasks.md"
                )
                with open(tasks_md_path, "w") as f:
                    f.write(
                        "- [ ] Task A @alice\n"
                        "- [ ] Task B @bob\n"
                        "- [~] Task C @carol\n"
                    )

                pkg = os.path.join(parent, "assign.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(result["errors"], [])
                self.assertEqual(result["tasks_converted"], 3)

                tj = _read_tasks_json(
                    os.path.join(staging, "alpha", "tasks.json")
                )
                tasks = tj["tasks"]
                self.assertEqual(len(tasks), 3)
                self.assertEqual(tasks[0]["session"], "alice")
                self.assertEqual(tasks[1]["session"], "bob")
                self.assertEqual(tasks[2]["session"], "carol")

    # ----- Case 8: tasks-md-with-status-markers -------------------------

    def test_tasks_md_with_mixed_status_markers(self):
        with self.subTest(case="tasks-md-with-status-markers"):
            with tempfile.TemporaryDirectory() as parent:
                walnut = build_walnut(
                    parent, name="src-status", layout="v2",
                    bundles=[{"name": "alpha", "goal": "A"}],
                )
                tasks_md_path = os.path.join(
                    walnut, "bundles", "alpha", "tasks.md"
                )
                with open(tasks_md_path, "w") as f:
                    f.write(
                        "## Active\n"
                        "- [ ] open task\n"
                        "- [~] in-progress task\n"
                        "- [x] done task\n"
                    )

                pkg = os.path.join(parent, "status.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                with _patch_env():
                    ap2p.migrate_v2_layout(staging)

                tasks = _read_tasks_json(
                    os.path.join(staging, "alpha", "tasks.json")
                )["tasks"]
                self.assertEqual(len(tasks), 3)

                # ``[ ]`` -> active normal
                self.assertEqual(tasks[0]["status"], "active")
                self.assertEqual(tasks[0]["priority"], "normal")
                # ``[~]`` -> active high
                self.assertEqual(tasks[1]["status"], "active")
                self.assertEqual(tasks[1]["priority"], "high")
                # ``[x]`` -> done normal
                self.assertEqual(tasks[2]["status"], "done")
                self.assertEqual(tasks[2]["priority"], "normal")

    # ----- Case 9: empty-bundles-dir ------------------------------------

    def test_empty_bundles_dir_treated_as_v3(self):
        """v2 walnut with an empty ``bundles/`` container is structurally
        already v3 (no bundles to migrate)."""
        with self.subTest(case="empty-bundles-dir"):
            with tempfile.TemporaryDirectory() as parent:
                walnut = build_walnut(
                    parent, name="src-empty", layout="v2",
                    # include_now_json=False so the only v2 marker is the
                    # bundles/ container, which is empty.
                    include_now_json=False,
                )
                pkg = os.path.join(parent, "empty.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                # The packager + tar may not preserve an empty bundles/
                # directory (regular files only). Drop it from staging if
                # present so the migration sees a clean v3 layout.
                bundles_dir = os.path.join(staging, "bundles")
                if not os.path.isdir(bundles_dir):
                    os.makedirs(bundles_dir)

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(
                    result["actions"], ["no-op (already v3 layout)"],
                )
                self.assertEqual(result["bundles_migrated"], [])
                self.assertEqual(result["tasks_converted"], 0)

    # ----- Case 10: generated-dir ---------------------------------------

    def test_generated_dir_dropped(self):
        with self.subTest(case="generated-dir"):
            with tempfile.TemporaryDirectory() as parent:
                walnut = build_walnut(
                    parent, name="src-gen", layout="v2",
                    bundles=[{"name": "alpha", "goal": "A"}],
                    include_now_json=True,
                )
                # Confirm the v2 walnut had _generated/ before packaging.
                self.assertTrue(os.path.isdir(
                    os.path.join(walnut, "_kernel", "_generated")
                ))
                pkg = os.path.join(parent, "gen.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                # Verify _generated/ travelled into staging via the package.
                self.assertTrue(os.path.isdir(
                    os.path.join(staging, "_kernel", "_generated")
                ))

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(result["errors"], [])
                self.assertIn(
                    "Dropped _kernel/_generated/", result["actions"],
                )
                self.assertFalse(os.path.exists(
                    os.path.join(staging, "_kernel", "_generated")
                ))
                # Other kernel files survived.
                self.assertTrue(os.path.isfile(
                    os.path.join(staging, "_kernel", "key.md")
                ))
                self.assertTrue(os.path.isfile(
                    os.path.join(staging, "_kernel", "log.md")
                ))
                self.assertTrue(os.path.isfile(
                    os.path.join(staging, "_kernel", "insights.md")
                ))

    # ----- Case 11: kernel-history-preserved ----------------------------

    def test_kernel_history_preserved(self):
        with self.subTest(case="kernel-history-preserved"):
            with tempfile.TemporaryDirectory() as parent:
                walnut = build_walnut(
                    parent, name="src-hist", layout="v2",
                    bundles=[{"name": "alpha", "goal": "A"}],
                )
                hist_dir = os.path.join(walnut, "_kernel", "history")
                os.makedirs(hist_dir)
                with open(os.path.join(hist_dir, "chapter-01.md"), "w") as f:
                    f.write("# Chapter 01\n\nolder log entries\n")

                pkg = os.path.join(parent, "hist.walnut")
                _package_v2_walnut(walnut, pkg)
                staging = _extract_to_staging(pkg, parent)

                with _patch_env():
                    result = ap2p.migrate_v2_layout(staging)

                self.assertEqual(result["errors"], [])
                chapter = os.path.join(
                    staging, "_kernel", "history", "chapter-01.md"
                )
                self.assertTrue(os.path.isfile(chapter))
                with open(chapter) as f:
                    self.assertIn("Chapter 01", f.read())


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class V2MigrationIdempotencyTests(unittest.TestCase):
    """Running migrate_v2_layout twice on the same staging dir is a no-op
    on the second pass and produces a byte-identical tree.
    """

    def test_migration_idempotent(self):
        with tempfile.TemporaryDirectory() as parent:
            walnut = build_walnut(
                parent, name="src-idem", layout="v2",
                bundles=[
                    {"name": "alpha", "goal": "A"},
                    {"name": "beta", "goal": "B"},
                ],
                tasks={
                    "alpha": [
                        {"title": "a1"},
                        {"title": "a2", "status": "done"},
                    ],
                    "beta": [{"title": "b1"}],
                },
            )
            pkg = os.path.join(parent, "idem.walnut")
            _package_v2_walnut(walnut, pkg)
            staging = _extract_to_staging(pkg, parent)

            with _patch_env():
                first = ap2p.migrate_v2_layout(staging)
                first_snap = _snapshot_tree(staging)
                first_listing = _listing(staging)
                second = ap2p.migrate_v2_layout(staging)
                second_snap = _snapshot_tree(staging)
                second_listing = _listing(staging)

            # First pass did real work.
            self.assertGreaterEqual(len(first["actions"]), 2)
            self.assertEqual(
                sorted(first["bundles_migrated"]), ["alpha", "beta"]
            )
            self.assertEqual(first["tasks_converted"], 3)

            # Second pass is the documented no-op short-circuit.
            self.assertEqual(
                second["actions"], ["no-op (already v3 layout)"]
            )
            self.assertEqual(second["bundles_migrated"], [])
            self.assertEqual(second["tasks_converted"], 0)
            self.assertEqual(second["warnings"], [])
            self.assertEqual(second["errors"], [])

            # Tree is byte-identical between the two snapshots.
            self.assertEqual(first_listing, second_listing)
            self.assertEqual(first_snap, second_snap)


# ---------------------------------------------------------------------------
# Partial-failure rollback
# ---------------------------------------------------------------------------


class V2ReceivePartialFailureTests(unittest.TestCase):
    """A migration failure during ``receive_package`` MUST leave the
    target untouched and preserve staging as
    ``.alive-receive-incomplete-{ts}/`` next to the target.
    """

    def test_partial_failure_preserves_target(self):
        with tempfile.TemporaryDirectory() as parent:
            # Build a v2 source walnut + v2 package.
            v2_walnut = build_walnut(
                parent, name="src-fail", layout="v2",
                bundles=[{"name": "alpha", "goal": "A"}],
                tasks={"alpha": [{"title": "task"}]},
            )
            pkg = os.path.join(parent, "fail.walnut")
            _package_v2_walnut(v2_walnut, pkg)
            target = os.path.join(parent, "fail-target")

            real_migrate = ap2p.migrate_v2_layout

            def failing_migrate(staging_dir):
                # Run the real migration so staging is partially rewritten,
                # then synthesise an error to mimic a mid-step failure that
                # left state behind. The receive pipeline must NOT touch
                # the target after seeing this error.
                result = real_migrate(staging_dir)
                result["errors"].append(
                    "synthetic failure: bundle 'alpha' unparseable"
                )
                return result

            with _patch_env(), \
                    mock.patch.dict(os.environ,
                                    {"ALIVE_P2P_SKIP_REGEN": "1"}), \
                    mock.patch.object(
                        ap2p, "migrate_v2_layout", side_effect=failing_migrate,
                    ):
                with self.assertRaises(ValueError) as ctx:
                    ap2p.receive_package(
                        package_path=pkg,
                        target_path=target,
                        yes=True,
                    )

            self.assertIn(
                "v2 -> v3 staging migration failed", str(ctx.exception),
            )
            self.assertIn("synthetic failure", str(ctx.exception))

            # Target was never created.
            self.assertFalse(os.path.exists(target))

            # Staging preserved as .alive-receive-incomplete-{ts}/ sibling.
            siblings = [
                e for e in os.listdir(parent)
                if e.startswith(".alive-receive-incomplete-")
            ]
            self.assertEqual(
                len(siblings), 1,
                "expected exactly one .alive-receive-incomplete-* dir, "
                "got {0}".format(siblings),
            )
            preserved = os.path.join(parent, siblings[0])
            self.assertTrue(os.path.isdir(preserved))
            # The preserved dir contains the v2-source kernel files.
            self.assertTrue(os.path.isfile(
                os.path.join(preserved, "_kernel", "key.md")
            ))


# ---------------------------------------------------------------------------
# Format version gate / source_layout hint
# ---------------------------------------------------------------------------


class V2FormatVersionGateTests(unittest.TestCase):
    """Cover both the manifest-hint path and the structural-inference path
    for v2 packages flowing through receive.
    """

    def test_v2_package_with_source_layout_hint_accepts(self):
        """A v2 package whose manifest carries ``source_layout: v2``
        triggers the migration step explicitly via LD7 precedence."""
        with tempfile.TemporaryDirectory() as parent:
            v2_walnut = build_walnut(
                parent, name="src-hint-on", layout="v2",
                bundles=[{"name": "alpha", "goal": "A"}],
                tasks={"alpha": [{"title": "task"}]},
            )
            pkg = os.path.join(parent, "hint-on.walnut")
            _package_v2_walnut(v2_walnut, pkg)

            target = os.path.join(parent, "hint-on-recv")
            with _patch_env(), \
                    mock.patch.dict(os.environ,
                                    {"ALIVE_P2P_SKIP_REGEN": "1"}):
                result = ap2p.receive_package(
                    package_path=pkg,
                    target_path=target,
                    yes=True,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["source_layout"], "v2")
            self.assertIsNotNone(result["migration"])
            self.assertEqual(result["migration"]["errors"], [])
            self.assertEqual(
                result["migration"]["bundles_migrated"], ["alpha"]
            )
            self.assertTrue(os.path.isdir(os.path.join(target, "alpha")))
            self.assertFalse(os.path.isdir(os.path.join(target, "bundles")))

    def test_v2_package_without_hint_structural_detection(self):
        """A package whose manifest is missing ``source_layout`` falls
        back to structural inference. Presence of ``bundles/`` or
        ``_kernel/_generated/`` in staging routes through the v2 path.
        """
        with tempfile.TemporaryDirectory() as parent:
            v2_walnut = build_walnut(
                parent, name="src-hint-off", layout="v2",
                bundles=[{"name": "alpha", "goal": "A"}],
                tasks={"alpha": [{"title": "task"}]},
            )
            pkg = os.path.join(parent, "hint-off.walnut")
            _package_v2_walnut(v2_walnut, pkg)

            # Open the package, scrub the source_layout field from the
            # manifest, and rewrite the tar without it. This forces the
            # receiver to fall back to structural inference (LD7 step 3).
            unpacked = tempfile.mkdtemp(prefix="rewrap-", dir=parent)
            ap2p.safe_tar_extract(pkg, unpacked)
            mpath = os.path.join(unpacked, "manifest.yaml")
            with open(mpath) as f:
                lines = [ln for ln in f if "source_layout" not in ln]
            with open(mpath, "w") as f:
                f.writelines(lines)
            os.unlink(pkg)
            ap2p.safe_tar_create(unpacked, pkg)
            shutil.rmtree(unpacked, ignore_errors=True)

            target = os.path.join(parent, "hint-off-recv")
            with _patch_env(), \
                    mock.patch.dict(os.environ,
                                    {"ALIVE_P2P_SKIP_REGEN": "1"}):
                result = ap2p.receive_package(
                    package_path=pkg,
                    target_path=target,
                    yes=True,
                )

            self.assertEqual(result["source_layout"], "v2")
            self.assertIsNotNone(result["migration"])
            self.assertEqual(result["migration"]["errors"], [])
            self.assertTrue(os.path.isdir(os.path.join(target, "alpha")))
            self.assertFalse(os.path.isdir(os.path.join(target, "bundles")))


# ---------------------------------------------------------------------------
# Cross-version lossy: v3 -> v2 downgrade
# ---------------------------------------------------------------------------


class V3ToV2DowngradeTests(unittest.TestCase):
    """v3 -> v2 downgrade is a hard break (we don't support it).

    The receive pipeline does not implement v3 -> v2 migration. The CLI
    accepts ``--source-layout v2`` on ``create`` only as a manifest-hint
    override -- it does NOT shape the staging tree as v2. Document the
    actual behaviour: a v3 walnut with ``source_layout=v2`` produces a
    package whose staging is structurally already flat, so the receiver's
    migrate step short-circuits to a no-op and the target ends up as v3.
    """

    def test_v3_to_v2_downgrade_documented_behaviour(self):
        with tempfile.TemporaryDirectory() as parent:
            os.makedirs(os.path.join(parent, ".alive"))
            walnut = build_walnut(
                parent, name="src-v3", layout="v3",
                bundles=[{"name": "alpha", "goal": "A"}],
            )

            output = os.path.join(parent, "v3-as-v2.walnut")
            os.environ["ALIVE_P2P_TESTING"] = "1"
            try:
                with _patch_env():
                    ap2p.create_package(
                        walnut_path=walnut,
                        scope="full",
                        output_path=output,
                        source_layout="v2",
                        include_full_history=True,
                    )
            finally:
                os.environ.pop("ALIVE_P2P_TESTING", None)

            # Receive the package. The migrate step short-circuits because
            # the staging tree is structurally flat -- v3 -> v2 downgrade
            # is NOT supported, and this test pins the documented
            # behaviour: the manifest hint flows through but no shape
            # transform happens, target is v3.
            target = os.path.join(parent, "v3-as-v2-recv")
            with _patch_env(), \
                    mock.patch.dict(os.environ,
                                    {"ALIVE_P2P_SKIP_REGEN": "1"}):
                result = ap2p.receive_package(
                    package_path=output,
                    target_path=target,
                    yes=True,
                )

            self.assertEqual(result["source_layout"], "v2")
            self.assertIsNotNone(result["migration"])
            self.assertEqual(result["migration"]["errors"], [])
            # The migrate step is a no-op because the source IS already
            # structurally flat (v3-shaped) -- the manifest hint is the
            # ONLY v2 marker. There are no bundles to flatten and no
            # _generated/ to drop.
            self.assertEqual(
                result["migration"]["actions"], ["no-op (already v3 layout)"],
            )
            # Target ends up as v3 (no bundles/, alpha at root).
            self.assertTrue(os.path.isdir(os.path.join(target, "alpha")))
            self.assertFalse(os.path.isdir(os.path.join(target, "bundles")))


# ---------------------------------------------------------------------------
# walnut_compare integration: v3 expected tree comparison
# ---------------------------------------------------------------------------


class V2MigrationStructuralEqualityTests(unittest.TestCase):
    """For the simple case, the migrated staging tree should match a
    builder-emitted v3 expected tree on every shared file (kernel + bundle
    manifest + raw files). The comparison ignores manifest.yaml (a
    packaging artifact) and the bundle's tasks.json (which is a v3-only
    file with timestamps the builder doesn't emit).
    """

    def test_simple_case_matches_v3_builder_output(self):
        with tempfile.TemporaryDirectory() as parent:
            v2_walnut = build_walnut(
                parent, name="match-src", layout="v2",
                bundles=[{
                    "name": "alpha",
                    "goal": "Alpha goal",
                    "files": {
                        "alpha-draft-01.md": "# alpha draft\n\nbody\n",
                    },
                    "raw_files": {
                        "2026-03-01-source.md": "raw source\n",
                    },
                }],
            )
            pkg = os.path.join(parent, "match.walnut")
            _package_v2_walnut(v2_walnut, pkg)
            staging = _extract_to_staging(pkg, parent)

            with _patch_env():
                result = ap2p.migrate_v2_layout(staging)
            self.assertEqual(result["errors"], [])

            # Build the v3 expected tree with the SAME data via walnut_builder.
            v3_expected = build_walnut(
                parent, name="match-expected", layout="v3",
                bundles=[{
                    "name": "alpha",
                    "goal": "Alpha goal",
                    "files": {
                        "alpha-draft-01.md": "# alpha draft\n\nbody\n",
                    },
                    "raw_files": {
                        "2026-03-01-source.md": "raw source\n",
                    },
                }],
                # Match the v2 walnut's identity so kernel files compare.
                walnut_created="2026-01-01",
            )

            # Compare the migrated staging against the v3 expected tree.
            # Ignore: manifest.yaml (packaging artifact); tasks.json /
            # completed.json (v3 builder vs v2 migration emit different
            # task shapes); _kernel/key.md, log.md, insights.md (the
            # builder embeds the walnut directory name into each kernel
            # template so the two walnuts -- match-src vs match-expected
            # -- diverge by design). The structural assertion this test
            # makes is "every bundle file plus raw/ tree present in the
            # v3 expected is also present and byte-equal in the migrated
            # staging".
            match, diffs = walnut_equal(
                v3_expected,
                staging,
                ignore_patterns=[
                    "manifest.yaml",
                    "_kernel/key.md",
                    "_kernel/log.md",
                    "_kernel/insights.md",
                    "_kernel/tasks.json",
                    "_kernel/completed.json",
                    "alpha/tasks.json",
                ],
            )
            self.assertTrue(
                match,
                "v3 expected vs migrated staging diverged:\n  - "
                + "\n  - ".join(diffs),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
