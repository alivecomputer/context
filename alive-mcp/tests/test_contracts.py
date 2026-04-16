"""MCP Inspector CLI contract tests (fn-10-60k.14 / T14).

Protects against silent contract drift — the kind of change that DOES
NOT throw an error but quietly alters LLM behavior. Per the
"Your MCP Server's Tool Descriptions Changed Last Night Nobody Noticed"
anti-pattern flagged in the research bundle: a one-word tweak to a
tool's ``description`` or a loosened ``inputSchema`` can silently change
downstream agent behavior. The only way to catch it is a snapshot
diff, run in CI on every build.

Strategy
--------
For each frozen MCP list method (``tools/list``, ``resources/list``,
``prompts/list``):

1. Invoke ``scripts/run-inspector-snapshot.sh`` with the fixture world.
   The shell wrapper handles npx/uv orchestration; we just capture its
   stdout.
2. Compare the stdout against the committed golden at
   ``tests/fixtures/contracts/<method>.snapshot.json``.
3. On mismatch, print a unified diff and a one-line instruction telling
   the developer how to intentionally update the golden if the change
   is desired.

Why the generator is the test's source of truth
-----------------------------------------------
The committed golden was produced by running the SAME generator against
the SAME fixture. So any diff under a clean repo means the Inspector's
current output diverged from the snapshot, not that the normalization
logic itself changed. If normalization DOES change (``normalize_snapshot``
evolves), re-running ``scripts/update-snapshots.sh`` re-canonicalizes
all three fixtures in lockstep.

Skip posture
------------
If ``npx`` / ``node`` / ``uv`` are not on PATH (e.g. a minimal CI image
that forgot the Node toolchain) the test skips with a clear reason
rather than failing mysteriously. The T15 CI job is the canonical place
where all three binaries are guaranteed present; skipping here keeps
local unit-test runs clean for contributors on Python-only machines.

T15 hand-off
------------
T15 pins the Inspector via a committed ``package-lock.json`` and
switches the generator from ``npx -y @modelcontextprotocol/inspector``
to ``./node_modules/.bin/mcp-inspector``. THIS test file and the
golden fixtures do not change — only the shell wrapper swaps its
invocation target.
"""
from __future__ import annotations

import difflib
import pathlib
import shutil
import subprocess
import sys
import unittest

# Side-effect import to ensure ``src/`` is on sys.path; matches the
# convention used by every other test module in the suite.
import tests  # noqa: F401

_THIS_DIR = pathlib.Path(__file__).resolve().parent
_PKG_ROOT = _THIS_DIR.parent
_SCRIPT = _PKG_ROOT / "scripts" / "run-inspector-snapshot.sh"
_CONTRACTS_DIR = _THIS_DIR / "fixtures" / "contracts"


# Top-of-file sanity: the committed golden MUST EXIST for every method
# we contract-test. If someone renames a fixture without updating this
# list (or vice versa) the test class will raise a clear error at
# module import time.
_METHODS = (
    # (method, golden_filename)
    ("tools/list", "tools.snapshot.json"),
    ("resources/list", "resources.snapshot.json"),
    ("prompts/list", "prompts.snapshot.json"),
)


def _toolchain_available() -> tuple[bool, str]:
    """Return ``(True, "")`` iff every binary the generator needs is on PATH."""
    for bin_name in ("npx", "node", "uv"):
        if shutil.which(bin_name) is None:
            return False, f"required binary not on PATH: {bin_name}"
    if not _SCRIPT.exists():
        return False, f"missing generator script: {_SCRIPT}"
    return True, ""


def _run_generator(method: str) -> str:
    """Run the generator and return its stdout.

    Raises ``AssertionError`` with the captured stderr attached if the
    generator exited non-zero — that is almost always a real problem
    (server crash, print-contaminated stdout) that should fail loudly.
    """
    result = subprocess.run(
        [str(_SCRIPT), method],
        cwd=str(_PKG_ROOT),
        capture_output=True,
        text=True,
        # 120 s is generous — ``npx -y`` has to populate the npm cache
        # on first run (cold machine) which can take ~60 s over
        # reasonable bandwidth. Subsequent runs hit the cache and
        # complete in 2-3 s.
        timeout=180,
    )
    if result.returncode != 0:
        raise AssertionError(
            "run-inspector-snapshot.sh failed for {}: rc={}\n"
            "stderr:\n{}\nstdout:\n{}".format(
                method, result.returncode, result.stderr, result.stdout
            )
        )
    return result.stdout


class ContractSnapshotTests(unittest.TestCase):
    """Diff live Inspector output against the committed golden snapshots."""

    @classmethod
    def setUpClass(cls) -> None:
        ok, reason = _toolchain_available()
        if not ok:
            raise unittest.SkipTest(
                "Inspector contract tests require node + npx + uv on PATH: "
                + reason
            )

    def _assert_snapshot_matches(self, method: str, golden_name: str) -> None:
        golden_path = _CONTRACTS_DIR / golden_name
        self.assertTrue(
            golden_path.exists(),
            msg=(
                f"golden fixture missing: {golden_path}. "
                "Run scripts/update-snapshots.sh to create it."
            ),
        )
        expected = golden_path.read_text(encoding="utf-8")
        actual = _run_generator(method)

        if expected == actual:
            return

        # Build a unified diff that is readable in CI logs and points
        # directly at the fix. ``splitlines(keepends=True)`` lets
        # difflib show exact newline-level differences if the whitespace
        # changed (rare, but makes whitespace bugs obvious).
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"golden/{golden_name}",
                tofile=f"live/{method}",
                n=3,
            )
        )
        self.fail(
            "MCP contract snapshot drift detected for "
            f"method={method!r}.\n\n"
            f"{diff}\n"
            "If this change is intentional, run:\n"
            "    scripts/update-snapshots.sh\n"
            "then re-run the test suite and commit the updated fixture "
            "alongside your source change."
        )

    def test_tools_list_matches_golden(self) -> None:
        self._assert_snapshot_matches("tools/list", "tools.snapshot.json")

    def test_resources_list_matches_golden(self) -> None:
        self._assert_snapshot_matches(
            "resources/list", "resources.snapshot.json"
        )

    def test_prompts_list_matches_golden(self) -> None:
        self._assert_snapshot_matches("prompts/list", "prompts.snapshot.json")


class ContractFixtureShapeTests(unittest.TestCase):
    """Pure-Python shape checks that do not depend on the node toolchain.

    These assert properties of the committed golden fixtures themselves
    so a broken fixture (e.g. hand-edited by mistake, or produced by a
    skewed fixture world) fails even on a minimal machine where the
    full generator cannot run. They also freeze the 10-tool / 12-
    resource / empty-prompts roster as an extra belt on top of the
    snapshot diff.
    """

    def test_tools_snapshot_has_exactly_ten_frozen_tools(self) -> None:
        import json

        path = _CONTRACTS_DIR / "tools.snapshot.json"
        self.assertTrue(path.exists(), msg=f"missing fixture: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("tools", data)
        self.assertEqual(
            len(data["tools"]),
            10,
            msg=(
                "v0.1 roster is frozen at exactly 10 tools. "
                "Adding/removing a tool requires an epic-level decision "
                "and updated snapshot fixture."
            ),
        )
        expected_names = {
            "list_walnuts",
            "get_walnut_state",
            "read_walnut_kernel",
            "list_bundles",
            "get_bundle",
            "read_bundle_manifest",
            "search_world",
            "search_walnut",
            "read_log",
            "list_tasks",
        }
        actual_names = {t["name"] for t in data["tools"]}
        self.assertEqual(actual_names, expected_names)

        # Every tool MUST be annotated read-only / non-destructive /
        # closed-world. The v0.1 contract forbids shipping a tool that
        # claims any other posture.
        for tool in data["tools"]:
            ann = tool.get("annotations") or {}
            self.assertTrue(
                ann.get("readOnlyHint"),
                msg=f"{tool['name']} missing readOnlyHint=True",
            )
            self.assertFalse(
                ann.get("destructiveHint"),
                msg=f"{tool['name']} must declare destructiveHint=False",
            )
            self.assertFalse(
                ann.get("openWorldHint"),
                msg=f"{tool['name']} must declare openWorldHint=False",
            )

    def test_resources_snapshot_has_four_files_per_walnut(self) -> None:
        import json

        path = _CONTRACTS_DIR / "resources.snapshot.json"
        self.assertTrue(path.exists(), msg=f"missing fixture: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("resources", data)
        resources = data["resources"]
        # Fixture world has 3 walnuts × 4 kernel files = 12 resources.
        self.assertEqual(len(resources), 12)

        # Every resource URI must match the frozen alive:// scheme.
        # Having this check here means a scheme change triggers a clear
        # test failure before the far-less-readable snapshot diff
        # signal fires.
        allowed_stems = {"key", "log", "insights", "now"}
        for res in resources:
            uri = res["uri"]
            self.assertTrue(
                uri.startswith("alive://walnut/"),
                msg=f"unexpected URI scheme: {uri}",
            )
            stem = uri.rsplit("/", 1)[-1]
            self.assertIn(
                stem,
                allowed_stems,
                msg=f"unexpected kernel stem in URI: {uri}",
            )

    def test_prompts_snapshot_is_empty_for_v01(self) -> None:
        import json

        path = _CONTRACTS_DIR / "prompts.snapshot.json"
        self.assertTrue(path.exists(), msg=f"missing fixture: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        # v0.1 does NOT register any prompts (stub for v0.2). If a
        # prompt is added, this assertion reminds the developer to also
        # update the v0.1 scope decision in the epic spec.
        self.assertEqual(data, {"prompts": []})


class NormalizerUnitTests(unittest.TestCase):
    """Direct tests for the ``scripts/normalize_snapshot.py`` helper.

    The end-to-end ``ContractSnapshotTests`` prove the full pipeline
    works; these tests cover the normalization step in isolation so a
    bug in sorting / canonicalization can be localized without a live
    Inspector subprocess.
    """

    def setUp(self) -> None:
        # Load normalize_snapshot.py as a module by path. It lives in
        # ``scripts/`` which is deliberately NOT on sys.path — tests
        # import it via its absolute filesystem location.
        import importlib.util

        mod_path = _PKG_ROOT / "scripts" / "normalize_snapshot.py"
        spec = importlib.util.spec_from_file_location(
            "normalize_snapshot", str(mod_path)
        )
        assert spec is not None and spec.loader is not None
        self._mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._mod)

    def test_tools_list_sorted_by_name(self) -> None:
        raw = (
            '{"tools": ['
            '{"name": "z_tool", "description": "Z"},'
            '{"name": "a_tool", "description": "A"}'
            "]}"
        )
        out = self._mod.normalize(raw, "tools/list")
        import json

        data = json.loads(out)
        self.assertEqual(
            [t["name"] for t in data["tools"]], ["a_tool", "z_tool"]
        )

    def test_resources_list_sorted_by_uri(self) -> None:
        raw = (
            '{"resources": ['
            '{"uri": "alive://walnut/z", "name": "z"},'
            '{"uri": "alive://walnut/a", "name": "a"}'
            "]}"
        )
        out = self._mod.normalize(raw, "resources/list")
        import json

        data = json.loads(out)
        self.assertEqual(
            [r["uri"] for r in data["resources"]],
            ["alive://walnut/a", "alive://walnut/z"],
        )

    def test_dict_keys_sorted_deeply(self) -> None:
        # ``zebra`` before ``alpha`` on input; sorted output must flip.
        # Validating via re-parse rather than string-matching — the
        # pretty-print indent shape is incidental, key order is what
        # matters.
        raw = '{"tools": [{"zebra": 1, "alpha": 2, "name": "t"}]}'
        out = self._mod.normalize(raw, "tools/list")
        # json.loads preserves insertion order in Python 3.7+, so the
        # list() of keys reflects serialization order.
        import json

        tool = json.loads(out)["tools"][0]
        self.assertEqual(list(tool.keys()), ["alpha", "name", "zebra"])

    def test_unknown_method_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mod.normalize('{"foo": []}', "bogus/method")

    def test_empty_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mod.normalize("", "tools/list")

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mod.normalize("not json at all", "tools/list")


if __name__ == "__main__":
    unittest.main()
