---
name: alive:my-context-graph
description: "Render an interactive map of your world. Generates the world index from all walnut and bundle frontmatter, then produces a force-directed graph showing connections between walnuts, people, bundles, and tags. Opens in the browser."
user-invocable: true
---

# Map

Visual overview of the entire ALIVE world — connections, clusters, health.

Not a list (that's the tree in alive:world). Not a search (that's alive:search-world). Map is spatial — it shows how everything connects and where the energy is.

---

## What It Does

### 1. Generate the World Index

Run `.alive/scripts/generate-index.py` to walk all walnuts and collect frontmatter. If the script doesn't exist in the world yet, copy it from the plugin (`scripts/generate-index.py`) or inline the logic.

```bash
python3 .alive/scripts/generate-index.py
```

The script walks all directories for `key.md` files, handling both `_kernel/key.md` (current structure) and root-level `key.md` (flat/legacy structure). It deduplicates entries and skips template walnuts.

**Collected data per walnut:**
- Name, type, goal, phase, rhythm, tags
- People (from `key.md` `people:` field — multi-line list)
- Links (from `key.md` `links:` field — wikilink extraction)
- Parent (from `key.md` `parent:` field)
- Bundles (detected by `*/context.manifest.yaml` in walnut root — name, status, goal)
- Task counts (from `_kernel/now.json` if present)
- Last updated (from `_kernel/now.json` `updated` field)

**Projection tiers:**
The graph supports three projection levels for controlling visual density:
- **Tier 1 — Walnuts only** — just the walnut nodes and their connections. Fast, clean overview.
- **Tier 2 — Walnuts + People** — adds people nodes that bridge multiple walnuts. Shows relationship topology.
- **Tier 3 — Full graph** — walnuts, people, bundles, tags. Maximum detail. Can get dense in large worlds.

Default to Tier 2. Offer toggle controls for Tier 1 (simplified) and Tier 3 (full detail).

**Outputs:**
- `.alive/_index.yaml` — human-readable world index
- `.alive/_index.json` — machine-readable for graph consumption

### 2. Render the Graph

Run `.alive/scripts/generate-graph.py` to read the JSON index and generate an interactive D3.js graph.

```bash
python3 .alive/scripts/generate-graph.py
```

The graph is written to `.alive/context-graph.html` — a self-contained HTML file with embedded data, D3.js from CDN, and custom fonts from Fontshare/Google Fonts.

**Theme:** Alive branded. Light mode default (cream #FAF8F5, orange primary #F97316). Dark mode toggle (forest green #0A1F0D, copper #B87333 accents). Custom fonts: Array (display), Khand (headings), Inter (body).

**Nodes:**
- Walnuts (primary nodes — sized by bundle count and recency)
- People (toggle — shown for people who connect 2+ walnuts)
- Bundles (expandable — click a walnut to show its bundles as orbiting nodes)
- Central node connecting top-level walnuts
- Inputs buffer node showing unrouted count

**Edges:**
- `links:` field connections between walnuts
- `parent:` -> child relationships (dashed)
- Person -> walnut connections (dotted, when people shown)
- Bundle -> parent walnut (when expanded)

**Color by ALIVE domain:**
- Life = blue
- Ventures = orange
- Experiments = green
- Archive = warm gray
- People = purple
- Inputs = red

Colors adapt per theme (lighter in dark mode for visibility).

**Size by activity:**
- 15+ bundles = largest (20px)
- 5+ bundles = large (15px)
- Updated in last 2 days = medium-large (12px)
- Updated in last week = medium (9px)
- Stale = small (5px)

**Health signals visible:**
- Active (recent) = full opacity, glow on today's updates
- Quiet (1-2 weeks) = reduced opacity
- Waiting (2+ weeks) = dim, small
- Bundle-heavy walnuts get outer glow rings

### 3. Open in Browser

```
╭─ squirrel map generated
│
│  58 walnuts, 78 people, 41 bundles
│  60 nodes, 36 links, 11 people connectors
│
│  > Opening in browser...
╰─
```

---

## Graph Features

### Interactive Controls

- **Hover node** -> tooltip with goal, phase, bundle count, next action
- **Hover node** -> highlight all connected nodes and edges, dim everything else
- **Click walnut** -> expand bundles as orbiting nodes + open details panel
- **Click node** -> pin details panel (right sidebar) with full context
- **Drag** -> reposition nodes (physics simulation)
- **Zoom + pan** -> navigate large worlds
- **Hover edge** -> show connection type label (linked / parent -> child / person name)
- **Esc** -> close details panel

### Details Panel

Click any node to open a pinned side panel showing:
- Name, domain badge, phase badge
- Goal description
- Metadata (rhythm, last updated, days since, session count)
- Next action (highlighted)
- Bundle list with status badges (draft/prototype/published/done)
- Active bundle highlighted in primary color
- People list
- Tags

### Search

- **`/` hotkey** -> focus search box
- Searches walnut names, goals, tags, and people
- Matching nodes highlighted, everything else dims
- **Esc** -> clear search

### Theme Toggle

- Light mode default (Alive cream branding)
- Dark mode toggle (forest green + copper)
- Theme persists via localStorage
- Sun/moon toggle in header

### Controls

- **reset view** -> zoom to fit
- **show people** -> toggle people connector nodes
- **labels on/off** -> toggle node labels
- **show archive** -> toggle archived walnuts
- **cluster** -> group nodes by ALIVE domain with background labels
- **projection tier** -> switch between Tier 1/2/3

### Bundle Expansion

Click any walnut with bundles to expand them as orbiting nodes:
- Bundle nodes sized by active status (active bundle = larger)
- Bundle color by status: draft (gray), prototype (amber), done (green)
- Active bundle has a highlighted stroke
- Click the walnut again to collapse
- Bundle details shown in the details panel

---

## Files

| File | Purpose |
|------|---------|
| `.alive/_index.yaml` | Generated world index — human-readable, all frontmatter |
| `.alive/_index.json` | Generated world index — JSON for graph consumption |
| `.alive/context-graph.html` | Interactive D3.js graph — self-contained, Alive branded |
| `.alive/scripts/generate-index.py` | Index generator (walks tree, reads frontmatter, outputs YAML + JSON) |
| `.alive/scripts/generate-graph.py` | Graph generator (reads JSON index, outputs branded HTML with D3.js) |

**Plugin source:** Both scripts ship with the plugin at `scripts/generate-index.py` and `scripts/generate-graph.py`. On first `alive:my-context-graph` invocation, copy them to `.alive/scripts/` if not already present.

---

## Regeneration

The index and graph should be regenerated:
- On every `alive:my-context-graph` invocation (always fresh)
- Suggested after `alive:save` when structural changes occurred (new walnut, new bundle, new person)
- After `alive:system-cleanup` resolves issues that affect the graph (broken links, orphan walnuts)

```
╭─ squirrel world changed — regenerate map?
│  New walnut created: flux-engine
│  2 new bundles, 1 new person
│
│  > Regenerate / Skip
╰─
```

---

## What Map Is NOT

- Not `alive:world` — world is the operational dashboard (what to work on). Map is the spatial view (how things connect).
- Not `alive:search-world` — find retrieves specific content. Map shows the topology.
- Not `alive:system-cleanup` — tidy fixes structural issues. Map visualizes the structure.

World answers "what should I do?" Map answers "what does my world look like?"
