#!/usr/bin/env python3
"""End-to-end tests for the v2 -> v3 migration path inside ``receive_package``
(task fn-7-7cw.9).

The lower-level ``migrate_v2_layout`` helper is exhaustively tested in
``test_migrate.py``. The basic auto-trigger case is covered in
``test_receive.py::V2MigrationOnReceiveTests``. This module focuses on the
SURFACING and INTEGRATION layer added in fn-7-7cw.9:

- Migration result is captured and threaded into the receive return dict
- Migration block is rendered above the standard preview when source_layout
  is v2
- Structural inference (no manifest hint) still routes a v2 staging tree
  through the migration step
- Bundle-scope receive of a v2 package migrates and lands flat in the
  target without touching the target's _kernel sources (LD18)
- Migration is idempotent across receive boundaries
- Migration failure aborts cleanly and preserves staging as
  ``.alive-receive-incomplete-{ts}/`` next to the target without touching
  the target walnut
- v2 ``tasks.md`` checklists are converted into v3 ``tasks.json`` with the
  expected entries

All fixtures are built programmatically with the stdlib only (no PyYAML,
no fixtures on disk, no subprocesses) so the suite stays fast and
hermetic. Each test builds a v2-shaped staging tree, calls
``generate_manifest`` against it, packs it with ``safe_tar_create``, then
feeds the resulting ``.walnut`` file through ``receive_package``.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_receive_migration -v
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock  # noqa: F401  -- mock used in receive failure test


# ---------------------------------------------------------------------------
# Module loading -- alive-p2p.py has a hyphen in the filename, so use
# importlib to bind the module under a Python-friendly name. Same pattern
# as test_receive.py / test_migrate.py.
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


FIXED_TS = "2026-04-07T12:00:00Z"
FIXED_SESSION = "test-session-mig"
FIXED_SENDER = "test-sender-mig"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_v2_staging_tree(staging, walnut_name="src-v2", bundles=None,
                          with_generated=True, live_dirs=None):
    """Build a v2-shaped staging tree at ``staging``.

    A v2 tree has:
        - ``_kernel/{key.md, log.md, insights.md}``
        - optional ``_kernel/_generated/now.json`` (the v2 marker)
        - ``bundles/{name}/...`` per bundle dict in ``bundles``
        - optional flat live-context dirs at the root

    Each entry in ``bundles`` is a dict with keys:
        name (str), tasks_md (str|None), draft (bool), context (str|None)
    """
    _write(
        os.path.join(staging, "_kernel", "key.md"),
        "---\ntype: venture\nname: {0}\n---\n".format(walnut_name),
    )
    _write(
        os.path.join(staging, "_kernel", "log.md"),
        ("---\nwalnut: {0}\ncreated: 2026-01-01\nlast-entry: "
         "2026-04-01T00:00:00Z\nentry-count: 1\nsummary: src\n---\n\n"
         "## 2026-04-01T00:00:00Z - squirrel:src\n\nseed.\n\n"
         "signed: squirrel:src\n").format(walnut_name),
    )
    _write(
        os.path.join(staging, "_kernel", "insights.md"),
        "---\nwalnut: {0}\n---\n\nseed insights\n".format(walnut_name),
    )
    _write(
        os.path.join(staging, "_kernel", "tasks.json"),
        '{"tasks": []}\n',
    )
    _write(
        os.path.join(staging, "_kernel", "completed.json"),
        '{"completed": []}\n',
    )
    if with_generated:
        _write(
            os.path.join(staging, "_kernel", "_generated", "now.json"),
            '{"phase": "active", "updated": "2026-03-01T00:00:00Z"}\n',
        )

    for bundle in bundles or []:
        name = bundle["name"]
        base = os.path.join(staging, "bundles", name)
        ctx = bundle.get("context", "goal: test {0}\nstatus: active\n".format(name))
        _write(os.path.join(base, "context.manifest.yaml"), ctx)
        if bundle.get("draft", True):
            _write(
                os.path.join(base, "{0}-draft-01.md".format(name)),
                "# {0}\n\ncontent\n".format(name),
            )
        if bundle.get("tasks_md") is not None:
            _write(os.path.join(base, "tasks.md"), bundle["tasks_md"])
        if bundle.get("observations", True):
            _write(
                os.path.join(base, "observations.md"),
                "## 2026-03-01\nobservation\n",
            )

    for live in live_dirs or []:
        _write(
            os.path.join(staging, live, "README.md"),
            "# live {0}\n".format(live),
        )


def _build_v2_package(workdir, package_name, walnut_name="src-v2",
                      bundles=None, scope="full", with_generated=True,
                      live_dirs=None):
    """Build a v2 package on disk and return its path.

    The staging tree is shaped v2 (bundles/ container + _kernel/_generated/),
    a manifest is generated against the AS-BUILT tree (so payload sha256
    matches), and the result is packed via ``safe_tar_create``. The return
    value is the absolute path to the .walnut file.
    """
    staging = tempfile.mkdtemp(prefix="v2-staging-", dir=workdir)
    _make_v2_staging_tree(
        staging,
        walnut_name=walnut_name,
        bundles=bundles or [],
        with_generated=with_generated,
        live_dirs=live_dirs or [],
    )

    bundle_names = None
    if scope == "bundle":
        bundle_names = [b["name"] for b in (bundles or [])]
        # In the v2 layout the manifest's bundles[] field carries the LEAF
        # names (not "bundles/<name>") -- the receive pipeline expects them
        # under the same key as v3 bundle scope packages.

    with _patch_env():
        ap2p.generate_manifest(
            staging,
            scope,
            walnut_name,
            bundles=bundle_names,
            description="v2 fixture",
            note="",
            session_id=FIXED_SESSION,
            engine="test-engine",
            plugin_version="3.1.0",
            sender=FIXED_SENDER,
            exclusions_applied=[],
            substitutions_applied=[],
            source_layout="v2",
        )

    output = os.path.join(workdir, package_name)
    ap2p.safe_tar_create(staging, output)
    shutil.rmtree(staging, ignore_errors=True)
    return output


def _make_v3_target_walnut(parent, name="dst-v3"):
    """Build a minimal v3 target walnut suitable for bundle-scope receive."""
    walnut = os.path.join(parent, name)
    _write(
        os.path.join(walnut, "_kernel", "key.md"),
        "---\ntype: venture\nname: {0}\n---\n".format(name),
    )
    _write(
        os.path.join(walnut, "_kernel", "log.md"),
        ("---\nwalnut: {0}\ncreated: 2026-01-01\nlast-entry: "
         "2026-04-01T00:00:00Z\nentry-count: 1\nsummary: dst\n---\n\n"
         "## 2026-04-01T00:00:00Z - squirrel:dst\n\nseed.\n\n"
         "signed: squirrel:dst\n").format(name),
    )
    _write(
        os.path.join(walnut, "_kernel", "insights.md"),
        "---\nwalnut: {0}\n---\n".format(name),
    )
    _write(
        os.path.join(walnut, "_kernel", "tasks.json"),
        '{"tasks": []}\n',
    )
    _write(
        os.path.join(walnut, "_kernel", "completed.json"),
        '{"completed": []}\n',
    )
    return walnut


@contextmanager
def _patch_env():
    """Pin time/session/sender so produced manifests are reproducible."""
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
    """Skip the LD1 step 12 project.py invocation -- the test fixture
    has no real plugin tree to invoke."""
    prev = os.environ.get("ALIVE_P2P_SKIP_REGEN")
    os.environ["ALIVE_P2P_SKIP_REGEN"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("ALIVE_P2P_SKIP_REGEN", None)
        else:
            os.environ["ALIVE_P2P_SKIP_REGEN"] = prev


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class FullScopeV2ReceiveTests(unittest.TestCase):
    """Receiving a v2 full-scope package into a fresh v3 target."""

    def test_receive_v2_full_package_migrates(self):
        """A full-scope v2 package becomes a flat v3 walnut at the target,
        with bundles at the root and tasks.json populated from tasks.md."""
        with tempfile.TemporaryDirectory() as parent:
            package = _build_v2_package(
                parent,
                "v2-full.walnut",
                walnut_name="src-v2",
                bundles=[
                    {"name": "shielding-review",
                     "tasks_md": "## Active\n- [ ] Plan review\n- [x] Source vendors\n"},
                    {"name": "launch-checklist",
                     "tasks_md": "- [ ] Item one\n- [ ] Item two\n- [ ] Item three\n"},
                ],
            )
            target = os.path.join(parent, "received")

            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=package,
                    target_path=target,
                    yes=True,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["source_layout"], "v2")
            # Migration result threaded through the return dict.
            mig = result["migration"]
            self.assertIsNotNone(mig)
            self.assertEqual(mig["errors"], [])
            self.assertIn("shielding-review", mig["bundles_migrated"])
            self.assertIn("launch-checklist", mig["bundles_migrated"])
            self.assertEqual(mig["tasks_converted"], 5)

            # No bundles/ container at the target -- v3 flat layout.
            self.assertFalse(os.path.isdir(os.path.join(target, "bundles")))

            # Bundles landed flat with their context.manifest.yaml intact.
            self.assertTrue(os.path.isfile(
                os.path.join(target, "shielding-review", "context.manifest.yaml")
            ))
            self.assertTrue(os.path.isfile(
                os.path.join(target, "launch-checklist", "context.manifest.yaml")
            ))

            # tasks.md was converted to tasks.json and the markdown removed.
            tasks_json = os.path.join(target, "shielding-review", "tasks.json")
            self.assertTrue(os.path.isfile(tasks_json))
            self.assertFalse(os.path.isfile(
                os.path.join(target, "shielding-review", "tasks.md")
            ))
            with open(tasks_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["tasks"]), 2)
            titles = [t["title"] for t in data["tasks"]]
            self.assertEqual(titles, ["Plan review", "Source vendors"])
            self.assertEqual(data["tasks"][0]["status"], "active")
            self.assertEqual(data["tasks"][1]["status"], "done")
            self.assertEqual(data["tasks"][0]["bundle"], "shielding-review")

            # _kernel/_generated was dropped during migration; it must NOT
            # appear at the target.
            self.assertFalse(os.path.isdir(
                os.path.join(target, "_kernel", "_generated")
            ))

            # Imports ledger records source_layout=v2 for the entry.
            ledger_path = os.path.join(target, "_kernel", "imports.json")
            self.assertTrue(os.path.isfile(ledger_path))
            with open(ledger_path) as f:
                ledger = json.load(f)
            self.assertEqual(ledger["imports"][-1]["source_layout"], "v2")


class BundleScopeV2ReceiveTests(unittest.TestCase):
    """Bundle-scope v2 package receive into an existing v3 target."""

    def test_receive_v2_bundle_package_migrates(self):
        """A v2 bundle-scope package lands flat in an existing v3 walnut
        without touching the target's _kernel source files (LD18)."""
        with tempfile.TemporaryDirectory() as parent:
            target = _make_v3_target_walnut(parent, name="dst-v3")
            with open(os.path.join(target, "_kernel", "key.md"), "rb") as f:
                target_key_before = f.read()
            with open(os.path.join(target, "_kernel", "log.md"), "rb") as f:
                target_log_before = f.read()

            # The package's _kernel/key.md must match the target's so the
            # LD18 walnut identity check passes. Build the v2 package with
            # the SAME walnut name so the staging-emitted key.md matches.
            # Then we have to byte-overwrite the staging key.md to match
            # the target walnut's exact bytes -- simplest path: write the
            # same content into both.
            package = _build_v2_package(
                parent,
                "v2-bundle.walnut",
                walnut_name="dst-v3",
                bundles=[
                    {"name": "extra-bundle",
                     "tasks_md": "- [ ] do thing\n- [~] urgent\n"},
                ],
                scope="bundle",
            )

            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=package,
                    target_path=target,
                    yes=True,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["source_layout"], "v2")
            self.assertEqual(result["scope"], "bundle")
            self.assertEqual(result["applied_bundles"], ["extra-bundle"])

            # Bundle landed flat at the target.
            self.assertTrue(os.path.isfile(
                os.path.join(target, "extra-bundle", "context.manifest.yaml")
            ))
            self.assertTrue(os.path.isfile(
                os.path.join(target, "extra-bundle", "tasks.json")
            ))
            # No bundles/ container leaked into the target.
            self.assertFalse(os.path.isdir(os.path.join(target, "bundles")))

            # Target's pre-existing kernel sources are byte-identical except
            # for log.md which gets a new prepended entry.
            with open(os.path.join(target, "_kernel", "key.md"), "rb") as f:
                target_key_after = f.read()
            self.assertEqual(target_key_before, target_key_after)
            with open(os.path.join(target, "_kernel", "log.md"), "rb") as f:
                target_log_after = f.read()
            self.assertNotEqual(target_log_before, target_log_after)
            self.assertIn(b"extra-bundle", target_log_after)

            # tasks.json content
            with open(os.path.join(target, "extra-bundle", "tasks.json")) as f:
                data = json.load(f)
            self.assertEqual(len(data["tasks"]), 2)
            self.assertEqual(data["tasks"][0]["title"], "do thing")
            self.assertEqual(data["tasks"][0]["priority"], "normal")
            self.assertEqual(data["tasks"][1]["title"], "urgent")
            self.assertEqual(data["tasks"][1]["priority"], "high")


class StructuralInferenceReceiveTests(unittest.TestCase):
    """When the manifest has no source_layout hint, structural inference
    must still catch v2 staging trees and route them through migration."""

    def test_receive_v2_no_source_layout_hint_structural_detection(self):
        with tempfile.TemporaryDirectory() as parent:
            staging = tempfile.mkdtemp(prefix="hint-staging-", dir=parent)
            _make_v2_staging_tree(
                staging,
                walnut_name="hint-v2",
                bundles=[{"name": "alpha",
                          "tasks_md": "- [ ] only one\n"}],
                with_generated=True,
            )

            # Generate manifest WITHOUT source_layout=v2; pass v3 so the
            # manifest field is "v3" but the staging tree is structurally
            # v2 (bundles/ container + _kernel/_generated/). Receive must
            # fall back to structural inference and rule that as v2.
            #
            # generate_manifest validates source_layout against the allowed
            # set, so we can't pass an empty string -- we pass v3 and rely
            # on _infer_source_layout's manifest_layout != v2/v3 fallback.
            # Since manifest says v3 explicitly, inference WOULD pin it to
            # v3 if not overridden. To test the structural fallback, we
            # must scrub the source_layout field from the manifest after
            # generation.
            with _patch_env():
                ap2p.generate_manifest(
                    staging, "full", "hint-v2",
                    description="hint test", note="",
                    session_id=FIXED_SESSION, engine="test-engine",
                    plugin_version="3.1.0", sender=FIXED_SENDER,
                    exclusions_applied=[], substitutions_applied=[],
                    source_layout="v3",
                )

            # Scrub source_layout from manifest.yaml so inference uses
            # structural detection only. The receiver tolerates a missing
            # field.
            mpath = os.path.join(staging, "manifest.yaml")
            with open(mpath, "r", encoding="utf-8") as f:
                lines = [
                    ln for ln in f.readlines() if "source_layout" not in ln
                ]
            with open(mpath, "w", encoding="utf-8") as f:
                f.writelines(lines)

            output = os.path.join(parent, "no-hint.walnut")
            ap2p.safe_tar_create(staging, output)
            shutil.rmtree(staging, ignore_errors=True)

            target = os.path.join(parent, "received-hint")
            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=output,
                    target_path=target,
                    yes=True,
                )

            # Structural inference triggered the v2 path even though the
            # manifest hint was missing.
            self.assertEqual(result["source_layout"], "v2")
            self.assertIsNotNone(result["migration"])
            self.assertTrue(os.path.isdir(os.path.join(target, "alpha")))
            self.assertFalse(os.path.isdir(os.path.join(target, "bundles")))


class PreviewSurfacingTests(unittest.TestCase):
    """The migration block must appear above the standard preview when
    receive runs against a v2 package."""

    def test_receive_v2_migration_preview_display(self):
        with tempfile.TemporaryDirectory() as parent:
            package = _build_v2_package(
                parent,
                "v2-preview.walnut",
                walnut_name="prev-v2",
                bundles=[
                    {"name": "alpha",
                     "tasks_md": "- [ ] task one\n- [x] task two\n"},
                ],
            )
            target = os.path.join(parent, "received-preview")
            stdout = io.StringIO()

            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=package,
                    target_path=target,
                    yes=True,
                    stdout=stdout,
                )

            self.assertEqual(result["status"], "ok")
            captured = stdout.getvalue()
            # The bordered migration block must appear ABOVE the standard
            # preview header.
            mig_idx = captured.find("v2 -> v3 migration required")
            preview_idx = captured.find("=== receive preview ===")
            self.assertNotEqual(mig_idx, -1, "migration block missing")
            self.assertNotEqual(preview_idx, -1, "preview block missing")
            self.assertLess(
                mig_idx, preview_idx,
                "migration block must render before the standard preview",
            )

            # Migration block must enumerate the actions and tasks count.
            self.assertIn("Dropped _kernel/_generated/", captured)
            self.assertIn("Flattened bundles/alpha -> alpha", captured)
            self.assertIn("Converted alpha/tasks.md -> tasks.json", captured)
            self.assertIn("Bundles migrated: alpha", captured)
            self.assertIn("Tasks converted:  2", captured)
            self.assertIn("Package source_layout: v2", captured)

    def test_v3_receive_does_not_show_migration_block(self):
        """v3 packages must NOT render the migration block (negative test)."""
        # Build a v3 package the conventional way via create_package.
        with tempfile.TemporaryDirectory() as parent:
            os.makedirs(os.path.join(parent, ".alive"))
            walnut = os.path.join(parent, "v3-src")
            _write(
                os.path.join(walnut, "_kernel", "key.md"),
                "---\ntype: venture\nname: v3-src\n---\n",
            )
            _write(
                os.path.join(walnut, "_kernel", "log.md"),
                "---\nwalnut: v3-src\ncreated: 2026-01-01\nlast-entry: "
                "2026-04-01T00:00:00Z\nentry-count: 1\nsummary: x\n---\n\n"
                "## 2026-04-01T00:00:00Z - squirrel:s\n\nx\n\n"
                "signed: squirrel:s\n",
            )
            _write(
                os.path.join(walnut, "_kernel", "insights.md"),
                "---\nwalnut: v3-src\n---\n",
            )
            _write(
                os.path.join(walnut, "_kernel", "tasks.json"),
                '{"tasks": []}\n',
            )
            _write(
                os.path.join(walnut, "_kernel", "completed.json"),
                '{"completed": []}\n',
            )
            _write(
                os.path.join(walnut, "alpha", "context.manifest.yaml"),
                "goal: x\nstatus: active\n",
            )
            output = os.path.join(parent, "v3.walnut")
            with _patch_env():
                ap2p.create_package(
                    walnut_path=walnut, scope="full",
                    output_path=output,
                )
            target = os.path.join(parent, "received-v3")
            stdout = io.StringIO()
            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=output,
                    target_path=target,
                    yes=True,
                    stdout=stdout,
                )
            self.assertEqual(result["source_layout"], "v3")
            self.assertIsNone(result["migration"])
            captured = stdout.getvalue()
            self.assertNotIn("v2 -> v3 migration required", captured)


class IdempotencyAcrossReceivesTests(unittest.TestCase):
    """Receiving the same v2 package into TWO fresh targets must produce
    identical migrated layouts (same bundles, same tasks.json structure).
    """

    def test_receive_v2_idempotent_migration(self):
        with tempfile.TemporaryDirectory() as parent:
            package = _build_v2_package(
                parent,
                "v2-idem.walnut",
                walnut_name="idem-v2",
                bundles=[
                    {"name": "alpha",
                     "tasks_md": "- [ ] one\n- [~] two\n- [x] three\n"},
                ],
            )
            target_a = os.path.join(parent, "recv-a")
            target_b = os.path.join(parent, "recv-b")
            with _patch_env(), _skip_regen():
                ra = ap2p.receive_package(
                    package_path=package, target_path=target_a, yes=True,
                )
                rb = ap2p.receive_package(
                    package_path=package, target_path=target_b, yes=True,
                )

            # Both succeed, same layout, same migration shape.
            self.assertEqual(ra["status"], "ok")
            self.assertEqual(rb["status"], "ok")
            self.assertEqual(
                ra["migration"]["bundles_migrated"],
                rb["migration"]["bundles_migrated"],
            )
            self.assertEqual(
                ra["migration"]["tasks_converted"],
                rb["migration"]["tasks_converted"],
            )

            # The two migrated targets have byte-identical tasks.json files
            # for the alpha bundle (timestamps are pinned by _patch_env).
            with open(os.path.join(target_a, "alpha", "tasks.json")) as f:
                a_tasks = json.load(f)
            with open(os.path.join(target_b, "alpha", "tasks.json")) as f:
                b_tasks = json.load(f)
            self.assertEqual(a_tasks, b_tasks)


class MigrationFailureRollbackTests(unittest.TestCase):
    """Migration failure must abort receive cleanly: target untouched,
    staging preserved as ``.alive-receive-incomplete-{ts}/`` for
    diagnosis.
    """

    def test_receive_v2_migration_failure_preserves_staging_no_target(self):
        with tempfile.TemporaryDirectory() as parent:
            package = _build_v2_package(
                parent,
                "v2-fail.walnut",
                walnut_name="fail-v2",
                bundles=[
                    {"name": "alpha", "tasks_md": "- [ ] one\n"},
                ],
            )
            target = os.path.join(parent, "received-fail")

            # Inject failure: monkey-patch migrate_v2_layout to return an
            # error from inside receive_package's call. This simulates
            # ANY mid-migration failure (corrupted staging, permission
            # denied, parse failure) without depending on platform-
            # specific tricks like read-only mounts.
            real_fn = ap2p.migrate_v2_layout

            def fail_fn(staging_dir):
                # Run the real migration first so staging is partially
                # rewritten, THEN inject an error to mimic a mid-step
                # failure that left state behind.
                result = real_fn(staging_dir)
                result["errors"].append(
                    "synthetic failure: tasks.md unparseable in alpha"
                )
                return result

            with _patch_env(), _skip_regen(), \
                    mock.patch.object(ap2p, "migrate_v2_layout", side_effect=fail_fn):
                with self.assertRaises(ValueError) as ctx:
                    ap2p.receive_package(
                        package_path=package,
                        target_path=target,
                        yes=True,
                    )
            self.assertIn("v2 -> v3 staging migration failed", str(ctx.exception))
            self.assertIn("synthetic failure", str(ctx.exception))

            # Target was never created.
            self.assertFalse(os.path.exists(target))

            # Staging was preserved as .alive-receive-incomplete-*/.
            siblings = [
                e for e in os.listdir(parent)
                if e.startswith(".alive-receive-incomplete-")
            ]
            self.assertEqual(
                len(siblings), 1,
                "expected one .alive-receive-incomplete-* dir, got {0}".format(
                    siblings
                ),
            )
            preserved = os.path.join(parent, siblings[0])
            self.assertTrue(os.path.isdir(preserved))
            # The preserved dir contains the (partially-migrated) v3-shaped
            # staging tree -- the kernel sources are intact and the alpha
            # bundle was flattened to the root by the real call before the
            # synthetic error fired.
            self.assertTrue(os.path.isfile(
                os.path.join(preserved, "_kernel", "key.md")
            ))
            self.assertTrue(
                os.path.isdir(os.path.join(preserved, "alpha"))
                or os.path.isdir(os.path.join(preserved, "bundles", "alpha"))
            )


class TasksMdConversionAtReceiveTests(unittest.TestCase):
    """Bundle-scoped check: a v2 ``tasks.md`` with N entries becomes a
    ``tasks.json`` with the same N entries after receive. The structure
    of each task entry matches the LD8 schema enforced by
    ``_parse_v2_tasks_md`` -- the receive layer just plumbs it through.
    """

    def test_receive_v2_package_with_tasks_md_conversion(self):
        with tempfile.TemporaryDirectory() as parent:
            tasks_md_content = (
                "## Active\n"
                "- [ ] First task\n"
                "- [~] Urgent thing @bob\n"
                "- [x] Done already\n"
                "\n"
                "## Done\n"
                "- [x] Old completed thing\n"
            )
            package = _build_v2_package(
                parent,
                "v2-tasks.walnut",
                walnut_name="tasks-v2",
                bundles=[
                    {"name": "alpha", "tasks_md": tasks_md_content},
                ],
            )
            target = os.path.join(parent, "received-tasks")
            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=package,
                    target_path=target,
                    yes=True,
                )

            self.assertEqual(result["status"], "ok")

            tasks_json_path = os.path.join(target, "alpha", "tasks.json")
            self.assertTrue(os.path.isfile(tasks_json_path))
            self.assertFalse(os.path.isfile(
                os.path.join(target, "alpha", "tasks.md")
            ))
            with open(tasks_json_path) as f:
                data = json.load(f)
            self.assertEqual(len(data["tasks"]), 4)

            titles = [t["title"] for t in data["tasks"]]
            self.assertEqual(
                titles,
                ["First task", "Urgent thing", "Done already",
                 "Old completed thing"],
            )

            # Status / priority mapping
            self.assertEqual(data["tasks"][0]["status"], "active")
            self.assertEqual(data["tasks"][0]["priority"], "normal")
            self.assertEqual(data["tasks"][1]["status"], "active")
            self.assertEqual(data["tasks"][1]["priority"], "high")
            self.assertEqual(data["tasks"][1]["session"], "bob")
            self.assertEqual(data["tasks"][2]["status"], "done")
            self.assertEqual(data["tasks"][3]["status"], "done")

            # Migration result on the return dict matches.
            self.assertEqual(result["migration"]["tasks_converted"], 4)
            self.assertEqual(
                result["migration"]["bundles_migrated"], ["alpha"],
            )


class CreateReceiveV2RoundTripTests(unittest.TestCase):
    """Integration: ``alive-p2p.py create --source-layout v2`` produces a
    package whose source_layout hint flows through receive's inference and
    settles in the target as a v3 walnut. Note that ``create_package``
    itself does NOT shape staging as v2 (it always emits flat bundles);
    this test confirms the manifest hint alone routes the package through
    the migration step (which becomes a near-no-op since the staging is
    already flat) and the target ends up v3.
    """

    def test_create_with_source_layout_v2_round_trips_to_v3_target(self):
        with tempfile.TemporaryDirectory() as parent:
            os.makedirs(os.path.join(parent, ".alive"))
            walnut = os.path.join(parent, "rt-src")
            _write(
                os.path.join(walnut, "_kernel", "key.md"),
                "---\ntype: venture\nname: rt-src\n---\n",
            )
            _write(
                os.path.join(walnut, "_kernel", "log.md"),
                "---\nwalnut: rt-src\ncreated: 2026-01-01\nlast-entry: "
                "2026-04-01T00:00:00Z\nentry-count: 1\nsummary: x\n---\n\n"
                "## 2026-04-01T00:00:00Z - squirrel:s\n\nx\n\n"
                "signed: squirrel:s\n",
            )
            _write(
                os.path.join(walnut, "_kernel", "insights.md"),
                "---\nwalnut: rt-src\n---\n",
            )
            _write(
                os.path.join(walnut, "_kernel", "tasks.json"),
                '{"tasks": []}\n',
            )
            _write(
                os.path.join(walnut, "_kernel", "completed.json"),
                '{"completed": []}\n',
            )
            _write(
                os.path.join(walnut, "alpha", "context.manifest.yaml"),
                "goal: x\n",
            )
            output = os.path.join(parent, "rt.walnut")

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
                del os.environ["ALIVE_P2P_TESTING"]

            target = os.path.join(parent, "received-rt")
            with _patch_env(), _skip_regen():
                result = ap2p.receive_package(
                    package_path=output,
                    target_path=target,
                    yes=True,
                )

            # The manifest hint set source_layout=v2 so the receiver runs
            # the migration step. The staging is structurally already flat
            # (because create_package always emits flat), so migrate_v2_layout
            # short-circuits to a no-op action and the target ends up v3.
            self.assertEqual(result["source_layout"], "v2")
            self.assertIsNotNone(result["migration"])
            self.assertEqual(result["migration"]["errors"], [])
            self.assertTrue(os.path.isdir(os.path.join(target, "alpha")))
            self.assertFalse(os.path.isdir(os.path.join(target, "bundles")))
            self.assertFalse(os.path.isdir(
                os.path.join(target, "_kernel", "_generated")
            ))


# ---------------------------------------------------------------------------
# Defense-in-depth: .alive/.walnut stripping (LD8 spec carries this from .8
# but the v2 path adds a fresh code path through migrate, so re-verify.)
# ---------------------------------------------------------------------------


class DefenseInDepthV2ReceiveTests(unittest.TestCase):
    """v2 packages must still have ``.alive/`` and ``.walnut/`` stripped
    from the staging tree before migration runs. The strip step is
    LD8 defense in depth (sender should never include them, but if they
    sneak in, the receiver guarantees they don't reach the target).
    """

    def test_receive_v2_strips_alive_and_walnut_dirs(self):
        with tempfile.TemporaryDirectory() as parent:
            staging = tempfile.mkdtemp(prefix="dei-staging-", dir=parent)
            _make_v2_staging_tree(
                staging,
                walnut_name="dei-v2",
                bundles=[{"name": "alpha", "tasks_md": "- [ ] x\n"}],
                with_generated=True,
            )
            # Inject .alive/ and .walnut/ directories into staging BEFORE
            # generating the manifest so they get tracked, then receive
            # must strip them.
            _write(os.path.join(staging, ".alive", "marker.md"), "x\n")
            _write(os.path.join(staging, ".walnut", "marker.md"), "x\n")

            with _patch_env():
                ap2p.generate_manifest(
                    staging, "full", "dei-v2",
                    description="dei test", note="",
                    session_id=FIXED_SESSION, engine="test-engine",
                    plugin_version="3.1.0", sender=FIXED_SENDER,
                    exclusions_applied=[], substitutions_applied=[],
                    source_layout="v2",
                )
            output = os.path.join(parent, "dei.walnut")
            ap2p.safe_tar_create(staging, output)
            shutil.rmtree(staging, ignore_errors=True)

            # Note: stripping happens BEFORE checksum verification, but
            # the stripped files were in the manifest. The checksum step
            # will then fail because the listed files no longer exist.
            # That's fine -- the test asserts the strip happened by
            # observing the ValueError mentioning checksum failure on the
            # stripped paths. This is the documented "defense in depth"
            # contract: stripping is non-negotiable, and a manifest that
            # tracked stripped files is malformed and rejected.
            target = os.path.join(parent, "received-dei")
            with _patch_env(), _skip_regen():
                with self.assertRaises(ValueError) as ctx:
                    ap2p.receive_package(
                        package_path=output,
                        target_path=target,
                        yes=True,
                    )
            err = str(ctx.exception)
            self.assertTrue(
                ".alive" in err or ".walnut" in err
                or "Checksum" in err or "checksum" in err,
                "expected checksum/strip error, got: {0}".format(err),
            )
            # Target must NOT exist.
            self.assertFalse(os.path.exists(target))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
