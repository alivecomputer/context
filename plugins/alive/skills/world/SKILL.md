---
name: alive:world
description: "The human doesn't know what to work on, or wants to see everything at once. They need the big picture — what's active, what's stale, what needs attention. Renders a live world view grouped by ALIVE domain, then routes to open, tidy, find, history, or map."
user-invocable: true
---

# World

This is Mission Control. When the human opens their world, it should feel like booting an operating system — everything they care about, at a glance, with clear paths to action.

NOT a database dump. NOT a flat list. A living view of their world, grouped by what matters, showing relationships, surfacing what needs attention.

---

## Load Sequence

1. **Read the injected `<WORLD_INDEX>`** — it's already in your session context from the SessionStart hook. Contains every walnut's type, goal, phase, rhythm, updated, people, links, tags, bundles, and parent relationships. Zero file reads needed. If `<WORLD_INDEX>` is not in context, fall back to reading `.alive/_index.yaml` directly.
2. **If no index exists at all** — generate it first (`python3 "$ALIVE_PLUGIN_ROOT/scripts/generate-index.py" "$WORLD_ROOT"`), then read the output. Fall back to manual scanning only on first-time setup before the index infrastructure exists.
3. **Freshness check** — read the `generated:` timestamp from the index. Display it in the dashboard header. If older than 10 minutes, show a warning. If older than 1 hour, suggest regeneration. This makes index staleness visible instead of invisible.
4. Build the tree from the index — parent/child relationships from `parent:` field
5. **Lightweight fresh checks** — one Bash call each, no subagents, no Explore agents:
   - **Unsaved sessions with stash:** already in the index as `unsaved_with_stash:`. If non-zero, surface in the Attention section. No bash loop needed.
   - **Unrouted inputs:** resolve the world root via the doctor JSON surface, then list the absolute path. Never use a relative `ls 03_Inbox/` — it silently fails when the Bash tool's cwd isn't the world root. One-liner:
     ```bash
     WR=$(alive doctor --check=world-root --json | jq -r '.world_root') && [ -n "$WR" ] && ls "$WR/03_Inbox/" 2>/dev/null | grep -v '^\.' | grep -v '^Icon'
     ```
     Doctor's pinned-strategy result is the authoritative resolver — preferred over raw config-file reads, which mangle paths with internal whitespace. Just the filenames, no deep reads.
   - **API context:** only if context sources are listed in the session start injection (already in your context from the hook — do NOT re-read preferences.yaml).
6. Compute attention items from fresh checks + index staleness signals
7. **Inbox triage (background)** — if `03_Inbox/` has items, dispatch a background agent to triage them. Don't wait for it — render the dashboard immediately, the triage results arrive while the human reads.

### Inbox Triage Agent

Dispatch with `run_in_background: true` when inbox has 1+ items. The agent:

1. Reads the subagent brief from the plugin templates (for ALIVE context)
2. Lists all files in `03_Inbox/` with `ls -la`
3. For each item, determines:
   - **Type:** transcript, email, document, screenshot, video, extraction directory, financial, unknown
   - **Likely destination walnut:** match against the world index (injected in the agent prompt) by keywords, people names, project names
   - **Priority:** urgent (contains decisions/deadlines), normal, low (reference material)
   - **Age:** how old is the file
4. Returns a structured triage report

When the background agent completes, surface the results:

```
╭─ 🐿️ inbox triaged (8 items)
│
│  Urgent
│   march-expenses.csv              → finance (transactions, needs review)
│   error-log-april-2.txt           → my-startup (build error from deploy)
│
│  Route
│   team-dinner-recap.mp4           → my-startup (event footage)
│   fathom-extraction/              → runs via /alive:mine-for-context
│   otter-extraction/               → runs via /alive:mine-for-context
│
│  Auto-route (low priority)
│   gmail/                          → capture via sync script
│   slack/                          → capture via sync script
│
│  ▸ Route all? Or review one at a time?
│  1. Route all suggested
│  2. Review each
│  3. Skip for now
╰─
```

The triage agent gets the world index in its prompt so it knows every walnut, person, and active bundle. It matches by name, keywords, and file type patterns. It does NOT move files — it suggests. The human confirms.

**DO NOT read preferences.yaml** — it's already injected at session start. **DO NOT read individual walnut files** (key.md, now.json, log.md) — the index has everything. **DO NOT read .alive/_squirrels/*.yaml files** — recent sessions are in the index under `recent_sessions:` and unsaved stash count is in `unsaved_with_stash:`. **DO NOT spawn Explore agents or subagents** for the dashboard — use the index and the one bash check above. The entire dashboard should render from data already in context plus 1 fast bash call (inputs listing).

## State Detection

Before rendering, detect system state:

- **Fresh install** (no walnuts exist) → route to `setup.md`
- **Previous system detected** (v3/v4 `_brain/` folders exist) → offer migration via `/alive:create-walnut` migrate mode
- **Normal** → render dashboard

---

## Dashboard Layout

The dashboard has 4 sections. Each tells you something different.

### Section 1: Right Now

What needs the human's attention TODAY. Not everything — just what's active and demanding.

```
╭─ 🐿️ your world
│
│  RIGHT NOW
│  ──────────────────────────────────────────────
│
│   1. my-startup              launching
│      Urgent: 1 · Active: 3
│      Last: 2 hours ago · 6 sessions this week
│
│   2. freelance-agency        legacy
│      Urgent: 3 · Active: 0
│      Last: 2 days ago
│      People: Jake Chen, Sarah Mills
│
│   3. social-content          building
│      Urgent: 0 · Active: 8
│      ⚠ 4 days past rhythm
│
╰─
```

Only show walnuts that are `active` or past their rhythm. Sort by most recently touched. Show:
- Phase
- Urgent + active task counts (from the index's `task_counts` payload, which mirrors `_kernel/now.json` `unscoped_tasks.counts`)
- Last activity (relative time)
- People involved (from `_kernel/key.md` — max 2-3 names)
- Warning if past rhythm

### Section 2: Attention

Things that need your decision or action. Not walnuts — specific issues.

```
╭─ 🐿️ attention
│
│   → 3 unread emails from Orion (Gmail, 2 days)
│   → Unsaved session on nova-station (squirrel:a3f7, 6 stash items)
│   → 03_Inbox/ has 2 items older than 48 hours
│   → flux-engine quiet for 12 days (rhythm: weekly)
│   → 4 working files older than 30 days across 3 walnuts
│
╰─
```

Sources:
- **Inputs buffer (HIGH PRIORITY)** — anything in `03_Inbox/` older than 48 hours. These are unrouted context that could impact active walnuts TODAY. The squirrel should stress this to the human: "You have unrouted inputs. These might contain decisions, tasks, or context that affects your active work. Route them before diving into a walnut."
- API context (Gmail unread, Slack mentions, Calendar upcoming)
- Unsaved sessions with stash items (saves: 0)
- Stale walnuts (quiet/waiting)
- Stale working files

**Inputs triage:** The world skill should understand that inputs are a buffer — content arrives there and needs routing to its proper walnut. When surfacing inputs, the squirrel should scan the context.manifest.yaml frontmatter (if manifests exist) or the file names to understand what the content might relate to. Don't digest the full content — just flag it, estimate which walnuts it might affect, and urge the human to route it. Use `alive:capture-context` to process each input properly.

### Section 3: Your World (the tree)

The full structure — grouped by ALIVE domain, with parent/child nesting visible.

```
╭─ 🐿️ your world
│
│  LIFE
│   identity           active     LinkedIn bio update
│   health             quiet      ADHD assessment follow-up
│   finance            quiet      ⚠ 10 days — subscriptions review
│   people/
│     jake-chen        updated 2 days ago
│     sarah-mills      updated 1 day ago
│     tom              updated 5 days ago
│
│  VENTURES
│   my-startup         launching  MVP demo + investor deck
│     └ mobile-app     building   React Native prototype
│   freelance-agency   legacy     Closing out client contracts
│
│  EXPERIMENTS
│   social-content     building   Content calendar + Buffer queue     3 bundles · 4 tasks
│   side-project       waiting    Decide: rewrite or revise
│   podcast            quiet      ⚠ 12 days — episode 4 edit
│   ... +3 more (2 waiting, 1 quiet)
│
│  INBOX
│   2 items (oldest: 4 days)
│
│  ARCHIVE
│   1 walnut (old-portfolio)
│
╰─
```

Key features:
- **Grouped by ALIVE domain** — not a flat list
- **Parent/child nesting** — sub-walnuts indented under parents with `└`
- **People** shown under Life with last-updated
- **Collapse quiet/waiting** — if there are 6+ quiet experiments, show the count not the full list
- **Inputs count** — just how many and how old
- **Archive count** — just the number
- **5-day activity indicator** — `●` dot for each of the last 5 days the walnut was touched. Visual pulse at a glance.

```
│   nova-station          ●●●●● building   Orbital test suite
│   stellarforge       ●●○○○ launching   Relay satellites
│   side-project     ○○○○○ waiting     Decide: rewrite or revise
```

`●` = touched that day. `○` = no activity. Read left to right: today, yesterday, 2 days, 3 days, 4 days. Five dots tells you this walnut is hot. Zero tells you it's cold. No numbers, no dates — just a visual heartbeat.

### Section 4: Recent Squirrel Activity

What's been happening across the world. A pulse check.

Recent session data is IN the index under `recent_sessions:`. Do NOT read individual squirrel YAML files. Do NOT run bash loops to grep squirrel entries. The index has everything: squirrel ID, walnut, date, bundle, saves count, summary, and tags for the 10 most recent sessions. The index also includes `unsaved_with_stash:` count -- if non-zero, surface it in the Attention section.

```
╭─ 🐿️ recent activity
│
│   Today     nova-station         6 sessions · shipped test harness
│   Yesterday nova-station         refined architecture, 22 decisions
│   Feb 22    stellarforge      infrastructure, telemetry, comms
│   Feb 22    nova-station         companion app, integration tests
│   Feb 21    nova-station         module refactor, ecosystem plan
│
│   5 sessions this week · 3 walnuts touched · 47 stash items routed
│
╰─
```

---

## Rendering Rules

1. **Right Now comes first.** Always. It answers "what should I work on?"
2. **Attention is actionable.** Every item should have a clear next step.
3. **The tree is scannable.** Indent sub-walnuts. Collapse where sensible. Show people under Life.
4. **Recent activity gives pulse.** Not details — just "what's been happening."
5. **Numbers for navigation.** Any walnut with a number can be opened by typing the number.
6. **Don't show everything.** Waiting walnuts can be collapsed. Quiet experiments get a count. The human asks for more if they want it.

---

## Index Freshness

The index regenerates automatically after every save (post-write hook detects `_kernel/now.json` writes). If the index is missing or the human asks for a fresh view, regenerate on demand:

```bash
python3 "$ALIVE_PLUGIN_ROOT/scripts/generate-index.py" "$WORLD_ROOT"
```

After regenerating, re-read `.alive/_index.yaml` to render the updated dashboard.

---

## After Dashboard

- **Number** → open that walnut (invoke `alive:load-context`)
- **"just chat"** → freestyle conversation, no walnut focus
- **"tidy"** → invoke `alive:system-cleanup`
- **"find X"** → invoke `alive:search-world`
- **"history"** → invoke `alive:session-history`
- **"map"** → invoke `alive:my-context-graph`
- **"mine"** → invoke `alive:mine-for-context`
- **"open [name]"** → open a specific walnut
- **Attention item** → address it directly ("deal with those emails", "sign that session")

---

## Context Sources (preferences.yaml)

If `context_sources:` is configured in `.alive/preferences.yaml`, surface relevant items from active sources:

- **mcp_live sources** (Gmail, Slack, Calendar, GitHub): Query on demand. Show actionable items only — "3 unread emails from Orion" not "847 emails."
- **sync_script sources**: Check last sync time. If stale, note it.
- **static_export / markdown_vault**: Don't query at dashboard — these are for `/alive:session-history` and `/alive:search-world`.

Filter by walnut scoping — only show sources where `walnuts: all` or the current active walnut is in the list.

---

## Internal Modes

- `setup.md` — first-time world creation (triggers automatically when no ALIVE structure found)
