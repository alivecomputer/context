"""Unit tests for log + task MCP tools (fn-10-60k.9 / T9).

Covers:

* :func:`parse_log_entries` -- the file-level entry extractor, with
  fixture-driven cases for the frozen contract (entry delimiters,
  frontmatter stripping, signed-trailer preservation, ``---`` divider
  handling).
* :func:`read_log` -- pagination, chapter spanning, ``next_offset``
  semantics, ``chapter_boundary_crossed`` flag, all error paths.
* :func:`list_tasks` -- walnut-scoped (unscoped=None), bundle-scoped,
  counts shape, and error paths.
* Tool registration -- names, annotations, descriptions.

Handlers are exercised directly (not through stdio) so assertions run
against plain envelope dicts. Mirrors the pattern in
``test_tools_walnut.py`` / ``test_tools_bundle.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from typing import Any, List, Optional
from unittest.mock import MagicMock

# Make src/ importable the same way tests/__init__.py does.
import tests  # noqa: F401

from alive_mcp import errors  # noqa: E402
from alive_mcp.tools import log_and_tasks as lt_tools  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers.
# ---------------------------------------------------------------------------


@dataclass
class FixtureWorld:
    """Temp ALIVE world for single-test construction."""

    root: pathlib.Path
    cleanup: Any

    def walnut_path(self, rel: str) -> pathlib.Path:
        return self.root / rel

    def write_kernel_file(
        self, walnut_rel: str, name: str, content: str
    ) -> pathlib.Path:
        target = self.walnut_path(walnut_rel) / "_kernel" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_chapter(
        self, walnut_rel: str, chapter_num: int, content: str
    ) -> pathlib.Path:
        target = (
            self.walnut_path(walnut_rel)
            / "_kernel"
            / "history"
            / f"chapter-{chapter_num:02d}.md"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_bundle(
        self,
        walnut_rel: str,
        bundle_rel: str,
        manifest_content: str = "goal: test\nstatus: draft\n",
        tasks: Optional[List[dict]] = None,
    ) -> pathlib.Path:
        bundle_dir = self.walnut_path(walnut_rel) / bundle_rel
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "context.manifest.yaml").write_text(
            manifest_content, encoding="utf-8"
        )
        if tasks is not None:
            (bundle_dir / "tasks.json").write_text(
                json.dumps({"tasks": tasks}), encoding="utf-8"
            )
        return bundle_dir


def _make_walnut(world: FixtureWorld, rel: str) -> None:
    world.write_kernel_file(
        rel,
        "key.md",
        "---\ntype: venture\ngoal: test\n---\n\n# test walnut\n",
    )


def _new_world() -> FixtureWorld:
    tmpdir = tempfile.mkdtemp(prefix="alive-mcp-logtask-test-")
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


# ---------------------------------------------------------------------------
# Canonical log fixtures. Each file matches the real ALIVE world shape
# so the tests exercise the production entry-extraction path, not a
# stripped-down variant.
# ---------------------------------------------------------------------------


def _make_log_with_entries(entries: List[dict]) -> str:
    """Build a log.md fixture with the given entries (newest first).

    Each entry dict takes:
        timestamp: ISO-8601 (str)
        squirrel: hex id (str)
        body: entry body text WITHOUT the heading or signed line.
        signed: optional signed squirrel id (str); defaults to
                ``entry["squirrel"]`` when absent.
    Entries are joined with ``---`` dividers matching the production
    format.
    """
    frontmatter = textwrap.dedent(
        f"""\
        ---
        walnut: test
        created: 2026-01-01
        last-entry: {entries[0]['timestamp']}
        entry-count: {len(entries)}
        summary: Test log.
        ---
        """
    )
    parts = [frontmatter]
    for e in entries:
        signed = e.get("signed", e["squirrel"])
        parts.append(
            f"\n## {e['timestamp']} -- squirrel:{e['squirrel']}\n"
            f"\n{e['body']}\n"
            f"\nsigned: squirrel:{signed}\n"
        )
        parts.append("\n---\n")
    return "".join(parts)


SEVEN_ENTRIES = [
    {"timestamp": "2026-04-16T14:30:00", "squirrel": "aaaa1111",
     "body": "Entry 7 (newest)."},
    {"timestamp": "2026-04-15T11:00:00", "squirrel": "bbbb2222",
     "body": "Entry 6."},
    {"timestamp": "2026-04-14T09:00:00", "squirrel": "cccc3333",
     "body": "Entry 5."},
    {"timestamp": "2026-04-13T08:00:00", "squirrel": "dddd4444",
     "body": "Entry 4."},
    {"timestamp": "2026-04-12T07:00:00", "squirrel": "eeee5555",
     "body": "Entry 3."},
    {"timestamp": "2026-04-11T06:00:00", "squirrel": "ffff6666",
     "body": "Entry 2."},
    {"timestamp": "2026-04-10T05:00:00", "squirrel": "11112222",
     "body": "Entry 1 (oldest)."},
]


# ---------------------------------------------------------------------------
# parse_log_entries unit tests.
# ---------------------------------------------------------------------------


class ParseLogEntriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(self.world, "demo")

    def _write_log(self, content: str) -> str:
        path = self.world.write_kernel_file("demo", "log.md", content)
        return str(path)

    def test_empty_file_returns_no_entries(self) -> None:
        path = self._write_log("")
        self.assertEqual(lt_tools.parse_log_entries(path, "demo"), [])

    def test_missing_file_returns_empty(self) -> None:
        path = self.world.walnut_path("demo") / "_kernel" / "log.md"
        # Do NOT write the file.
        self.assertEqual(
            lt_tools.parse_log_entries(str(path), "demo"), []
        )

    def test_frontmatter_stripped_before_parsing(self) -> None:
        log = _make_log_with_entries(SEVEN_ENTRIES[:1])
        path = self._write_log(log)
        entries = lt_tools.parse_log_entries(path, "demo")
        self.assertEqual(len(entries), 1)
        # The frontmatter's closing --- must not be treated as an
        # entry boundary that swallows the first entry's body.
        self.assertIn("Entry 7", entries[0].body)

    def test_seven_entries_in_file_order(self) -> None:
        log = _make_log_with_entries(SEVEN_ENTRIES)
        path = self._write_log(log)
        entries = lt_tools.parse_log_entries(path, "demo")
        self.assertEqual(len(entries), 7)
        self.assertEqual(entries[0].timestamp, "2026-04-16T14:30:00")
        self.assertEqual(entries[-1].timestamp, "2026-04-10T05:00:00")

    def test_squirrel_id_parsed_from_heading(self) -> None:
        log = _make_log_with_entries(SEVEN_ENTRIES[:2])
        path = self._write_log(log)
        entries = lt_tools.parse_log_entries(path, "demo")
        self.assertEqual(entries[0].squirrel_id, "aaaa1111")
        self.assertEqual(entries[1].squirrel_id, "bbbb2222")

    def test_signed_trailer_preserved_in_body(self) -> None:
        log = _make_log_with_entries(SEVEN_ENTRIES[:1])
        path = self._write_log(log)
        entries = lt_tools.parse_log_entries(path, "demo")
        self.assertIn("signed: squirrel:aaaa1111", entries[0].body)
        self.assertEqual(entries[0].signed, "squirrel:aaaa1111")

    def test_divider_terminates_body(self) -> None:
        log = _make_log_with_entries(SEVEN_ENTRIES[:2])
        path = self._write_log(log)
        entries = lt_tools.parse_log_entries(path, "demo")
        # Entry 7's body must NOT contain entry 6's timestamp or
        # body text -- the divider between them terminated entry 7.
        self.assertNotIn("2026-04-15T11:00:00", entries[0].body)
        self.assertNotIn("Entry 6.", entries[0].body)

    def test_walnut_stamped_on_every_entry(self) -> None:
        log = _make_log_with_entries(SEVEN_ENTRIES[:3])
        path = self._write_log(log)
        entries = lt_tools.parse_log_entries(path, "04_Ventures/demo")
        for e in entries:
            self.assertEqual(e.walnut, "04_Ventures/demo")

    def test_entry_without_divider_runs_to_eof(self) -> None:
        """No trailing `---` is valid -- body runs to EOF."""
        content = textwrap.dedent(
            """\
            ## 2026-04-16T14:30:00 -- squirrel:abcd1234

            Only entry, no divider, no trailing ---.

            signed: squirrel:abcd1234
            """
        )
        path = self._write_log(content)
        entries = lt_tools.parse_log_entries(path, "demo")
        self.assertEqual(len(entries), 1)
        self.assertIn("Only entry", entries[0].body)
        self.assertEqual(entries[0].signed, "squirrel:abcd1234")

    def test_heading_with_extra_metadata_still_parses(self) -> None:
        """Headings may carry labels after the timestamp."""
        content = textwrap.dedent(
            """\
            ## 2026-04-16T14:30:00 -- squirrel:aa11bb22 -- phase:shipping

            Body here.

            signed: squirrel:aa11bb22
            """
        )
        path = self._write_log(content)
        entries = lt_tools.parse_log_entries(path, "demo")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].squirrel_id, "aa11bb22")


# ---------------------------------------------------------------------------
# read_log tool tests.
# ---------------------------------------------------------------------------


class ReadLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(self.world, "demo")
        log = _make_log_with_entries(SEVEN_ENTRIES)
        self.world.write_kernel_file("demo", "log.md", log)

    def _call(
        self, walnut: str, offset: int = 0, limit: int = 20
    ) -> dict:
        ctx = _fake_ctx(str(self.world.root))
        return _run(lt_tools.read_log(ctx, walnut, offset, limit))

    def test_no_world(self) -> None:
        ctx = _fake_ctx(None)
        env = _run(lt_tools.read_log(ctx, "demo", 0, 5))
        self.assertTrue(env["isError"])
        self.assertEqual(env["structuredContent"]["error"], "NO_WORLD")

    def test_walnut_not_found(self) -> None:
        env = self._call("does-not-exist")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "WALNUT_NOT_FOUND"
        )

    def test_path_escape(self) -> None:
        env = self._call("../../escape")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "PATH_ESCAPE"
        )

    def test_acceptance_first_five_entries(self) -> None:
        """read_log(walnut, offset=0, limit=5) returns the 5 newest."""
        env = self._call("demo", offset=0, limit=5)
        self.assertFalse(env["isError"], msg=env)
        sc = env["structuredContent"]
        self.assertEqual(len(sc["entries"]), 5)
        # Newest first.
        self.assertEqual(sc["entries"][0]["timestamp"], "2026-04-16T14:30:00")
        self.assertEqual(sc["entries"][4]["timestamp"], "2026-04-12T07:00:00")
        self.assertEqual(sc["next_offset"], 5)
        self.assertEqual(sc["total_entries"], 7)
        self.assertEqual(sc["total_chapters"], 0)
        self.assertFalse(sc["chapter_boundary_crossed"])

    def test_acceptance_deterministic_window(self) -> None:
        """offset=2, limit=3 returns entries 3-5 (0-indexed from newest)."""
        env = self._call("demo", offset=2, limit=3)
        sc = env["structuredContent"]
        self.assertEqual(len(sc["entries"]), 3)
        self.assertEqual(
            [e["timestamp"] for e in sc["entries"]],
            [
                "2026-04-14T09:00:00",  # entry 3 (0-indexed from newest)
                "2026-04-13T08:00:00",  # entry 4
                "2026-04-12T07:00:00",  # entry 5
            ],
        )
        self.assertEqual(sc["next_offset"], 5)

    def test_entry_shape(self) -> None:
        env = self._call("demo", offset=0, limit=1)
        entry = env["structuredContent"]["entries"][0]
        self.assertEqual(
            set(entry.keys()),
            {"timestamp", "walnut", "squirrel_id", "body", "signed"},
        )
        self.assertEqual(entry["walnut"], "demo")
        self.assertEqual(entry["squirrel_id"], "aaaa1111")
        self.assertIn("Entry 7", entry["body"])
        self.assertEqual(entry["signed"], "squirrel:aaaa1111")

    def test_limit_clamped_to_cap(self) -> None:
        env = self._call("demo", offset=0, limit=10_000)
        # Cap is 100; but we only have 7 entries so 7 returned.
        self.assertEqual(len(env["structuredContent"]["entries"]), 7)

    def test_negative_offset_treated_as_zero(self) -> None:
        env = self._call("demo", offset=-5, limit=2)
        sc = env["structuredContent"]
        self.assertEqual(sc["entries"][0]["timestamp"], "2026-04-16T14:30:00")

    def test_non_positive_limit_uses_default(self) -> None:
        env = self._call("demo", offset=0, limit=0)
        # Default is 20; we have 7 entries.
        self.assertEqual(len(env["structuredContent"]["entries"]), 7)

    def test_offset_past_end_returns_empty(self) -> None:
        env = self._call("demo", offset=100, limit=5)
        sc = env["structuredContent"]
        self.assertEqual(sc["entries"], [])
        self.assertIsNone(sc["next_offset"])

    def test_final_page_has_null_next_offset(self) -> None:
        env = self._call("demo", offset=5, limit=5)
        sc = env["structuredContent"]
        self.assertEqual(len(sc["entries"]), 2)
        self.assertIsNone(sc["next_offset"])

    def test_fresh_walnut_no_log_returns_empty_payload(self) -> None:
        _make_walnut(self.world, "fresh")
        env = self._call("fresh", offset=0, limit=5)
        self.assertFalse(env["isError"], msg=env)
        sc = env["structuredContent"]
        self.assertEqual(sc["entries"], [])
        self.assertEqual(sc["total_entries"], 0)
        self.assertEqual(sc["total_chapters"], 0)
        self.assertIsNone(sc["next_offset"])
        self.assertFalse(sc["chapter_boundary_crossed"])


class ReadLogChapterSpanningTests(unittest.TestCase):
    """Chapter-aware pagination: when offset + limit exceeds the
    active log, the tool spans into chapter files in descending
    chapter number. ``chapter_boundary_crossed`` flips when at least
    one returned entry came from a chapter.
    """

    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(self.world, "demo")
        # Active log has 3 entries (newest).
        self.world.write_kernel_file(
            "demo",
            "log.md",
            _make_log_with_entries(
                [
                    {"timestamp": "2026-04-16T12:00:00", "squirrel": "1" * 8,
                     "body": "Active 1 (newest overall)."},
                    {"timestamp": "2026-04-15T12:00:00", "squirrel": "2" * 8,
                     "body": "Active 2."},
                    {"timestamp": "2026-04-14T12:00:00", "squirrel": "3" * 8,
                     "body": "Active 3."},
                ]
            ),
        )
        # Chapter 02 has 3 entries (more recent chapter).
        self.world.write_chapter(
            "demo",
            2,
            _make_log_with_entries(
                [
                    {"timestamp": "2026-03-30T12:00:00", "squirrel": "4" * 8,
                     "body": "Chapter 02 entry 1."},
                    {"timestamp": "2026-03-29T12:00:00", "squirrel": "5" * 8,
                     "body": "Chapter 02 entry 2."},
                    {"timestamp": "2026-03-28T12:00:00", "squirrel": "6" * 8,
                     "body": "Chapter 02 entry 3."},
                ]
            ),
        )
        # Chapter 01 has 2 entries (oldest).
        self.world.write_chapter(
            "demo",
            1,
            _make_log_with_entries(
                [
                    {"timestamp": "2026-03-15T12:00:00", "squirrel": "7" * 8,
                     "body": "Chapter 01 entry 1."},
                    {"timestamp": "2026-03-14T12:00:00", "squirrel": "8" * 8,
                     "body": "Chapter 01 entry 2."},
                ]
            ),
        )

    def _call(
        self, offset: int = 0, limit: int = 20
    ) -> dict:
        ctx = _fake_ctx(str(self.world.root))
        return _run(lt_tools.read_log(ctx, "demo", offset, limit))

    def test_total_entries_covers_log_plus_chapters(self) -> None:
        env = self._call(offset=0, limit=1)
        self.assertEqual(env["structuredContent"]["total_entries"], 8)
        self.assertEqual(env["structuredContent"]["total_chapters"], 2)

    def test_first_page_is_active_log_only(self) -> None:
        env = self._call(offset=0, limit=3)
        sc = env["structuredContent"]
        self.assertEqual(len(sc["entries"]), 3)
        self.assertFalse(sc["chapter_boundary_crossed"])
        # Newest across the whole universe is the first active-log entry.
        self.assertEqual(sc["entries"][0]["timestamp"], "2026-04-16T12:00:00")

    def test_spanning_flips_chapter_boundary_crossed(self) -> None:
        """offset=0, limit=5 covers 3 active + 2 chapter-02 entries."""
        env = self._call(offset=0, limit=5)
        sc = env["structuredContent"]
        self.assertEqual(len(sc["entries"]), 5)
        self.assertTrue(sc["chapter_boundary_crossed"])
        self.assertEqual(
            [e["timestamp"] for e in sc["entries"]],
            [
                "2026-04-16T12:00:00",
                "2026-04-15T12:00:00",
                "2026-04-14T12:00:00",
                "2026-03-30T12:00:00",  # chapter-02 (highest num first)
                "2026-03-29T12:00:00",
            ],
        )
        self.assertEqual(sc["next_offset"], 5)

    def test_newer_chapter_consumed_first(self) -> None:
        """When spanning, chapter-02 (newer) entries precede chapter-01."""
        env = self._call(offset=3, limit=5)
        sc = env["structuredContent"]
        timestamps = [e["timestamp"] for e in sc["entries"]]
        self.assertEqual(
            timestamps,
            [
                "2026-03-30T12:00:00",  # chapter-02 entry 1
                "2026-03-29T12:00:00",  # chapter-02 entry 2
                "2026-03-28T12:00:00",  # chapter-02 entry 3
                "2026-03-15T12:00:00",  # chapter-01 entry 1
                "2026-03-14T12:00:00",  # chapter-01 entry 2
            ],
        )
        self.assertTrue(sc["chapter_boundary_crossed"])

    def test_exhausted_returns_null_next_offset(self) -> None:
        env = self._call(offset=0, limit=20)
        self.assertEqual(len(env["structuredContent"]["entries"]), 8)
        self.assertIsNone(env["structuredContent"]["next_offset"])

    def test_walnut_with_chapters_but_no_active_log(self) -> None:
        """A walnut whose active log was purged but chapters remain."""
        _make_walnut(self.world, "legacy")
        self.world.write_chapter(
            "legacy",
            1,
            _make_log_with_entries(
                [
                    {"timestamp": "2025-12-10T12:00:00", "squirrel": "a" * 8,
                     "body": "Only chapter entry."}
                ]
            ),
        )
        ctx = _fake_ctx(str(self.world.root))
        env = _run(lt_tools.read_log(ctx, "legacy", 0, 5))
        sc = env["structuredContent"]
        self.assertEqual(len(sc["entries"]), 1)
        self.assertTrue(sc["chapter_boundary_crossed"])
        self.assertEqual(sc["total_chapters"], 1)

    def test_non_chapter_files_in_history_ignored(self) -> None:
        """A README.md inside _kernel/history/ must not count."""
        history = (
            self.world.walnut_path("demo") / "_kernel" / "history"
        )
        (history / "README.md").write_text("notes", encoding="utf-8")
        env = self._call(offset=0, limit=1)
        # We still have exactly 2 chapter files.
        self.assertEqual(env["structuredContent"]["total_chapters"], 2)


# ---------------------------------------------------------------------------
# list_tasks tool tests.
# ---------------------------------------------------------------------------


class ListTasksWalnutScopedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(self.world, "demo")
        # Kernel-level tasks.
        kernel_tasks = {
            "tasks": [
                {"title": "kernel urgent", "priority": "urgent",
                 "status": "active"},
                {"title": "kernel todo", "status": "todo"},
            ]
        }
        (self.world.walnut_path("demo") / "_kernel" / "tasks.json").write_text(
            json.dumps(kernel_tasks), encoding="utf-8"
        )
        # Bundle A tasks.
        self.world.write_bundle(
            "demo",
            "bundles/alpha",
            tasks=[
                {"title": "alpha todo", "status": "todo"},
                {"title": "alpha blocked", "status": "blocked"},
            ],
        )
        # Bundle B tasks.
        self.world.write_bundle(
            "demo",
            "beta",  # v3 flat
            tasks=[
                {"title": "beta done", "status": "done"},
            ],
        )

    def _call(self, walnut: str, bundle: Optional[str] = None) -> dict:
        ctx = _fake_ctx(str(self.world.root))
        return _run(lt_tools.list_tasks(ctx, walnut, bundle))

    def test_walnut_scoped_aggregates_all_tasks(self) -> None:
        env = self._call("demo")
        self.assertFalse(env["isError"], msg=env)
        tasks = env["structuredContent"]["tasks"]
        titles = {t["title"] for t in tasks}
        self.assertEqual(
            titles,
            {
                "kernel urgent",
                "kernel todo",
                "alpha todo",
                "alpha blocked",
                "beta done",
            },
        )

    def test_walnut_scoped_counts_shape(self) -> None:
        env = self._call("demo")
        counts = env["structuredContent"]["counts"]
        self.assertEqual(
            set(counts.keys()),
            {"urgent", "active", "todo", "blocked", "done"},
        )
        self.assertEqual(counts["urgent"], 1)
        self.assertEqual(counts["active"], 1)
        # alpha todo + kernel todo (status=todo); "kernel urgent" has
        # status=active so does NOT add to todo.
        self.assertEqual(counts["todo"], 2)
        self.assertEqual(counts["blocked"], 1)
        self.assertEqual(counts["done"], 1)

    def test_no_world(self) -> None:
        ctx = _fake_ctx(None)
        env = _run(lt_tools.list_tasks(ctx, "demo"))
        self.assertTrue(env["isError"])
        self.assertEqual(env["structuredContent"]["error"], "NO_WORLD")

    def test_walnut_not_found(self) -> None:
        env = self._call("does-not-exist")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "WALNUT_NOT_FOUND"
        )

    def test_path_escape(self) -> None:
        env = self._call("../../escape")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "PATH_ESCAPE"
        )

    def test_walnut_with_no_tasks_returns_empty_with_zero_counts(self) -> None:
        _make_walnut(self.world, "empty")
        env = self._call("empty")
        self.assertFalse(env["isError"], msg=env)
        sc = env["structuredContent"]
        self.assertEqual(sc["tasks"], [])
        self.assertEqual(
            sc["counts"],
            {"urgent": 0, "active": 0, "todo": 0, "blocked": 0, "done": 0},
        )


class ListTasksBundleScopedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _new_world()
        self.addCleanup(self.world.cleanup)
        _make_walnut(self.world, "demo")
        self.world.write_bundle(
            "demo",
            "operations",
            tasks=[
                {"title": "ops task 1", "status": "active"},
                {"title": "ops task 2", "status": "todo"},
            ],
        )
        self.world.write_bundle(
            "demo",
            "other",
            tasks=[{"title": "other task", "status": "todo"}],
        )

    def _call(self, walnut: str, bundle: Optional[str] = None) -> dict:
        ctx = _fake_ctx(str(self.world.root))
        return _run(lt_tools.list_tasks(ctx, walnut, bundle))

    def test_bundle_scoped_returns_only_that_bundle(self) -> None:
        env = self._call("demo", bundle="operations")
        self.assertFalse(env["isError"], msg=env)
        tasks = env["structuredContent"]["tasks"]
        titles = {t["title"] for t in tasks}
        self.assertEqual(titles, {"ops task 1", "ops task 2"})

    def test_bundle_scoped_counts(self) -> None:
        env = self._call("demo", bundle="operations")
        counts = env["structuredContent"]["counts"]
        self.assertEqual(counts["active"], 1)
        self.assertEqual(counts["todo"], 1)

    def test_bundle_not_found(self) -> None:
        env = self._call("demo", bundle="nonsense")
        self.assertTrue(env["isError"])
        self.assertEqual(
            env["structuredContent"]["error"], "BUNDLE_NOT_FOUND"
        )

    def test_bundle_not_found_suggestions(self) -> None:
        env = self._call("demo", bundle="operatonz")  # typo
        self.assertTrue(env["isError"])
        suggestions = env["structuredContent"]["suggestions"]
        self.assertGreaterEqual(len(suggestions), 1)
        self.assertIn("Did you mean", suggestions[0])

    def test_empty_bundle_returns_empty_tasks(self) -> None:
        self.world.write_bundle("demo", "empty-bundle")  # no tasks.json
        env = self._call("demo", bundle="empty-bundle")
        self.assertFalse(env["isError"], msg=env)
        self.assertEqual(env["structuredContent"]["tasks"], [])
        self.assertEqual(
            env["structuredContent"]["counts"],
            {"urgent": 0, "active": 0, "todo": 0, "blocked": 0, "done": 0},
        )


# ---------------------------------------------------------------------------
# Task counts helper.
# ---------------------------------------------------------------------------


class TaskCountsTests(unittest.TestCase):
    def test_counts_empty(self) -> None:
        self.assertEqual(
            lt_tools._task_counts([]),
            {"urgent": 0, "active": 0, "todo": 0, "blocked": 0, "done": 0},
        )

    def test_urgent_is_orthogonal_to_status(self) -> None:
        """A task with priority=urgent + status=active counts in BOTH."""
        counts = lt_tools._task_counts(
            [{"priority": "urgent", "status": "active"}]
        )
        self.assertEqual(counts["urgent"], 1)
        self.assertEqual(counts["active"], 1)

    def test_unknown_status_ignored(self) -> None:
        counts = lt_tools._task_counts([{"status": "weird"}])
        self.assertEqual(
            counts,
            {"urgent": 0, "active": 0, "todo": 0, "blocked": 0, "done": 0},
        )

    def test_non_dict_items_skipped(self) -> None:
        counts = lt_tools._task_counts([None, "stringly", {"status": "todo"}])
        self.assertEqual(counts["todo"], 1)
        # Others unaffected.


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


class RegistrationTests(unittest.TestCase):
    def test_build_server_registers_read_log_and_list_tasks(self) -> None:
        from alive_mcp import server as server_mod  # lazy -- keeps deps light.

        instance = server_mod.build_server()
        # FastMCP exposes registered tools via ``list_tools`` / its
        # internal manager. The simplest invariant check is to make
        # sure both tool names appear via the internal ``_tool_manager``
        # attribute (public surface uses async list_tools which is
        # overkill here).
        tool_manager = instance._tool_manager
        names = set(tool_manager._tools.keys())
        self.assertIn("read_log", names)
        self.assertIn("list_tasks", names)

    def test_read_log_has_read_only_annotation(self) -> None:
        from alive_mcp import server as server_mod

        instance = server_mod.build_server()
        tool = instance._tool_manager._tools["read_log"]
        anns = tool.annotations
        self.assertTrue(anns.readOnlyHint)
        self.assertFalse(anns.destructiveHint)
        self.assertFalse(anns.openWorldHint)

    def test_list_tasks_has_read_only_annotation(self) -> None:
        from alive_mcp import server as server_mod

        instance = server_mod.build_server()
        tool = instance._tool_manager._tools["list_tasks"]
        anns = tool.annotations
        self.assertTrue(anns.readOnlyHint)
        self.assertFalse(anns.destructiveHint)
        self.assertFalse(anns.openWorldHint)


if __name__ == "__main__":
    unittest.main()
