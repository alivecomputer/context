"""Smoke tests for the vendored ALIVE kernel utilities (fn-10-60k.2).

Covers the five T2 acceptance criteria:

1. Each vendored module imports cleanly with ZERO stdout output (verified
   in a subprocess per module).
2. ``project_pure.py`` and ``tasks_pure.py`` source contain no ``print(`` or
   ``sys.exit(`` calls (grep-equivalent scan of the extracted helpers).
3. Typed error classes exist and are raised by the right helpers.
4. ``walnut_paths.find_bundles()``, ``project_pure.find_world_root()``,
   ``tasks_pure._collect_all_tasks()`` succeed against the tiny fixture at
   ``tests/fixtures/tiny/``.
5. No external deps: the combined vendor import works in a subprocess with
   only stdlib + the `alive_mcp` package itself on ``sys.path``.

The subprocess check is the critical one -- it's the definitive proof that
importing the vendor package inside a stdio JSON-RPC server won't corrupt
framing.
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
import unittest

# Ensure ``python3 -m unittest discover tests`` works without requiring the
# package to be installed or ``PYTHONPATH=src`` to be set. When unittest
# discovers test modules it loads them by filename rather than as part of
# the ``tests`` package, so ``tests/__init__.py`` alone isn't enough --
# we need the path prepended at test-module-load time. Idempotent.
_SRC_DIR = str(
    pathlib.Path(__file__).resolve().parent.parent / "src"
)
if os.path.isdir(_SRC_DIR) and _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


FIXTURE_ROOT = pathlib.Path(__file__).parent / "fixtures" / "tiny"
FIXTURE_WALNUT = FIXTURE_ROOT / "demo-walnut"

# Path to the src/ directory so subprocesses can import alive_mcp without
# needing the package installed in a venv.
SRC_ROOT = pathlib.Path(__file__).parent.parent / "src"

# Upstream source for the direct-copy file. Discovered by walking UPWARD
# from this test file, checking each ancestor for the sentinel
# ``plugins/alive/scripts/walnut_paths.py``. Robust to the several layouts
# this repo ships in:
#   - monorepo main checkout:        <repo>/claude-code/plugins/...
#   - worktree of alive-mcp-v0.1:    <repo>/claude-code/.worktrees/alive-mcp-v0.1/plugins/...
#     (upstream lives at <repo>/claude-code/plugins/... -- one more parent up)
#   - future layouts:                anywhere above, as long as the sentinel exists
# When the sentinel isn't found within 8 ancestors, the byte-identity test
# skips -- tarball installs and tests-in-isolation legitimately don't have
# the plugin tree alongside.
# Two candidate suffixes per ancestor -- the monorepo uses the
# ``claude-code/`` prefix in its canonical path (per VENDORING.md), but
# future repo reshufflings or flattened vendor tests might land alongside
# a bare ``plugins/...`` tree. Trying both keeps the test robust.
_SENTINEL_SUFFIXES = (
    pathlib.Path("claude-code") / "plugins" / "alive" / "scripts" / "walnut_paths.py",
    pathlib.Path("plugins") / "alive" / "scripts" / "walnut_paths.py",
)


def _find_upstream_walnut_paths() -> pathlib.Path | None:
    here = pathlib.Path(__file__).resolve()
    # Walk ``here`` plus up to 8 ancestors, trying each sentinel suffix.
    # 8 is enough for any plausible monorepo layout (main checkout, a
    # worktree one level deep, a worktree of a worktree) without being
    # so deep it hits filesystem root on short paths.
    for ancestor in [here, *here.parents][:9]:
        for suffix in _SENTINEL_SUFFIXES:
            candidate = ancestor / suffix
            if candidate.is_file():
                return candidate
    return None


UPSTREAM_WALNUT_PATHS: pathlib.Path | None = _find_upstream_walnut_paths()

# Source files we extracted into -- these must be scrubbed of print / exit.
EXTRACTED_SOURCES = [
    SRC_ROOT / "alive_mcp" / "_vendor" / "_pure" / "project_pure.py",
    SRC_ROOT / "alive_mcp" / "_vendor" / "_pure" / "tasks_pure.py",
]

# All vendor modules (direct + extracted). These must import with zero
# stdout output in a fresh Python interpreter.
VENDOR_IMPORT_TARGETS = [
    "alive_mcp._vendor",
    "alive_mcp._vendor.walnut_paths",
    "alive_mcp._vendor._pure",
    "alive_mcp._vendor._pure.project_pure",
    "alive_mcp._vendor._pure.tasks_pure",
]


def _run_import_subprocess(module: str) -> subprocess.CompletedProcess:
    """Import ``module`` in a fresh subprocess, capturing stdout and stderr."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(SRC_ROOT) + (os.pathsep + existing if existing else "")
    )
    # -S skips site-packages auto-import (keeps the test hermetic against
    # whatever the contributor's user site-packages might inject on import).
    # -I would be stricter but also ignores PYTHONPATH, so we use -S only.
    return subprocess.run(
        [sys.executable, "-S", "-c", "import {}".format(module)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class VendorImportIsSilent(unittest.TestCase):
    """Each vendor module must import with zero stdout output.

    Stdout on import would corrupt MCP JSON-RPC framing once T5 wires the
    stdio server -- catching it at T2 keeps us honest from the start.
    """

    def test_each_module_imports_silently(self) -> None:
        for module in VENDOR_IMPORT_TARGETS:
            with self.subTest(module=module):
                result = _run_import_subprocess(module)
                self.assertEqual(
                    result.returncode, 0,
                    msg="import {} failed: stderr={!r}".format(
                        module, result.stderr
                    ),
                )
                self.assertEqual(
                    result.stdout, "",
                    msg="import {} wrote to stdout: {!r}".format(
                        module, result.stdout
                    ),
                )
                self.assertEqual(
                    result.stderr, "",
                    msg="import {} wrote to stderr: {!r}".format(
                        module, result.stderr
                    ),
                )

    def test_combined_import_is_silent(self) -> None:
        """Importing all vendor modules at once must still be silent.

        This is the shape the MCP server will actually use: one interpreter,
        many imports. A side effect in any one module would corrupt framing
        for every tool call.
        """
        joined = "; ".join("import {}".format(m) for m in VENDOR_IMPORT_TARGETS)
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(SRC_ROOT) + (os.pathsep + existing if existing else "")
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", joined],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0,
                         msg="combined import failed: {!r}".format(result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")


class DirectCopyIsByteIdentical(unittest.TestCase):
    """The direct-copy file must be byte-identical to upstream.

    Guards the vendoring contract stated in ``VENDORING.md``: refreshes are
    done by re-copying the upstream file verbatim. Any edit local to
    ``_vendor/walnut_paths.py`` that doesn't go through a refresh will
    surface as a diff here.

    Skipped when the upstream source isn't on disk (tarball install, CI
    runner without the plugin tree alongside). The test's intent is to
    catch drift in the contributor workflow, not to block distribution.
    """

    def test_walnut_paths_is_byte_for_byte_identical_to_upstream(self) -> None:
        if UPSTREAM_WALNUT_PATHS is None:
            self.skipTest(
                "upstream plugins/alive/scripts/walnut_paths.py not found "
                "next to alive-mcp -- skipping byte-identity check "
                "(intended for contributor runs, not tarball installs)"
            )

        vendored = SRC_ROOT / "alive_mcp" / "_vendor" / "walnut_paths.py"
        self.assertEqual(
            vendored.read_bytes(),
            UPSTREAM_WALNUT_PATHS.read_bytes(),
            msg=(
                "vendored walnut_paths.py diverges from upstream "
                "({}). Direct copies must be byte-identical -- "
                "restore by re-copying upstream and updating VENDORING.md "
                "if upstream has moved."
            ).format(UPSTREAM_WALNUT_PATHS),
        )


class ExtractedSourcesAreLibrarySafe(unittest.TestCase):
    """The two extracted modules must have zero ``print()`` or ``sys.exit()``
    call sites.

    Direct-copy files (``walnut_paths.py``) are exempt because upstream
    audited them -- we don't re-audit every refresh. Extracted files get
    hand-lifted from a CLI so the AST scan catches accidental leftovers.

    We parse each source with ``ast`` rather than grepping the raw text so
    that docstrings, comments, and string literals mentioning
    ``print(...)`` / ``sys.exit(...)`` as prose (which the vendor docs DO,
    to explain what was stripped) don't false-positive the check. Only real
    call sites matter for stdio safety.
    """

    @staticmethod
    def _find_forbidden_calls(path: pathlib.Path) -> list:
        """Return a list of (func_name, lineno) for forbidden call sites."""
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Bare `print(...)`
            if isinstance(func, ast.Name) and func.id == "print":
                hits.append(("print", node.lineno))
                continue
            # `sys.exit(...)` / `os._exit(...)` -- attribute access on
            # `sys` / `os`
            if isinstance(func, ast.Attribute):
                if (
                    isinstance(func.value, ast.Name)
                    and func.value.id == "sys"
                    and func.attr == "exit"
                ):
                    hits.append(("sys.exit", node.lineno))
                if (
                    isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                    and func.attr == "_exit"
                ):
                    hits.append(("os._exit", node.lineno))
        return hits

    def test_no_forbidden_calls_in_extracted_sources(self) -> None:
        for path in EXTRACTED_SOURCES:
            with self.subTest(path=str(path)):
                hits = self._find_forbidden_calls(path)
                self.assertEqual(
                    hits, [],
                    msg=(
                        "{} contains forbidden call sites: {!r}. These "
                        "corrupt stdio JSON-RPC framing or kill the server."
                    ).format(path.name, hits),
                )


class TypedErrorsAreDefined(unittest.TestCase):
    """The three typed errors must be importable from ``_pure``."""

    def test_error_classes_importable(self) -> None:
        # Direct path; if any of these ImportError, the suite fails loudly.
        from alive_mcp._vendor._pure import (
            KernelFileError,
            MalformedYAMLWarning,
            WorldNotFoundError,
        )
        self.assertTrue(issubclass(WorldNotFoundError, Exception))
        self.assertTrue(issubclass(KernelFileError, Exception))
        self.assertTrue(issubclass(MalformedYAMLWarning, Warning))

    def test_find_world_root_raises_on_missing(self) -> None:
        from alive_mcp._vendor._pure import WorldNotFoundError
        from alive_mcp._vendor._pure import project_pure

        # A bare temp dir far from any ``.alive/`` must raise.
        # ``/`` on macOS/Linux doesn't have ``.alive``; use it as the
        # guaranteed-no-World path.
        with self.assertRaises(WorldNotFoundError):
            project_pure.find_world_root("/")


class VendorHelpersWorkAgainstTinyFixture(unittest.TestCase):
    """End-to-end: the three headline helpers return sane data."""

    def test_walnut_paths_find_bundles(self) -> None:
        from alive_mcp._vendor import walnut_paths

        bundles = walnut_paths.find_bundles(str(FIXTURE_WALNUT))
        # Sorted list of (relpath, abspath). Fixture has exactly one bundle.
        self.assertEqual(len(bundles), 1, msg="bundles={!r}".format(bundles))
        relpath, abspath = bundles[0]
        self.assertEqual(relpath, "demo-bundle")
        self.assertTrue(os.path.isdir(abspath))
        self.assertEqual(
            os.path.basename(abspath.rstrip(os.sep)),
            "demo-bundle",
        )

    def test_walnut_paths_scan_bundles_parses_manifest(self) -> None:
        from alive_mcp._vendor import walnut_paths

        scanned = walnut_paths.scan_bundles(str(FIXTURE_WALNUT))
        self.assertIn("demo-bundle", scanned)
        manifest = scanned["demo-bundle"]
        self.assertEqual(manifest.get("status"), "draft")
        self.assertIn(
            "Demo bundle for alive-mcp",
            manifest.get("goal", ""),
        )
        # active_sessions is always present, even when empty.
        self.assertIn("active_sessions", manifest)
        self.assertEqual(manifest["active_sessions"], ["abcdef01"])

    def test_project_pure_find_world_root_walks_up(self) -> None:
        from alive_mcp._vendor._pure import project_pure

        # Starting from deep inside the walnut should still find the
        # fixture's ``.alive/`` at FIXTURE_ROOT.
        deep_start = FIXTURE_WALNUT / "demo-bundle"
        world = project_pure.find_world_root(str(deep_start))
        self.assertEqual(
            os.path.realpath(world),
            os.path.realpath(str(FIXTURE_ROOT)),
        )

    def test_project_pure_parse_log_returns_structured_dict(self) -> None:
        from alive_mcp._vendor._pure import project_pure

        parsed = project_pure.parse_log(str(FIXTURE_WALNUT))
        # Fixture log has one entry with squirrel id abcdef01.
        self.assertEqual(parsed.get("squirrel"), "abcdef01")
        # parse_log extracts phase from both "phase: X" and narrative
        # keywords; fixture includes both "Phase: building" and the word
        # "building", so expect "building".
        self.assertEqual(parsed.get("phase"), "building")
        next_info = parsed.get("next")
        self.assertIsNotNone(next_info)
        self.assertIn("action", next_info)

    def test_tasks_pure_collect_all_tasks(self) -> None:
        from alive_mcp._vendor._pure import tasks_pure

        tasks = tasks_pure._collect_all_tasks(str(FIXTURE_WALNUT))
        # Fixture has two tasks: t001 (unscoped) + t002 (bundle).
        ids = sorted(t.get("id") for t in tasks)
        self.assertEqual(ids, ["t001", "t002"])

    def test_tasks_pure_public_alias_matches(self) -> None:
        from alive_mcp._vendor._pure import tasks_pure

        private = tasks_pure._collect_all_tasks(str(FIXTURE_WALNUT))
        public = tasks_pure.collect_all_tasks(str(FIXTURE_WALNUT))
        self.assertEqual(
            sorted(t["id"] for t in private),
            sorted(t["id"] for t in public),
        )

    def test_tasks_pure_summary_shape(self) -> None:
        from alive_mcp._vendor._pure import tasks_pure

        summary = tasks_pure.summary_from_walnut(str(FIXTURE_WALNUT))
        self.assertIn("bundles", summary)
        self.assertIn("unscoped", summary)
        self.assertIn("active", summary["bundles"])
        self.assertIn("recent", summary["bundles"])
        self.assertIn("summary", summary["bundles"])


class ScanBundlesHonorsNestedWalnutBoundaries(unittest.TestCase):
    """Regression: ``scan_bundles`` must treat nested walnuts as boundaries.

    The upstream ``project.py::scan_bundles`` has a latent dead-code bug --
    its ``"_kernel" in dirs`` check can never fire because ``_kernel`` is
    pruned from ``dirs`` earlier in the loop. That means a parent walnut's
    scan bleeds into a child walnut's bundles. The extracted version in
    ``project_pure`` fixes this by checking ``_kernel/key.md`` on disk.
    """

    def test_nested_walnut_bundles_are_not_harvested_by_parent(self) -> None:
        import tempfile

        from alive_mcp._vendor._pure import project_pure

        with tempfile.TemporaryDirectory() as tmpdir:
            parent = pathlib.Path(tmpdir) / "parent-walnut"
            parent_kernel = parent / "_kernel"
            parent_kernel.mkdir(parents=True)
            (parent_kernel / "key.md").write_text("---\ntype: venture\n---\n")

            # Parent has its own bundle directly.
            parent_bundle = parent / "parent-bundle"
            parent_bundle.mkdir()
            (parent_bundle / "context.manifest.yaml").write_text(
                "goal: parent's bundle\nstatus: draft\n"
            )

            # Nested walnut lives inside the parent, with its own bundle.
            child = parent / "child-walnut"
            child_kernel = child / "_kernel"
            child_kernel.mkdir(parents=True)
            (child_kernel / "key.md").write_text("---\ntype: project\n---\n")
            child_bundle = child / "child-bundle"
            child_bundle.mkdir()
            (child_bundle / "context.manifest.yaml").write_text(
                "goal: child's bundle\nstatus: draft\n"
            )

            result = project_pure.scan_bundles(str(parent))

            # Parent bundle MUST be harvested.
            self.assertIn("parent-bundle", result)
            # Child walnut's bundles MUST NOT bleed in under any path form.
            bad_keys = [
                k for k in result
                if "child-walnut" in k or k.startswith("child-walnut")
            ]
            self.assertEqual(
                bad_keys, [],
                msg="child walnut's bundles leaked into parent scan: {!r}".format(
                    list(result.keys())
                ),
            )


class TasksJsonSchemaIsValidated(unittest.TestCase):
    """Regression: ``_read_tasks_json`` must reject non-list ``tasks``.

    Without the ``isinstance(data["tasks"], list)`` guard, a walnut with
    ``tasks.json`` holding ``{"tasks": "oops"}`` or similar would crash
    the caller's ``list.extend()`` path or silently yield nonsense.
    """

    def test_non_list_tasks_field_is_rejected_with_warning(self) -> None:
        import tempfile
        import warnings as warnings_mod

        from alive_mcp._vendor._pure import MalformedYAMLWarning, tasks_pure

        with tempfile.TemporaryDirectory() as tmpdir:
            walnut = pathlib.Path(tmpdir) / "walnut"
            walnut_kernel = walnut / "_kernel"
            walnut_kernel.mkdir(parents=True)
            (walnut_kernel / "key.md").write_text("---\ntype: experiment\n---\n")

            # Schema drift: ``tasks`` is a string, not a list.
            (walnut_kernel / "tasks.json").write_text('{"tasks": "oops"}')

            with warnings_mod.catch_warnings(record=True) as caught:
                warnings_mod.simplefilter("always")
                collected = tasks_pure._collect_all_tasks(str(walnut))

            self.assertEqual(
                collected, [],
                msg="non-list tasks field leaked into results: {!r}".format(
                    collected
                ),
            )
            self.assertTrue(
                any(
                    issubclass(w.category, MalformedYAMLWarning)
                    and "'tasks' is str" in str(w.message)
                    for w in caught
                ),
                msg=(
                    "expected MalformedYAMLWarning about non-list 'tasks'; "
                    "got {!r}"
                ).format([str(w.message) for w in caught]),
            )


class ScanNestedWalnutsFallsBackOnMalformedV3Projection(unittest.TestCase):
    """Regression: a malformed v3 ``now.json`` must NOT mask a valid v2 fallback.

    Upstream ``project.py::scan_nested_walnuts`` tries v3 then v2; on read
    failure at the first candidate it falls through to the next iteration
    of the loop. The extracted version preserves that -- on exception we
    ``continue``, not ``break``.
    """

    def test_v2_fallback_used_when_v3_is_malformed(self) -> None:
        import tempfile
        import warnings as warnings_mod

        from alive_mcp._vendor._pure import MalformedYAMLWarning, project_pure

        with tempfile.TemporaryDirectory() as tmpdir:
            parent = pathlib.Path(tmpdir) / "parent"
            parent.mkdir()

            child = parent / "child"
            child_kernel = child / "_kernel"
            child_kernel.mkdir(parents=True)
            (child_kernel / "key.md").write_text("---\ntype: project\n---\n")

            # v3 projection is malformed.
            (child_kernel / "now.json").write_text("{ not valid json")

            # v2 projection is valid.
            v2_gen = child_kernel / "_generated"
            v2_gen.mkdir()
            (v2_gen / "now.json").write_text(
                '{"phase": "shipping", "next": "ship it", "updated": "2026-04-16"}'
            )

            with warnings_mod.catch_warnings(record=True) as caught:
                warnings_mod.simplefilter("always")
                children = project_pure.scan_nested_walnuts(str(parent))

            self.assertIn("child", children)
            self.assertEqual(
                children["child"]["phase"], "shipping",
                msg="v2 fallback not used after malformed v3: {!r}".format(
                    children["child"]
                ),
            )
            self.assertEqual(children["child"]["next"], "ship it")
            self.assertEqual(children["child"]["updated"], "2026-04-16")

            # A MalformedYAMLWarning must have been emitted for the v3 path.
            self.assertTrue(
                any(
                    issubclass(w.category, MalformedYAMLWarning)
                    for w in caught
                ),
                msg="no MalformedYAMLWarning emitted for bad v3 now.json",
            )


if __name__ == "__main__":
    unittest.main()
