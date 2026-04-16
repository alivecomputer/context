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


FIXTURE_ROOT = pathlib.Path(__file__).parent / "fixtures" / "tiny"
FIXTURE_WALNUT = FIXTURE_ROOT / "demo-walnut"

# Path to the src/ directory so subprocesses can import alive_mcp without
# needing the package installed in a venv.
SRC_ROOT = pathlib.Path(__file__).parent.parent / "src"

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


if __name__ == "__main__":
    unittest.main()
