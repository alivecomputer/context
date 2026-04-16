"""Unit tests for the search tools (fn-10-60k.8 / T8).

Covers the two search tools exposed in :mod:`alive_mcp.tools.search`:

* ``search_world`` -- world-wide substring search with pagination.
* ``search_walnut`` -- walnut-scoped substring search.

Plus the cursor codec, pagination semantics, file inclusion rules,
deterministic ordering, unicode normalization, and the 500KB size cap.

Handlers are exercised directly (not through stdio) so assertions run
against plain envelope dicts. Matches the pattern in
``test_tools_walnut.py`` / ``test_tools_bundle.py``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import shutil
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from typing import Any, List, Optional
from unittest.mock import MagicMock

# Make ``src/`` importable the same way tests/__init__.py does.
import tests  # noqa: F401

from alive_mcp import errors  # noqa: E402
from alive_mcp.tools import search as search_tools  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture world builder.
# ---------------------------------------------------------------------------


@dataclass
class FixtureWorld:
    """Temp ALIVE world + helpers for single-test construction."""

    root: pathlib.Path
    cleanup: Any

    def walnut_path(self, rel: str) -> pathlib.Path:
        return self.root / rel

    def make_walnut(
        self,
        rel: str,
        *,
        key_text: str = "---\ntype: venture\ngoal: test\n---\n\n# walnut\n",
        log_text: Optional[str] = None,
        insights_text: Optional[str] = None,
        now_text: Optional[str] = None,
    ) -> pathlib.Path:
        """Create a walnut with its kernel files populated."""
        walnut_dir = self.walnut_path(rel)
        kernel = walnut_dir / "_kernel"
        kernel.mkdir(parents=True, exist_ok=True)
        (kernel / "key.md").write_text(key_text, encoding="utf-8")
        if log_text is not None:
            (kernel / "log.md").write_text(log_text, encoding="utf-8")
        if insights_text is not None:
            (kernel / "insights.md").write_text(insights_text, encoding="utf-8")
        if now_text is not None:
            (kernel / "now.json").write_text(now_text, encoding="utf-8")
        return walnut_dir

    def make_bundle(
        self,
        walnut_rel: str,
        bundle_rel: str,
        manifest_text: str,
    ) -> pathlib.Path:
        """Create a bundle with the given manifest text."""
        bundle_dir = self.walnut_path(walnut_rel) / bundle_rel
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "context.manifest.yaml").write_text(
            manifest_text, encoding="utf-8"
        )
        return bundle_dir


def _new_world() -> FixtureWorld:
    tmpdir = tempfile.mkdtemp(prefix="alive-mcp-search-test-")
    root = pathlib.Path(tmpdir)
    (root / ".alive").mkdir()
    return FixtureWorld(
        root=root,
        cleanup=lambda: shutil.rmtree(tmpdir, ignore_errors=True),
    )


class _FakeLifespan:
    def __init__(self, world_root: Optional[str]) -> None:
        self.world_root = world_root


class _FakeRequestContext:
    def __init__(self, world_root: Optional[str]) -> None:
        self.lifespan_context = _FakeLifespan(world_root=world_root)


def _fake_ctx(world_root: Optional[str]) -> Any:
    ctx = MagicMock()
    ctx.request_context = _FakeRequestContext(world_root)
    return ctx


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _decode_token(token: str) -> dict:
    """Inspect a cursor token's JSON payload (for test assertions)."""
    padded = token + "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Cursor codec tests.
# ---------------------------------------------------------------------------


class CursorCodecTests(unittest.TestCase):
    def test_none_cursor_is_origin(self) -> None:
        c = search_tools._decode_cursor(None)
        self.assertEqual((c.wi, c.fi, c.lo), (0, 0, 0))

    def test_empty_string_is_origin(self) -> None:
        c = search_tools._decode_cursor("")
        self.assertEqual((c.wi, c.fi, c.lo), (0, 0, 0))

    def test_roundtrip_preserves_state(self) -> None:
        c = search_tools._Cursor(wi=3, fi=2, lo=17)
        token = c.encode()
        decoded = search_tools._decode_cursor(token)
        self.assertEqual(decoded, c)

    def test_encoded_token_is_deterministic(self) -> None:
        c1 = search_tools._Cursor(wi=1, fi=1, lo=1).encode()
        c2 = search_tools._Cursor(wi=1, fi=1, lo=1).encode()
        self.assertEqual(c1, c2)

    def test_encoded_token_carries_version_1(self) -> None:
        token = search_tools._Cursor(wi=0, fi=0, lo=0).encode()
        payload = _decode_token(token)
        self.assertEqual(payload["v"], 1)

    def test_garbage_base64_raises_invalid(self) -> None:
        with self.assertRaises(errors.InvalidCursorError):
            search_tools._decode_cursor("!!!not-base64!!!")

    def test_non_json_bytes_raise_invalid(self) -> None:
        token = (
            base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode("ascii")
        )
        with self.assertRaises(errors.InvalidCursorError):
            search_tools._decode_cursor(token)

    def test_missing_version_raises_invalid(self) -> None:
        raw = json.dumps({"wi": 0, "fi": 0, "lo": 0}).encode("utf-8")
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        with self.assertRaises(errors.InvalidCursorError):
            search_tools._decode_cursor(token)

    def test_wrong_version_raises_invalid(self) -> None:
        raw = json.dumps({"v": 99, "wi": 0, "fi": 0, "lo": 0}).encode("utf-8")
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        with self.assertRaises(errors.InvalidCursorError):
            search_tools._decode_cursor(token)

    def test_negative_index_raises_invalid(self) -> None:
        raw = json.dumps({"v": 1, "wi": -1, "fi": 0, "lo": 0}).encode("utf-8")
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        with self.assertRaises(errors.InvalidCursorError):
            search_tools._decode_cursor(token)

    def test_non_integer_index_raises_invalid(self) -> None:
        raw = json.dumps({"v": 1, "wi": "a", "fi": 0, "lo": 0}).encode("utf-8")
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        with self.assertRaises(errors.InvalidCursorError):
            search_tools._decode_cursor(token)

    def test_bool_is_rejected_as_integer(self) -> None:
        # bool is a subclass of int in Python; the decoder must reject
        # it specifically so ``{"wi": true}`` doesn't decode to wi=1.
        raw = json.dumps({"v": 1, "wi": True, "fi": 0, "lo": 0}).encode("utf-8")
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        with self.assertRaises(errors.InvalidCursorError):
            search_tools._decode_cursor(token)

    def test_non_object_payload_raises_invalid(self) -> None:
        raw = json.dumps([1, 2, 3]).encode("utf-8")
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        with self.assertRaises(errors.InvalidCursorError):
            search_tools._decode_cursor(token)


# ---------------------------------------------------------------------------
# Query normalization tests.
# ---------------------------------------------------------------------------


class QueryNormalizationTests(unittest.TestCase):
    def test_case_insensitive_casefolds(self) -> None:
        self.assertEqual(
            search_tools._normalize_query("ABC", case_sensitive=False),
            "abc",
        )

    def test_case_sensitive_preserves_case(self) -> None:
        self.assertEqual(
            search_tools._normalize_query("ABC", case_sensitive=True),
            "ABC",
        )

    def test_nfc_folds_decomposed_to_composed(self) -> None:
        # "e" + U+0301 (combining acute) -> composed U+00E9. NFKC
        # preserves this canonical-composition equivalence (NFKC is
        # strictly stronger than NFC, so all NFC-equivalent pairs
        # remain equivalent under NFKC).
        decomposed = "cafe\u0301"
        composed = "caf\u00e9"
        self.assertEqual(
            search_tools._normalize_query(decomposed, case_sensitive=False),
            search_tools._normalize_query(composed, case_sensitive=False),
        )

    def test_nfkc_folds_compat_equivalents(self) -> None:
        # NFKC-only equivalences (not caught by NFC):
        #   - ligature ``\ufb00`` -> "ff"
        #   - full-width Latin "Ａ" (U+FF21) -> "A"
        # Both should normalize to the same canonical form as their
        # ASCII counterpart after NFKC + casefold.
        self.assertEqual(
            search_tools._normalize_query("\ufb00", case_sensitive=False),
            search_tools._normalize_query("ff", case_sensitive=False),
        )
        self.assertEqual(
            search_tools._normalize_query("\uff21", case_sensitive=False),
            search_tools._normalize_query("a", case_sensitive=False),
        )


# ---------------------------------------------------------------------------
# Context slice tests.
# ---------------------------------------------------------------------------


class ContextSliceTests(unittest.TestCase):
    def test_middle_of_file_returns_two_each(self) -> None:
        lines = [f"l{i}" for i in range(10)]
        before, after = search_tools._context_slice(lines, 5)
        self.assertEqual(before, ["l3", "l4"])
        self.assertEqual(after, ["l6", "l7"])

    def test_start_of_file_before_is_short(self) -> None:
        lines = [f"l{i}" for i in range(10)]
        before, after = search_tools._context_slice(lines, 0)
        self.assertEqual(before, [])
        self.assertEqual(after, ["l1", "l2"])

    def test_end_of_file_after_is_short(self) -> None:
        lines = [f"l{i}" for i in range(10)]
        before, after = search_tools._context_slice(lines, 9)
        self.assertEqual(before, ["l7", "l8"])
        self.assertEqual(after, [])

    def test_context_lines_also_truncated(self) -> None:
        long = "x" * 400
        lines = ["a", long, "match", long, "b"]
        before, after = search_tools._context_slice(lines, 2)
        # Middle line is "match"; before[1] should be the long line
        # capped at 200 chars.
        self.assertEqual(len(before[1]), search_tools._CONTENT_MAX_CHARS)
        self.assertEqual(len(after[0]), search_tools._CONTENT_MAX_CHARS)


# ---------------------------------------------------------------------------
# Limit clamping tests.
# ---------------------------------------------------------------------------


class ResolveLimitTests(unittest.TestCase):
    def test_default_for_zero(self) -> None:
        self.assertEqual(
            search_tools._resolve_limit(0),
            search_tools._DEFAULT_LIMIT,
        )

    def test_default_for_negative(self) -> None:
        self.assertEqual(
            search_tools._resolve_limit(-5),
            search_tools._DEFAULT_LIMIT,
        )

    def test_passes_through_in_bounds(self) -> None:
        self.assertEqual(search_tools._resolve_limit(42), 42)

    def test_clamps_to_max(self) -> None:
        self.assertEqual(
            search_tools._resolve_limit(500),
            search_tools._MAX_LIMIT,
        )


# ---------------------------------------------------------------------------
# File plan tests.
# ---------------------------------------------------------------------------


class FilePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()

    def tearDown(self) -> None:
        self.world.cleanup()

    def test_kernel_order_is_fixed(self) -> None:
        self.world.make_walnut(
            "alive",
            log_text="log",
            insights_text="insights",
            now_text='{"phase":"x"}',
        )
        plan = search_tools._walnut_file_plan(
            str(self.world.root),
            str(self.world.walnut_path("alive")),
        )
        # Fixed order: key, log, insights, now.
        self.assertEqual(
            plan[:4],
            [
                "_kernel/key.md",
                "_kernel/log.md",
                "_kernel/insights.md",
                "_kernel/now.json",
            ],
        )

    def test_missing_files_dropped_from_plan(self) -> None:
        # No log.md, no now.json.
        self.world.make_walnut("alive", insights_text="insights")
        plan = search_tools._walnut_file_plan(
            str(self.world.root),
            str(self.world.walnut_path("alive")),
        )
        self.assertEqual(plan, ["_kernel/key.md", "_kernel/insights.md"])

    def test_v2_now_is_not_searched(self) -> None:
        # Spec: search inclusion is frozen at the v3 kernel layout.
        # A v2 ``_kernel/_generated/now.json`` exists but is NOT
        # part of the file plan -- only the v3 ``_kernel/now.json``
        # qualifies, and when it's absent now.json contributes
        # nothing to the plan.
        self.world.make_walnut("alive", log_text="log")
        generated = (
            self.world.walnut_path("alive") / "_kernel" / "_generated"
        )
        generated.mkdir()
        (generated / "now.json").write_text('{"phase":"x"}', encoding="utf-8")
        plan = search_tools._walnut_file_plan(
            str(self.world.root),
            str(self.world.walnut_path("alive")),
        )
        self.assertNotIn("_kernel/_generated/now.json", plan)
        self.assertNotIn("_kernel/now.json", plan)

    def test_bundles_under_templates_skipped(self) -> None:
        self.world.make_walnut("alive")
        # Bundle at templates/foo should be filtered because
        # ``templates`` is in _SKIP_DIRS per the spec.
        self.world.make_bundle(
            "alive", "templates/foo", "goal: should not be searched\n"
        )
        # And a normal bundle as a positive control.
        self.world.make_bundle(
            "alive", "bundles/real", "goal: should be searched\n"
        )
        plan = search_tools._walnut_file_plan(
            str(self.world.root),
            str(self.world.walnut_path("alive")),
        )
        self.assertIn("bundles/real/context.manifest.yaml", plan)
        self.assertNotIn(
            "templates/foo/context.manifest.yaml", plan
        )

    def test_archive_bundles_included(self) -> None:
        # Spec-frozen skip set does NOT include ``_archive`` or
        # ``01_Archive`` -- only node_modules, .git, __pycache__,
        # .venv, venv, dist, build, .next, target, templates, raw.
        # The vendored find_bundles DOES skip those (and _core +
        # 01_Archive), so using vendor discovery would silently drop
        # archive bundles from search -- violating the frozen rules.
        # This test asserts the local walk includes them.
        self.world.make_walnut("alive")
        self.world.make_bundle(
            "alive", "_archive/retired-one", "goal: archived\n"
        )
        plan = search_tools._walnut_file_plan(
            str(self.world.root),
            str(self.world.walnut_path("alive")),
        )
        self.assertIn(
            "_archive/retired-one/context.manifest.yaml", plan
        )

    def test_nested_walnut_bundles_excluded(self) -> None:
        # A nested walnut (its own _kernel/key.md) is a boundary.
        # Its bundles are that walnut's inventory, not the parent's.
        self.world.make_walnut("parent")
        self.world.make_walnut("parent/nested")
        self.world.make_bundle(
            "parent/nested", "bundles/child", "goal: child\n"
        )
        plan = search_tools._walnut_file_plan(
            str(self.world.root),
            str(self.world.walnut_path("parent")),
        )
        # Parent's plan must not include the nested walnut's bundle.
        self.assertNotIn(
            "nested/bundles/child/context.manifest.yaml", plan
        )

    def test_node_modules_skipped(self) -> None:
        self.world.make_walnut("alive")
        # A manifest accidentally landing under node_modules must
        # not surface in search results.
        self.world.make_bundle(
            "alive", "node_modules/some-pkg", "goal: should not appear\n"
        )
        plan = search_tools._walnut_file_plan(
            str(self.world.root),
            str(self.world.walnut_path("alive")),
        )
        self.assertNotIn(
            "node_modules/some-pkg/context.manifest.yaml", plan
        )

    def test_bundle_manifests_sorted_alphabetically(self) -> None:
        self.world.make_walnut("alive")
        # Create bundles in reverse-alphabetical order to confirm sort.
        self.world.make_bundle("alive", "zeta", "goal: z\n")
        self.world.make_bundle("alive", "alpha", "goal: a\n")
        self.world.make_bundle("alive", "mid", "goal: m\n")
        plan = search_tools._walnut_file_plan(
            str(self.world.root),
            str(self.world.walnut_path("alive")),
        )
        manifests = [p for p in plan if p.endswith("context.manifest.yaml")]
        self.assertEqual(
            manifests,
            [
                "alpha/context.manifest.yaml",
                "mid/context.manifest.yaml",
                "zeta/context.manifest.yaml",
            ],
        )


# ---------------------------------------------------------------------------
# search_world end-to-end tests.
# ---------------------------------------------------------------------------


class SearchWorldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()

    def tearDown(self) -> None:
        self.world.cleanup()

    def _build_basic_world(self) -> None:
        """Three walnuts with distinct content mentioning 'MCP'."""
        # 04_Ventures/alive -- log contains "MCP server" reference.
        self.world.make_walnut(
            "04_Ventures/alive",
            log_text="# Log\n\nDecided to ship MCP server\nOther note\n",
            now_text='{"phase":"testing","next":"MCP review"}',
        )
        self.world.make_bundle(
            "04_Ventures/alive",
            "bundles/mcp-research",
            "goal: MCP competitor analysis\nstatus: draft\n",
        )
        # 02_Life/people/ben-flint -- insights mentions MCP.
        self.world.make_walnut(
            "02_Life/people/ben-flint",
            insights_text="# Insights\n\nBen cares about MCP distribution.\n",
        )
        # 05_Experiments/unrelated -- no MCP mentions.
        self.world.make_walnut(
            "05_Experiments/unrelated",
            log_text="Nothing to see here\n",
        )

    def test_no_world_returns_err_no_world(self) -> None:
        ctx = _fake_ctx(None)
        result = _run(search_tools.search_world(ctx, query="anything"))
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], "NO_WORLD")

    def test_basic_match_finds_across_walnuts(self) -> None:
        self._build_basic_world()
        ctx = _fake_ctx(str(self.world.root))
        result = _run(search_tools.search_world(ctx, query="MCP"))
        self.assertFalse(result["isError"])
        matches = result["structuredContent"]["matches"]
        walnuts_hit = {m["walnut"] for m in matches}
        # All three mentioned walnuts should surface.
        self.assertIn("04_Ventures/alive", walnuts_hit)
        self.assertIn("02_Life/people/ben-flint", walnuts_hit)
        # And the unrelated walnut should NOT.
        self.assertNotIn("05_Experiments/unrelated", walnuts_hit)

    def test_case_insensitive_by_default(self) -> None:
        self._build_basic_world()
        ctx = _fake_ctx(str(self.world.root))
        upper = _run(search_tools.search_world(ctx, query="MCP"))
        lower = _run(search_tools.search_world(ctx, query="mcp"))
        self.assertEqual(
            len(upper["structuredContent"]["matches"]),
            len(lower["structuredContent"]["matches"]),
        )

    def test_case_sensitive_filters(self) -> None:
        self.world.make_walnut(
            "alive",
            log_text="Uppercase MCP only here\nlowercase mcp only here\n",
        )
        ctx = _fake_ctx(str(self.world.root))
        sensitive_upper = _run(
            search_tools.search_world(
                ctx, query="MCP", case_sensitive=True
            )
        )
        sensitive_lower = _run(
            search_tools.search_world(
                ctx, query="mcp", case_sensitive=True
            )
        )
        # Different case-sensitive searches find different lines.
        up_lines = {
            m["line_number"] for m in sensitive_upper["structuredContent"]["matches"]
        }
        lo_lines = {
            m["line_number"] for m in sensitive_lower["structuredContent"]["matches"]
        }
        self.assertEqual(up_lines, {1})
        self.assertEqual(lo_lines, {2})

    def test_match_shape_includes_required_fields(self) -> None:
        self.world.make_walnut("alive", log_text="target line\n")
        ctx = _fake_ctx(str(self.world.root))
        result = _run(search_tools.search_world(ctx, query="target"))
        m = result["structuredContent"]["matches"][0]
        for field in (
            "walnut", "file", "line_number", "content",
            "context_before", "context_after",
        ):
            self.assertIn(field, m)

    def test_content_capped_at_200_chars(self) -> None:
        long_line = "x" * 500 + " MATCH " + "y" * 500
        self.world.make_walnut("alive", log_text=long_line + "\n")
        ctx = _fake_ctx(str(self.world.root))
        result = _run(search_tools.search_world(ctx, query="MATCH"))
        m = result["structuredContent"]["matches"][0]
        self.assertLessEqual(
            len(m["content"]), search_tools._CONTENT_MAX_CHARS
        )

    def test_context_window_is_two_each(self) -> None:
        text = "\n".join([f"line{i}" for i in range(1, 11)] + ["match", "after1", "after2"])
        # Lines: 1..10 = "line1".."line10", 11="match", 12="after1", 13="after2"
        self.world.make_walnut("alive", log_text=text + "\n")
        ctx = _fake_ctx(str(self.world.root))
        result = _run(search_tools.search_world(ctx, query="match"))
        m = result["structuredContent"]["matches"][0]
        self.assertEqual(m["line_number"], 11)
        self.assertEqual(m["context_before"], ["line9", "line10"])
        self.assertEqual(m["context_after"], ["after1", "after2"])

    def test_pagination_cursor_then_resume(self) -> None:
        # 10 matches on separate lines within one file.
        lines = [f"match line {i}" for i in range(10)]
        self.world.make_walnut("alive", log_text="\n".join(lines) + "\n")
        ctx = _fake_ctx(str(self.world.root))
        page1 = _run(
            search_tools.search_world(ctx, query="match", limit=5)
        )
        self.assertFalse(page1["isError"])
        self.assertEqual(
            len(page1["structuredContent"]["matches"]), 5
        )
        cursor = page1["structuredContent"]["next_cursor"]
        self.assertIsNotNone(cursor)

        page2 = _run(
            search_tools.search_world(
                ctx, query="match", limit=5, cursor=cursor
            )
        )
        self.assertEqual(
            len(page2["structuredContent"]["matches"]), 5
        )
        # No overlap between page1 and page2.
        page1_lines = [
            m["line_number"] for m in page1["structuredContent"]["matches"]
        ]
        page2_lines = [
            m["line_number"] for m in page2["structuredContent"]["matches"]
        ]
        self.assertEqual(set(page1_lines) & set(page2_lines), set())
        # Final page ends the pagination.
        self.assertIsNone(page2["structuredContent"]["next_cursor"])

    def test_stable_ordering_across_calls(self) -> None:
        self._build_basic_world()
        ctx = _fake_ctx(str(self.world.root))
        r1 = _run(search_tools.search_world(ctx, query="MCP"))
        r2 = _run(search_tools.search_world(ctx, query="MCP"))
        self.assertEqual(
            r1["structuredContent"]["matches"],
            r2["structuredContent"]["matches"],
        )

    def test_deterministic_walnut_ordering(self) -> None:
        # Three walnuts whose names sort non-obviously. All have the
        # same match so ordering in the result confirms traversal
        # order.
        self.world.make_walnut("04_Ventures/beta", log_text="common\n")
        self.world.make_walnut("04_Ventures/alpha", log_text="common\n")
        self.world.make_walnut("02_Life/people/alice", log_text="common\n")
        ctx = _fake_ctx(str(self.world.root))
        result = _run(search_tools.search_world(ctx, query="common"))
        walnuts = [m["walnut"] for m in result["structuredContent"]["matches"]]
        # POSIX-relpath sort: 02_Life/... < 04_Ventures/alpha < 04_Ventures/beta.
        self.assertEqual(
            walnuts,
            [
                "02_Life/people/alice",
                "04_Ventures/alpha",
                "04_Ventures/beta",
            ],
        )

    def test_invalid_cursor_returns_err_invalid_cursor(self) -> None:
        self._build_basic_world()
        ctx = _fake_ctx(str(self.world.root))
        result = _run(
            search_tools.search_world(
                ctx, query="MCP", cursor="not-a-valid-cursor!!!"
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"], "INVALID_CURSOR"
        )

    def test_large_file_listed_in_skipped_walnut_anchored(self) -> None:
        # search_world: skipped paths are walnut-anchored so callers
        # can disambiguate across walnuts without cross-referencing.
        big_line = "match " + ("x" * 1000) + "\n"
        # Each line ~1007 bytes; need > 500KB so make 600.
        body = big_line * 600
        self.world.make_walnut("big-walnut", log_text=body)
        ctx = _fake_ctx(str(self.world.root))
        result = _run(search_tools.search_world(ctx, query="match"))
        skipped = result["structuredContent"]["skipped"]
        # Walnut prefix present.
        paths = [s["path"] for s in skipped]
        self.assertIn("big-walnut/_kernel/log.md", paths)
        reasons = [s["reason"] for s in skipped]
        self.assertIn(
            search_tools._SKIP_REASON_TOO_LARGE, reasons
        )

    def test_unicode_nfkc_equivalence(self) -> None:
        # Write decomposed form; query composed form. The match must
        # still succeed because both sides are NFKC-folded.
        decomposed = "cafe\u0301 is open\n"
        composed_query = "caf\u00e9"
        self.world.make_walnut("alive", log_text=decomposed)
        ctx = _fake_ctx(str(self.world.root))
        result = _run(
            search_tools.search_world(ctx, query=composed_query)
        )
        self.assertEqual(
            len(result["structuredContent"]["matches"]), 1
        )

    def test_unicode_nfkc_compat_equivalence(self) -> None:
        # Write content using an NFKC-but-not-NFC equivalent (the
        # ``\ufb00`` ligature). Query with plain "ff". NFKC
        # equivalence catches this; NFC would not.
        self.world.make_walnut(
            "alive", log_text="the \ufb00iciency tradeoff\n"
        )
        ctx = _fake_ctx(str(self.world.root))
        result = _run(search_tools.search_world(ctx, query="ffici"))
        self.assertEqual(
            len(result["structuredContent"]["matches"]), 1
        )

    def test_limit_clamped_to_max(self) -> None:
        # More matches than the max limit; only max returned.
        matches = "\n".join([f"match {i}" for i in range(250)])
        self.world.make_walnut("alive", log_text=matches + "\n")
        ctx = _fake_ctx(str(self.world.root))
        # Request 500 (above cap). Response is capped at _MAX_LIMIT.
        result = _run(
            search_tools.search_world(ctx, query="match", limit=500)
        )
        self.assertLessEqual(
            len(result["structuredContent"]["matches"]),
            search_tools._MAX_LIMIT,
        )

    def test_no_match_returns_empty_list(self) -> None:
        self.world.make_walnut("alive", log_text="hello\n")
        ctx = _fake_ctx(str(self.world.root))
        result = _run(
            search_tools.search_world(ctx, query="nonexistent")
        )
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["matches"], [])
        self.assertIsNone(result["structuredContent"]["next_cursor"])

    def test_empty_query_short_circuits(self) -> None:
        # An empty query would otherwise match every line (``"" in
        # haystack`` is always True). Guard returns empty matches
        # without scanning.
        self.world.make_walnut(
            "alive", log_text="a\nb\nc\nd\ne\n"
        )
        ctx = _fake_ctx(str(self.world.root))
        result = _run(search_tools.search_world(ctx, query=""))
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["matches"], [])
        self.assertIsNone(result["structuredContent"]["next_cursor"])

    def test_whitespace_query_short_circuits(self) -> None:
        self.world.make_walnut(
            "alive", log_text="a\nb\nc\n"
        )
        ctx = _fake_ctx(str(self.world.root))
        result = _run(search_tools.search_world(ctx, query="   "))
        self.assertEqual(result["structuredContent"]["matches"], [])

    def test_cursor_past_walnut_inventory_invalid(self) -> None:
        # Create one walnut, then supply a cursor pointing past it.
        self.world.make_walnut("alive", log_text="x\n")
        ctx = _fake_ctx(str(self.world.root))
        stale = search_tools._Cursor(wi=99, fi=0, lo=0).encode()
        result = _run(
            search_tools.search_world(ctx, query="x", cursor=stale)
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"], "INVALID_CURSOR"
        )

    def test_cursor_past_file_plan_invalid(self) -> None:
        # One walnut with a short file plan; cursor claims file index
        # past the plan length -> ERR_INVALID_CURSOR.
        self.world.make_walnut("alive", log_text="x\n")
        ctx = _fake_ctx(str(self.world.root))
        stale = search_tools._Cursor(wi=0, fi=99, lo=0).encode()
        result = _run(
            search_tools.search_world(ctx, query="x", cursor=stale)
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"], "INVALID_CURSOR"
        )


# ---------------------------------------------------------------------------
# search_walnut tests.
# ---------------------------------------------------------------------------


class SearchWalnutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()

    def tearDown(self) -> None:
        self.world.cleanup()

    def test_scoped_to_single_walnut(self) -> None:
        self.world.make_walnut(
            "04_Ventures/alive",
            log_text="inside alive\n",
        )
        self.world.make_walnut(
            "04_Ventures/other",
            log_text="inside alive\n",  # same text, different walnut
        )
        ctx = _fake_ctx(str(self.world.root))
        result = _run(
            search_tools.search_walnut(
                ctx, walnut="04_Ventures/alive", query="inside"
            )
        )
        matches = result["structuredContent"]["matches"]
        walnuts = {m["walnut"] for m in matches}
        self.assertEqual(walnuts, {"04_Ventures/alive"})

    def test_unknown_walnut_returns_not_found(self) -> None:
        self.world.make_walnut("04_Ventures/alive")
        ctx = _fake_ctx(str(self.world.root))
        result = _run(
            search_tools.search_walnut(
                ctx, walnut="04_Ventures/nonexistent", query="x"
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"], "WALNUT_NOT_FOUND"
        )

    def test_path_escape_returns_err_path_escape(self) -> None:
        self.world.make_walnut("alive")
        ctx = _fake_ctx(str(self.world.root))
        result = _run(
            search_tools.search_walnut(
                ctx, walnut="../../escape", query="x"
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"], "PATH_ESCAPE"
        )

    def test_no_world_returns_err_no_world(self) -> None:
        ctx = _fake_ctx(None)
        result = _run(
            search_tools.search_walnut(
                ctx, walnut="any", query="any"
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"], "NO_WORLD"
        )

    def test_invalid_cursor_returns_invalid(self) -> None:
        self.world.make_walnut("alive", log_text="x\n")
        ctx = _fake_ctx(str(self.world.root))
        result = _run(
            search_tools.search_walnut(
                ctx, walnut="alive", query="x", cursor="!!!"
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"], "INVALID_CURSOR"
        )

    def test_pagination_end_to_end(self) -> None:
        lines = [f"match {i}" for i in range(12)]
        self.world.make_walnut(
            "alive", log_text="\n".join(lines) + "\n"
        )
        ctx = _fake_ctx(str(self.world.root))
        all_matches: List[dict] = []
        cursor: Optional[str] = None
        pages = 0
        while True:
            pages += 1
            if pages > 10:  # safety
                self.fail("pagination did not terminate")
            r = _run(
                search_tools.search_walnut(
                    ctx,
                    walnut="alive",
                    query="match",
                    limit=5,
                    cursor=cursor,
                )
            )
            all_matches.extend(r["structuredContent"]["matches"])
            cursor = r["structuredContent"]["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(all_matches), 12)
        # Uniqueness -- no match seen twice.
        seen = set()
        for m in all_matches:
            key = (m["walnut"], m["file"], m["line_number"])
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_searches_bundle_manifests(self) -> None:
        self.world.make_walnut("alive")
        self.world.make_bundle(
            "alive",
            "bundles/research",
            "goal: find moat C\nstatus: draft\n",
        )
        ctx = _fake_ctx(str(self.world.root))
        result = _run(
            search_tools.search_walnut(
                ctx, walnut="alive", query="moat C"
            )
        )
        matches = result["structuredContent"]["matches"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            matches[0]["file"],
            "bundles/research/context.manifest.yaml",
        )

    def test_skipped_path_is_walnut_relative(self) -> None:
        # search_walnut: skipped paths are walnut-relative because
        # the tool is already scoped to one walnut -- re-prefixing
        # would be redundant noise.
        big_line = "match " + ("x" * 1000) + "\n"
        body = big_line * 600
        self.world.make_walnut("alive", log_text=body)
        ctx = _fake_ctx(str(self.world.root))
        result = _run(
            search_tools.search_walnut(
                ctx, walnut="alive", query="match"
            )
        )
        skipped = result["structuredContent"]["skipped"]
        paths = [s["path"] for s in skipped]
        # Walnut-relative: no "alive/" prefix.
        self.assertIn("_kernel/log.md", paths)
        self.assertNotIn("alive/_kernel/log.md", paths)


# ---------------------------------------------------------------------------
# Registration contract.
# ---------------------------------------------------------------------------


class RegistrationTests(unittest.TestCase):
    def test_register_attaches_two_tools(self) -> None:
        server = MagicMock()
        search_tools.register(server)
        # server.tool is called twice (one per tool).
        self.assertEqual(server.tool.call_count, 2)
        names = [
            call.kwargs["name"] for call in server.tool.call_args_list
        ]
        self.assertIn("search_world", names)
        self.assertIn("search_walnut", names)

    def test_both_tools_are_read_only(self) -> None:
        server = MagicMock()
        search_tools.register(server)
        for call in server.tool.call_args_list:
            ann = call.kwargs["annotations"]
            self.assertTrue(ann.readOnlyHint)
            self.assertFalse(ann.destructiveHint)
            self.assertFalse(ann.openWorldHint)


# ---------------------------------------------------------------------------
# Server integration.
# ---------------------------------------------------------------------------


class ServerWiringTests(unittest.TestCase):
    def test_build_server_registers_search_tools(self) -> None:
        # Import inside the test to avoid pulling FastMCP at module
        # import time (matches test_tools_bundle.py posture).
        from alive_mcp.server import build_server  # noqa: E402

        server = build_server()
        # FastMCP stores tools internally; list via its public API.
        # The attribute name varies; fall back to listing via the
        # async ``list_tools()`` coroutine.
        tools = _run(server.list_tools())
        names = {t.name for t in tools}
        self.assertIn("search_world", names)
        self.assertIn("search_walnut", names)


if __name__ == "__main__":
    unittest.main()
