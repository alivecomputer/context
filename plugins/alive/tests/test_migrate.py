#!/usr/bin/env python3
"""Unit tests for ``migrate_v2_layout`` in ``alive-p2p.py``.

Covers LD6/LD7 (v2 -> v3 staging migration) and the ``migrate`` CLI verb.
Each test builds a v2-shaped fixture tree in a ``tempfile.TemporaryDirectory``,
runs ``migrate_v2_layout``, and asserts on the reshaped staging dir.

The helper is pure file I/O over a staging dir -- no network, no subprocess,
no walnut target -- so tests are fast and isolated. ``now_utc_iso`` and
``resolve_session_id`` are patched to pin the emitted task metadata for
deterministic assertions.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_migrate -v

Stdlib only -- no PyYAML, no third-party assertions.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


# ---------------------------------------------------------------------------
# Module loading: alive-p2p.py has a hyphen in the filename so a plain
# ``import alive_p2p`` does not work. Load it via importlib.util from the
# scripts directory, matching the pattern used in test_staging.py.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import walnut_paths  # noqa: E402,F401  (prime sys.modules cache)

_AP2P_PATH = os.path.join(_SCRIPTS, "alive-p2p.py")
_spec = importlib.util.spec_from_file_location("alive_p2p", _AP2P_PATH)
ap2p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap2p)  # type: ignore[union-attr]


FIXED_TS = "2026-04-07T12:00:00Z"
FIXED_SESSION = "test-session-abc"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_v2_kernel(staging, with_generated=True, with_history=False):
    """Write a v2-shaped ``_kernel/`` into ``staging``.

    Includes source files plus (by default) a ``_generated/now.json`` so the
    first migration step has something to drop. ``with_history`` adds a
    legacy chapter file to prove it survives the migration.
    """
    _write(
        os.path.join(staging, "_kernel", "key.md"),
        "---\ntype: venture\nname: test-walnut\n---\n",
    )
    _write(
        os.path.join(staging, "_kernel", "log.md"),
        "---\nwalnut: test-walnut\nentry-count: 0\n---\n\nempty log\n",
    )
    _write(
        os.path.join(staging, "_kernel", "insights.md"),
        "---\nwalnut: test-walnut\n---\n\nempty insights\n",
    )
    if with_generated:
        _write(
            os.path.join(staging, "_kernel", "_generated", "now.json"),
            '{"phase": "active", "updated": "2026-03-01T00:00:00Z"}\n',
        )
    if with_history:
        _write(
            os.path.join(staging, "_kernel", "history", "chapter-01.md"),
            "# Chapter 01\n\nolder entries\n",
        )


def _make_v2_bundle(staging, name, tasks_md=None, with_raw=True,
                    with_draft=True, with_manifest=True):
    """Write a v2-shaped ``bundles/{name}/`` tree.

    By default the bundle has a context.manifest.yaml, a draft file, a raw/
    source, and an observations.md. ``tasks_md`` (if provided) is written at
    ``bundles/{name}/tasks.md`` -- pass ``None`` to skip.
    """
    base = os.path.join(staging, "bundles", name)
    if with_manifest:
        _write(
            os.path.join(base, "context.manifest.yaml"),
            "goal: test {0}\nstatus: active\n".format(name),
        )
    if with_draft:
        _write(
            os.path.join(base, "{0}-draft-01.md".format(name)),
            "# {0} draft\n\nlorem ipsum\n".format(name),
        )
    if with_raw:
        _write(
            os.path.join(base, "raw", "2026-03-01-note.md"),
            "raw source content\n",
        )
    _write(
        os.path.join(base, "observations.md"),
        "## 2026-03-01\nobservation\n",
    )
    if tasks_md is not None:
        _write(os.path.join(base, "tasks.md"), tasks_md)


def _make_live_dir(staging, name, content="# live\n"):
    """Write a live-context top-level dir (e.g. ``engineering/``) into staging."""
    _write(os.path.join(staging, name, "README.md"), content)


def _listing(staging):
    """Return a sorted list of POSIX relpaths of every file under ``staging``."""
    out = []
    for root, _dirs, files in os.walk(staging):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), staging)
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _patched():
    """Patch ``now_utc_iso`` + ``resolve_session_id`` inside alive_p2p."""
    return _PatchContext()


class _PatchContext(object):
    def __enter__(self):
        self._patches = [
            mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS),
            mock.patch.object(
                ap2p, "resolve_session_id", return_value=FIXED_SESSION
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDropGenerated(unittest.TestCase):
    def test_migrate_drops_generated(self):
        """Step 1: ``_kernel/_generated/`` is removed entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=True)
            _make_v2_bundle(tmp, "alpha")

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            self.assertFalse(
                os.path.exists(os.path.join(tmp, "_kernel", "_generated"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(tmp, "_kernel", "key.md"))
            )
            self.assertIn("Dropped _kernel/_generated/", result["actions"])
            self.assertEqual(result["errors"], [])


class TestFlattenBundles(unittest.TestCase):
    def test_migrate_flattens_bundles(self):
        """Step 2: ``bundles/{a,b}`` become ``{a,b}`` at staging root."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            _make_v2_bundle(tmp, "alpha")
            _make_v2_bundle(tmp, "beta")

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            self.assertFalse(os.path.isdir(os.path.join(tmp, "bundles")))
            self.assertTrue(
                os.path.isdir(os.path.join(tmp, "alpha"))
            )
            self.assertTrue(
                os.path.isdir(os.path.join(tmp, "beta"))
            )
            self.assertTrue(
                os.path.isfile(
                    os.path.join(tmp, "alpha", "context.manifest.yaml")
                )
            )
            self.assertTrue(
                os.path.isfile(
                    os.path.join(tmp, "beta", "context.manifest.yaml")
                )
            )
            self.assertEqual(
                sorted(result["bundles_migrated"]), ["alpha", "beta"]
            )
            self.assertIn("Flattened bundles/alpha -> alpha", result["actions"])
            self.assertIn("Flattened bundles/beta -> beta", result["actions"])

    def test_migrate_handles_collision(self):
        """Collision: existing live-context ``alpha/`` forces ``alpha-imported``."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            # Live context dir that collides with the bundle name.
            _make_live_dir(tmp, "alpha", content="live alpha\n")
            _make_v2_bundle(tmp, "alpha")

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            self.assertFalse(os.path.isdir(os.path.join(tmp, "bundles")))
            # Original live alpha/ is intact.
            self.assertTrue(
                os.path.isfile(os.path.join(tmp, "alpha", "README.md"))
            )
            # Flattened bundle went to alpha-imported/.
            self.assertTrue(
                os.path.isdir(os.path.join(tmp, "alpha-imported"))
            )
            self.assertTrue(
                os.path.isfile(
                    os.path.join(
                        tmp, "alpha-imported", "context.manifest.yaml"
                    )
                )
            )
            self.assertEqual(
                result["bundles_migrated"], ["alpha-imported"]
            )
            # Action log flags the suffix.
            suffix_logged = any(
                "collision suffix" in a for a in result["actions"]
            )
            self.assertTrue(suffix_logged)


class TestConvertTasks(unittest.TestCase):
    def test_migrate_converts_tasks_md(self):
        """Step 3: ``tasks.md`` checkbox list becomes ``tasks.json`` entries."""
        tasks_md = (
            "## Active\n"
            "- [ ] foo\n"
            "- [~] bar @alice\n"
            "- [x] baz\n"
            "\n"
            "not a task line\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            _make_v2_bundle(tmp, "alpha", tasks_md=tasks_md)

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            json_path = os.path.join(tmp, "alpha", "tasks.json")
            md_path = os.path.join(tmp, "alpha", "tasks.md")
            self.assertTrue(os.path.isfile(json_path))
            self.assertFalse(os.path.exists(md_path))

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tasks = data["tasks"]
            self.assertEqual(len(tasks), 3)

            # Task 1: unchecked -> active normal
            self.assertEqual(tasks[0]["title"], "foo")
            self.assertEqual(tasks[0]["status"], "active")
            self.assertEqual(tasks[0]["priority"], "normal")
            self.assertEqual(tasks[0]["session"], FIXED_SESSION)
            self.assertEqual(tasks[0]["created"], FIXED_TS)
            self.assertEqual(tasks[0]["bundle"], "alpha")

            # Task 2: ~ with session attribution -> active high, @alice
            self.assertEqual(tasks[1]["title"], "bar")
            self.assertEqual(tasks[1]["status"], "active")
            self.assertEqual(tasks[1]["priority"], "high")
            self.assertEqual(tasks[1]["session"], "alice")

            # Task 3: x -> done normal
            self.assertEqual(tasks[2]["title"], "baz")
            self.assertEqual(tasks[2]["status"], "done")

            self.assertEqual(result["tasks_converted"], 3)
            converted_logged = any(
                "Converted alpha/tasks.md" in a for a in result["actions"]
            )
            self.assertTrue(converted_logged)

    def test_migrate_tasks_md_with_frontmatter(self):
        """Frontmatter is stripped before parsing checkbox lines."""
        tasks_md = (
            "---\n"
            "bundle: alpha\n"
            "- [ ] frontmatter line that looks like a task\n"
            "---\n"
            "- [ ] real task\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            _make_v2_bundle(tmp, "alpha", tasks_md=tasks_md)

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            json_path = os.path.join(tmp, "alpha", "tasks.json")
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["tasks"]), 1)
            self.assertEqual(data["tasks"][0]["title"], "real task")
            self.assertEqual(result["tasks_converted"], 1)

    def test_migrate_tasks_md_empty(self):
        """Bundle with empty tasks.md -> tasks.json with empty tasks list."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            _make_v2_bundle(tmp, "alpha", tasks_md="## Active\n\nno tasks yet\n")

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            json_path = os.path.join(tmp, "alpha", "tasks.json")
            self.assertTrue(os.path.isfile(json_path))
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["tasks"], [])
            self.assertEqual(result["tasks_converted"], 0)

    def test_migrate_tasks_json_already_present(self):
        """If both tasks.md and tasks.json exist -> warn, keep json, no convert."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            _make_v2_bundle(tmp, "alpha", tasks_md="- [ ] new md task\n")
            _write(
                os.path.join(tmp, "bundles", "alpha", "tasks.json"),
                '{"tasks": [{"id": "t-001", "title": "existing"}]}\n',
            )

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            # tasks.md is left in place (as documented); tasks.json preserved.
            self.assertTrue(
                os.path.isfile(os.path.join(tmp, "alpha", "tasks.json"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(tmp, "alpha", "tasks.md"))
            )
            with open(
                os.path.join(tmp, "alpha", "tasks.json"), "r", encoding="utf-8"
            ) as f:
                data = json.load(f)
            self.assertEqual(data["tasks"][0]["title"], "existing")
            self.assertEqual(result["tasks_converted"], 0)
            warn_logged = any(
                "both tasks.md and tasks.json" in w for w in result["warnings"]
            )
            self.assertTrue(warn_logged)


class TestIdempotency(unittest.TestCase):
    def test_migrate_idempotent(self):
        """Second run on already-migrated staging is a no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=True)
            _make_v2_bundle(tmp, "alpha", tasks_md="- [ ] one\n")
            _make_v2_bundle(tmp, "beta")

            with _patched():
                first = ap2p.migrate_v2_layout(tmp)
                first_listing = _listing(tmp)
                second = ap2p.migrate_v2_layout(tmp)
                second_listing = _listing(tmp)

            self.assertEqual(first_listing, second_listing)
            self.assertEqual(second["actions"], ["no-op (already v3 layout)"])
            self.assertEqual(second["bundles_migrated"], [])
            self.assertEqual(second["tasks_converted"], 0)
            self.assertEqual(second["warnings"], [])
            self.assertEqual(second["errors"], [])
            # First run did real work.
            self.assertGreaterEqual(len(first["actions"]), 2)

    def test_migrate_no_op_on_v3_staging(self):
        """Pure v3 staging (no bundles/, no _generated/) returns no-op early."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            # v3-flat bundles
            _write(
                os.path.join(tmp, "shielding-review", "context.manifest.yaml"),
                "goal: x\nstatus: active\n",
            )
            _write(
                os.path.join(tmp, "shielding-review", "draft-01.md"),
                "# shielding\n",
            )

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            self.assertEqual(result["actions"], ["no-op (already v3 layout)"])
            self.assertEqual(result["bundles_migrated"], [])
            self.assertEqual(result["tasks_converted"], 0)
            self.assertTrue(os.path.isdir(os.path.join(tmp, "shielding-review")))

    def test_migrate_empty_bundles_container_treated_as_v3(self):
        """``bundles/`` existing but empty counts as already-v3."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            os.makedirs(os.path.join(tmp, "bundles"))

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            self.assertEqual(result["actions"], ["no-op (already v3 layout)"])


class TestPreservation(unittest.TestCase):
    def test_migrate_preserves_kernel_history(self):
        """``_kernel/history/`` is a valid overflow dir -- never touched."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=True, with_history=True)
            _make_v2_bundle(tmp, "alpha")

            with _patched():
                ap2p.migrate_v2_layout(tmp)

            chapter = os.path.join(
                tmp, "_kernel", "history", "chapter-01.md"
            )
            self.assertTrue(os.path.isfile(chapter))
            with open(chapter, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("Chapter 01", content)

    def test_migrate_preserves_raw_and_drafts(self):
        """Bundle contents (raw/, draft files, observations.md) survive intact."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            _make_v2_bundle(
                tmp, "alpha", tasks_md="- [ ] do it\n",
            )

            with _patched():
                ap2p.migrate_v2_layout(tmp)

            # Raw source survived.
            raw_file = os.path.join(
                tmp, "alpha", "raw", "2026-03-01-note.md"
            )
            self.assertTrue(os.path.isfile(raw_file))
            with open(raw_file, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "raw source content\n")

            # Draft file survived.
            draft = os.path.join(tmp, "alpha", "alpha-draft-01.md")
            self.assertTrue(os.path.isfile(draft))

            # observations.md survived.
            obs = os.path.join(tmp, "alpha", "observations.md")
            self.assertTrue(os.path.isfile(obs))

            # context.manifest.yaml survived.
            manifest = os.path.join(tmp, "alpha", "context.manifest.yaml")
            self.assertTrue(os.path.isfile(manifest))


class TestReturnDictAccuracy(unittest.TestCase):
    def test_migrate_returns_accurate_counts(self):
        """bundles_migrated + tasks_converted reflect actual work done."""
        tasks_md_a = "- [ ] a1\n- [~] a2\n- [x] a3\n"
        tasks_md_b = "- [ ] b1\n- [ ] b2\n"
        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=True)
            _make_v2_bundle(tmp, "alpha", tasks_md=tasks_md_a)
            _make_v2_bundle(tmp, "beta", tasks_md=tasks_md_b)
            _make_v2_bundle(tmp, "gamma")  # no tasks.md

            with _patched():
                result = ap2p.migrate_v2_layout(tmp)

            self.assertEqual(
                sorted(result["bundles_migrated"]),
                ["alpha", "beta", "gamma"],
            )
            # 3 from alpha + 2 from beta + 0 from gamma = 5
            self.assertEqual(result["tasks_converted"], 5)
            self.assertEqual(result["errors"], [])

    def test_migrate_missing_staging_reports_error(self):
        """Invalid staging path -> error entry, no crash."""
        with tempfile.TemporaryDirectory() as tmp:
            nope = os.path.join(tmp, "does-not-exist")
            result = ap2p.migrate_v2_layout(nope)
            self.assertTrue(any("does not exist" in e for e in result["errors"]))
            self.assertEqual(result["bundles_migrated"], [])


class TestCliMigrate(unittest.TestCase):
    def test_cli_migrate_json_output(self):
        """``alive-p2p.py migrate --staging <dir> --json`` emits a JSON result."""
        import io

        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=True)
            _make_v2_bundle(tmp, "alpha", tasks_md="- [ ] one\n")

            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                with _patched():
                    with self.assertRaises(SystemExit) as cm:
                        ap2p._cli(
                            ["migrate", "--staging", tmp, "--json"]
                        )
            self.assertEqual(cm.exception.code, 0)
            output = buf.getvalue()
            data = json.loads(output)
            self.assertIn("bundles_migrated", data)
            self.assertEqual(data["bundles_migrated"], ["alpha"])
            self.assertEqual(data["tasks_converted"], 1)

    def test_cli_migrate_human_output(self):
        """Default CLI output is human-readable, not JSON."""
        import io

        with tempfile.TemporaryDirectory() as tmp:
            _make_v2_kernel(tmp, with_generated=False)
            _make_v2_bundle(tmp, "alpha")

            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                with _patched():
                    with self.assertRaises(SystemExit) as cm:
                        ap2p._cli(["migrate", "--staging", tmp])
            self.assertEqual(cm.exception.code, 0)
            output = buf.getvalue()
            self.assertIn("migrate_v2_layout result:", output)
            self.assertIn("bundles_migrated: alpha", output)

    def test_cli_migrate_invalid_staging(self):
        """Missing staging dir exits nonzero and writes to stderr."""
        import io

        buf_err = io.StringIO()
        with mock.patch.object(sys, "stderr", buf_err):
            with self.assertRaises(SystemExit) as cm:
                ap2p._cli(["migrate", "--staging", "/nonexistent/xyz"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("does not exist", buf_err.getvalue())


if __name__ == "__main__":
    unittest.main()
