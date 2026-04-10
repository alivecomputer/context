"""ALIVE Context System memory plugin for Hermes Agent.

File-based personal context manager. Walnuts are structured context units
with identity (key.md), history (log.md), domain knowledge (insights.md),
and current state (now.json). Zero dependencies -- reads/writes plain files.

Architecture (from the ALIVE x Hermes design spec):
  Layer 1: Memory Provider (this file)
    - Lean system prompt (~50 tokens)
    - Smart prefetch (inject at transitions, not every turn)
    - 3 autonomous tools: alive_load, alive_world, alive_search
    - on_session_end: persist stash + write squirrel YAML
    - on_pre_compress: flag for re-brief on next turn
    - on_memory_write: route built-in writes to walnut insights

Install: claude plugin install alive@alivecontext
Docs: https://github.com/alivecontext/alive
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# World detection
# ---------------------------------------------------------------------------

def _find_world_root() -> Optional[Path]:
    """Find the ALIVE world root by checking common locations."""
    # Explicit env var
    env_root = os.environ.get("ALIVE_WORLD_ROOT", "")
    if env_root:
        p = Path(env_root).expanduser()
        if (p / ".alive").is_dir():
            return p

    # ~/world symlink (recommended setup)
    home_world = Path.home() / "world"
    if home_world.exists():
        resolved = home_world.resolve()
        if (resolved / ".alive").is_dir():
            return resolved

    # iCloud default (macOS)
    icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "alive"
    if (icloud / ".alive").is_dir():
        return icloud

    # Walk up from cwd
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".alive").is_dir():
            return parent

    return None


def _read_file(path: Path, limit: int = 0) -> str:
    """Read a file, optionally limiting to first N lines."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if limit > 0:
            lines = text.split("\n")
            return "\n".join(lines[:limit])
        return text
    except Exception:
        return ""


def _read_json(path: Path) -> dict:
    """Read a JSON file, return empty dict on failure."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    """Write a JSON file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.rename(path)


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter as a simple dict (no PyYAML dependency)."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    fm = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


# ---------------------------------------------------------------------------
# Walnut discovery and search
# ---------------------------------------------------------------------------

def _find_walnuts(world_root: Path) -> List[Dict[str, str]]:
    """Find all walnuts in the world by looking for _kernel/key.md."""
    walnuts = []
    for key_file in world_root.rglob("_kernel/key.md"):
        walnut_dir = key_file.parent.parent
        rel = walnut_dir.relative_to(world_root)
        # Skip archive
        if str(rel).startswith("01_Archive"):
            continue
        # Read goal + rhythm from frontmatter
        text = _read_file(key_file, limit=25)
        fm = _parse_frontmatter(text)
        goal = fm.get("goal", "")
        rhythm = fm.get("rhythm", "")

        # Check now.json for health signal
        now = _read_json(walnut_dir / "_kernel" / "now.json")
        updated = now.get("updated", "")
        phase = now.get("phase", "")

        # Health calculation
        health = "unknown"
        if updated and rhythm:
            try:
                updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                days_since = (now_dt - updated_dt).days
                rhythm_days = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 30}.get(rhythm, 7)
                if days_since <= rhythm_days:
                    health = "active"
                elif days_since <= rhythm_days * 2:
                    health = "quiet"
                else:
                    health = "waiting"
            except Exception:
                pass

        walnuts.append({
            "name": walnut_dir.name,
            "path": str(rel),
            "goal": goal,
            "phase": phase,
            "health": health,
            "updated": updated,
        })
    return walnuts


def _search_logs(world_root: Path, query: str, max_results: int = 10) -> List[Dict]:
    """Search across all walnut logs for a query string."""
    results = []
    query_lower = query.lower()
    for log_file in world_root.rglob("_kernel/log.md"):
        walnut_dir = log_file.parent.parent
        rel = walnut_dir.relative_to(world_root)
        if str(rel).startswith("01_Archive"):
            continue
        text = _read_file(log_file)
        if not text:
            continue
        entries = re.split(r"(?=^## )", text, flags=re.MULTILINE)
        for entry in entries:
            if query_lower in entry.lower():
                snippet = entry[:500].strip()
                results.append({
                    "walnut": walnut_dir.name,
                    "path": str(rel),
                    "match": snippet,
                })
                if len(results) >= max_results:
                    return results
    return results


def _search_insights(world_root: Path, query: str, max_results: int = 10) -> List[Dict]:
    """Search across all walnut insights for a query string."""
    results = []
    query_lower = query.lower()
    for insights_file in world_root.rglob("_kernel/insights.md"):
        walnut_dir = insights_file.parent.parent
        rel = walnut_dir.relative_to(world_root)
        if str(rel).startswith("01_Archive"):
            continue
        text = _read_file(insights_file)
        if query_lower in text.lower():
            results.append({
                "walnut": walnut_dir.name,
                "path": str(rel),
                "match": text[:500].strip(),
            })
            if len(results) >= max_results:
                return results
    return results


def _search_keys(world_root: Path, query: str, max_results: int = 10) -> List[Dict]:
    """Search walnut identities (key.md) for a query string."""
    results = []
    query_lower = query.lower()
    for key_file in world_root.rglob("_kernel/key.md"):
        walnut_dir = key_file.parent.parent
        rel = walnut_dir.relative_to(world_root)
        if str(rel).startswith("01_Archive"):
            continue
        text = _read_file(key_file)
        if query_lower in text.lower():
            results.append({
                "walnut": walnut_dir.name,
                "path": str(rel),
                "match": text[:500].strip(),
            })
            if len(results) >= max_results:
                return results
    return results


# ---------------------------------------------------------------------------
# Walnut briefing builder
# ---------------------------------------------------------------------------

def _build_walnut_briefing(world_root: Path, walnut_rel: str) -> str:
    """Build a full walnut briefing from kernel files.

    This is what gets injected at session start, walnut switch,
    and post-compression. Equivalent to the squirrel's brief pack.
    """
    walnut_path = world_root / walnut_rel
    parts = []

    # key.md -- identity
    key = _read_file(walnut_path / "_kernel" / "key.md")
    if key:
        parts.append(f"## Identity\n{key}")

    # now.json -- current state
    now = _read_json(walnut_path / "_kernel" / "now.json")
    if now:
        state_lines = []
        phase = now.get("phase", "")
        if phase:
            state_lines.append(f"Phase: {phase}")

        next_action = now.get("next", {})
        if isinstance(next_action, dict):
            action = next_action.get("action", "")
            why = next_action.get("why", "")
            if action:
                state_lines.append(f"Next: {action}")
            if why:
                state_lines.append(f"Why: {why}")
        elif isinstance(next_action, str) and next_action:
            state_lines.append(f"Next: {next_action}")

        blockers = now.get("blockers", [])
        if blockers:
            state_lines.append(f"Blockers: {', '.join(blockers)}")

        context = now.get("context", "")
        if context:
            state_lines.append(f"\n{context[:800]}")

        # Bundle summary
        bundles = now.get("bundles", {})
        summary = bundles.get("summary", {})
        if summary:
            state_lines.append(
                f"\nBundles: {summary.get('total', 0)} total "
                f"({summary.get('active', 0)} active, "
                f"{summary.get('draft', 0)} draft, "
                f"{summary.get('done', 0)} done)"
            )

        # Active bundles with tasks
        active = bundles.get("active", {})
        if isinstance(active, dict):
            for bname, bdata in active.items():
                if isinstance(bdata, dict):
                    bstatus = bdata.get("status", "")
                    bgoal = bdata.get("goal", "")
                    tasks = bdata.get("tasks", {})
                    counts = tasks.get("counts", {})
                    urgent = tasks.get("urgent", [])
                    line = f"  [{bname}] {bstatus} -- {bgoal}"
                    if counts:
                        line += f" ({counts.get('urgent', 0)}u/{counts.get('active', 0)}a/{counts.get('todo', 0)}t)"
                    state_lines.append(line)
                    for u in urgent:
                        if u:
                            state_lines.append(f"    ! {u}")

        if state_lines:
            parts.append(f"## Current State\n" + "\n".join(state_lines))

    # insights.md -- frontmatter only (section names)
    insights = _read_file(walnut_path / "_kernel" / "insights.md", limit=20)
    if insights:
        fm = _parse_frontmatter(insights)
        sections = fm.get("sections", "")
        if sections:
            parts.append(f"## Domain Knowledge Sections\n{sections}")

    # log.md -- most recent entry only
    log = _read_file(walnut_path / "_kernel" / "log.md")
    if log:
        log_parts = log.split("---", 2)
        body = log_parts[2] if len(log_parts) >= 3 else log
        entries = re.split(r"(?=^## )", body.strip(), flags=re.MULTILINE)
        recent = [e for e in entries if e.strip()]
        if recent:
            parts.append(f"## Most Recent Log Entry\n{recent[0][:600].strip()}")

    if not parts:
        return ""

    header = f"# ALIVE Walnut: {Path(walnut_rel).name}"
    return header + "\n\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool schemas (spec: 3 tools)
# ---------------------------------------------------------------------------

LOAD_SCHEMA = {
    "name": "alive_load",
    "description": (
        "Load an ALIVE walnut's context -- identity, current state, active bundles, "
        "recent history. Use at conversation start, when switching topics, or when "
        "a walnut is mentioned by name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "walnut": {
                "type": "string",
                "description": "Walnut name or path (e.g. 'alivecomputer', '04_Ventures/lock-in-lab')",
            },
        },
        "required": ["walnut"],
    },
}

WORLD_SCHEMA = {
    "name": "alive_world",
    "description": (
        "List all active walnuts in the ALIVE world with health signals. "
        "Shows name, goal, phase, health (active/quiet/waiting), and domain. "
        "Use for agent orientation or when the user asks 'what am I working on'."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SEARCH_SCHEMA = {
    "name": "alive_search",
    "description": (
        "Search across all ALIVE walnuts -- logs, insights, and identities. "
        "Finds past decisions, people context, domain knowledge, and references. "
        "Use to verify past context before asserting what happened."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "scope": {
                "type": "string",
                "description": "Where to search: 'all' (default), 'logs', 'insights', 'walnuts'.",
                "enum": ["all", "logs", "insights", "walnuts"],
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class AliveMemoryProvider(MemoryProvider):
    """ALIVE Context System -- file-based personal context manager.

    Smart prefetch architecture:
      - Turn 0 (session start): inject full walnut briefing
      - Walnut switch: inject new walnut's briefing
      - Post-compression: re-inject current walnut briefing
      - Normal working turns: empty (context already in conversation)
    """

    def __init__(self):
        self._world_root: Optional[Path] = None
        self._active_walnut: Optional[str] = None  # relative path from world root
        self._session_id: str = ""
        self._session_uuid: str = ""  # 8-char hex for signing
        self._stash: List[Dict] = []
        self._stash_lock = threading.Lock()

        # Smart prefetch state
        self._turn_count: int = 0
        self._needs_briefing: bool = True  # True at session start
        self._last_briefed_walnut: Optional[str] = None
        self._briefing_cache: str = ""

        # Session timing
        self._started_at: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Cron guard
        self._cron_skipped: bool = False

    @property
    def name(self) -> str:
        return "alive"

    def is_available(self) -> bool:
        return _find_world_root() is not None

    def get_config_schema(self):
        return [
            {
                "key": "world_root",
                "description": "Path to your ALIVE world (folder containing .alive/)",
                "default": str(_find_world_root() or "~/world"),
                "env_var": "ALIVE_WORLD_ROOT",
            },
        ]

    def save_config(self, values, hermes_home):
        """Write ALIVE config to $HERMES_HOME/alive.json."""
        config_path = Path(hermes_home) / "alive.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception:
                pass
        existing.update(values)
        config_path.write_text(json.dumps(existing, indent=2))

    def initialize(self, session_id: str, **kwargs) -> None:
        # Cron guard -- skip for background/flush contexts
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")
        if agent_context in ("cron", "flush") or platform == "cron":
            logger.debug("ALIVE skipped: cron/flush context")
            self._cron_skipped = True
            return

        self._session_id = session_id
        self._session_uuid = uuid.uuid4().hex[:8]
        self._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._world_root = _find_world_root()

        if not self._world_root:
            logger.warning("ALIVE world not found. Set ALIVE_WORLD_ROOT env var.")
            return

        # Try to detect active walnut from cwd
        cwd = Path.cwd()
        try:
            rel = cwd.relative_to(self._world_root)
            parts = list(rel.parts)
            for i in range(len(parts), 0, -1):
                candidate = self._world_root / Path(*parts[:i])
                if (candidate / "_kernel" / "key.md").exists():
                    self._active_walnut = str(Path(*parts[:i]))
                    break
        except ValueError:
            pass

        # Check for unrouted stash from previous sessions
        stash_path = self._world_root / ".alive" / "stash.json"
        stash_data = _read_json(stash_path)
        pending = stash_data.get("items", [])
        if pending:
            logger.info("ALIVE: %d unrouted stash items from previous sessions", len(pending))

        logger.info("ALIVE initialized. World: %s, Active: %s, Session: %s",
                     self._world_root, self._active_walnut or "(none)", self._session_uuid)

    def system_prompt_block(self) -> str:
        """Lean static block (~50 tokens). Announces ALIVE + tool names."""
        if self._cron_skipped or not self._world_root:
            return ""

        lines = [
            "# ALIVE Context System",
            "Personal context manager active. Walnuts structure your projects, people, and knowledge.",
            "Tools: alive_load (load walnut), alive_world (list all), alive_search (find context).",
        ]

        if self._active_walnut:
            lines.append(f"Active walnut: {Path(self._active_walnut).name}")

        # Surface unrouted stash count
        stash_path = self._world_root / ".alive" / "stash.json"
        stash_data = _read_json(stash_path)
        pending = stash_data.get("items", [])
        if pending:
            lines.append(f"Unrouted stash: {len(pending)} items from previous sessions.")

        return "\n".join(lines)

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Track turn count for smart prefetch."""
        self._turn_count = turn_number

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Smart prefetch -- inject context only at transitions.

        Injection triggers:
          1. Session start (turn 0)
          2. Walnut switch (active walnut changed since last brief)
          3. Post-compression (flag set by on_pre_compress)
          4. Orientation query (user asks "what am I working on")

        Normal working turns: empty string.
        """
        if self._cron_skipped or not self._world_root:
            return ""

        # Trigger 1: Session start
        if self._turn_count == 0 and self._needs_briefing:
            return self._inject_briefing()

        # Trigger 2: Walnut switch
        if self._active_walnut and self._active_walnut != self._last_briefed_walnut:
            return self._inject_briefing()

        # Trigger 3: Post-compression re-brief
        if self._needs_briefing:
            return self._inject_briefing()

        # Trigger 4: Orientation query detection
        if query:
            orientation_signals = [
                "what am i working on",
                "what's happening",
                "where was i",
                "catch me up",
                "what's the status",
                "show me the world",
                "what's active",
            ]
            query_lower = query.lower()
            if any(signal in query_lower for signal in orientation_signals):
                return self._inject_briefing()

        # Normal working turn -- nothing to inject
        return ""

    def _inject_briefing(self) -> str:
        """Build and inject the walnut briefing, update state."""
        if not self._active_walnut:
            # No walnut active -- inject world overview
            walnuts = _find_walnuts(self._world_root)
            active_walnuts = [w for w in walnuts if w["health"] == "active"]

            lines = ["## ALIVE World Overview"]
            lines.append(f"Total walnuts: {len(walnuts)}, Active: {len(active_walnuts)}")
            for w in active_walnuts[:10]:
                lines.append(f"  [{w['name']}] {w['phase']} -- {w['goal'][:80]}")

            self._needs_briefing = False
            return "\n".join(lines)

        briefing = _build_walnut_briefing(self._world_root, self._active_walnut)
        self._last_briefed_walnut = self._active_walnut
        self._needs_briefing = False
        self._briefing_cache = briefing
        return briefing

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """No background prefetch needed -- ALIVE reads local files (instant)."""
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """No-op. ALIVE captures explicitly via the stash mechanic, not every turn."""
        pass

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Before compression, flag for re-brief and preserve context."""
        self._needs_briefing = True  # Next prefetch will re-inject full briefing

        if not self._active_walnut:
            return ""

        parts = [
            f"ALIVE walnut '{Path(self._active_walnut).name}' is active.",
            "Preserve: walnut name, active bundle, any decisions/tasks/insights from conversation.",
        ]

        # Include stash items so they survive compression
        with self._stash_lock:
            if self._stash:
                parts.append(f"Pending stash ({len(self._stash)} items):")
                for item in self._stash:
                    parts.append(f"  - [{item.get('type', 'note')}] {item.get('content', '')[:100]}")

        return " ".join(parts)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Intercept built-in memory writes. Route to walnut insights when appropriate.

        Built-in MEMORY.md stores flat facts. When ALIVE is active, some of those
        facts belong in walnut insights instead. We stash them for routing at save.
        """
        if self._cron_skipped or not self._world_root:
            return
        if action != "add" or not content:
            return

        # Stash the memory write for potential routing to insights
        with self._stash_lock:
            self._stash.append({
                "type": "insight_candidate",
                "content": content,
                "source": f"builtin_memory_{target}",
                "walnut": self._active_walnut or "",
                "time": datetime.now(timezone.utc).isoformat(),
            })
        logger.debug("ALIVE: intercepted memory write, stashed for routing: %s", content[:80])

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Persist unrouted stash to .alive/stash.json. Write squirrel YAML entry."""
        if self._cron_skipped or not self._world_root:
            return

        # 1. Persist stash
        with self._stash_lock:
            stash_items = list(self._stash)

        if stash_items:
            stash_path = self._world_root / ".alive" / "stash.json"
            existing = _read_json(stash_path)
            existing_items = existing.get("items", [])
            existing_items.extend(stash_items)
            _write_json(stash_path, {
                "items": existing_items,
                "updated": datetime.now(timezone.utc).isoformat(),
            })
            logger.info("ALIVE: persisted %d stash items to %s", len(stash_items), stash_path)

        # 2. Write squirrel YAML entry
        squirrel_dir = self._world_root / ".alive" / "_squirrels"
        squirrel_dir.mkdir(parents=True, exist_ok=True)
        squirrel_path = squirrel_dir / f"{self._session_uuid}.yaml"

        # Build YAML manually (no PyYAML dependency)
        ended_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"session_id: {self._session_uuid}",
            f"runtime_id: squirrel.hermes@1.0",
            f"engine: hermes-agent",
            f"walnut: {self._active_walnut or '(none)'}",
            f"started: {self._started_at}",
            f"ended: {ended_at}",
            f"signed: true",
            f"saves: 0",
            f"platform: hermes",
        ]

        # Stash summary
        if stash_items:
            lines.append("stash:")
            for item in stash_items:
                content = item.get("content", "").replace('"', '\\"')[:120]
                itype = item.get("type", "note")
                walnut = item.get("walnut", "")
                lines.append(f'  - content: "{content}"')
                lines.append(f"    type: {itype}")
                if walnut:
                    lines.append(f"    routed: {walnut}")
        else:
            lines.append("stash: []")

        # Recovery state
        if self._active_walnut:
            lines.append(f'recovery_state: "session ended, active walnut: {self._active_walnut}"')
        else:
            lines.append('recovery_state: "session ended, no walnut loaded"')

        squirrel_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("ALIVE: squirrel entry written to %s", squirrel_path)

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        """Observe subagent results. Stash any decisions or findings."""
        if self._cron_skipped or not result:
            return
        # Only stash if the result seems substantive (>100 chars)
        if len(result) > 100:
            with self._stash_lock:
                self._stash.append({
                    "type": "note",
                    "content": f"Subagent result: {result[:300]}",
                    "source": f"delegation:{child_session_id[:8]}",
                    "walnut": self._active_walnut or "",
                    "time": datetime.now(timezone.utc).isoformat(),
                })

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if self._cron_skipped:
            return []
        return [LOAD_SCHEMA, WORLD_SCHEMA, SEARCH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._cron_skipped:
            return json.dumps({"error": "ALIVE is not active (cron context)."})
        if not self._world_root:
            return json.dumps({"error": "ALIVE world not found. Set ALIVE_WORLD_ROOT."})

        if tool_name == "alive_load":
            return self._handle_load(args)
        elif tool_name == "alive_world":
            return self._handle_world()
        elif tool_name == "alive_search":
            return self._handle_search(args)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    def _handle_load(self, args: dict) -> str:
        """Load a walnut's full context. Sets it as active."""
        walnut_name = args.get("walnut", "")
        if not walnut_name:
            return json.dumps({"error": "Missing walnut name."})

        # Find the walnut -- try exact path first, then search by name
        walnut_path = self._world_root / walnut_name
        if not (walnut_path / "_kernel" / "key.md").exists():
            for key_file in self._world_root.rglob("_kernel/key.md"):
                if key_file.parent.parent.name == walnut_name:
                    walnut_path = key_file.parent.parent
                    break
            else:
                return json.dumps({"error": f"Walnut '{walnut_name}' not found."})

        rel = str(walnut_path.relative_to(self._world_root))
        self._active_walnut = rel

        # Build full briefing
        briefing = _build_walnut_briefing(self._world_root, rel)
        self._last_briefed_walnut = rel
        self._needs_briefing = False
        self._briefing_cache = briefing

        return json.dumps({
            "walnut": walnut_path.name,
            "path": rel,
            "briefing": briefing,
        }, indent=2, default=str)

    def _handle_world(self) -> str:
        """List all active walnuts with health signals."""
        walnuts = _find_walnuts(self._world_root)

        # Group by domain
        domains = {}
        for w in walnuts:
            parts = w["path"].split("/")
            domain = parts[0] if parts else "other"
            if domain not in domains:
                domains[domain] = []
            domains[domain].append(w)

        return json.dumps({
            "total": len(walnuts),
            "active_walnut": self._active_walnut,
            "domains": domains,
        }, indent=2, default=str)

    def _handle_search(self, args: dict) -> str:
        """Search across all walnuts -- logs, insights, identities."""
        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "Missing query."})

        scope = args.get("scope", "all")
        results = {}

        if scope in ("all", "walnuts"):
            matches = _search_keys(self._world_root, query)
            if matches:
                results["walnuts"] = matches[:10]

        if scope in ("all", "logs"):
            log_matches = _search_logs(self._world_root, query)
            if log_matches:
                results["logs"] = log_matches

        if scope in ("all", "insights"):
            insight_matches = _search_insights(self._world_root, query)
            if insight_matches:
                results["insights"] = insight_matches

        if not results:
            return json.dumps({"result": "No matches found."})

        return json.dumps(results, indent=2, default=str)

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register ALIVE as a memory provider plugin."""
    ctx.register_memory_provider(AliveMemoryProvider())
