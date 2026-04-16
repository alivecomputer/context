---
name: alive:search-world
description: "The human needs something that exists somewhere in the world but they don't know where. A decision, a person, a file, a reference — it's been captured, they just can't find it. Searches decisions, people, files, references, insights, and log history across all walnuts in priority order."
user-invocable: true
---

# Find

Search across the world. One verb for all retrieval.

---

## How It Searches

Priority order — fastest and highest signal first:

### 1. Frontmatter Scan (fast, structured)
Scan `_kernel/key.md` across all walnuts. Matches on: type, goal, people names, tags, links, reference descriptions.

### 2. Insights Search (standing knowledge)
Scan `_kernel/insights.md` across relevant walnuts. Domain knowledge that persists — "Nova Station test windows are Tue-Thu only."

### 3. Log Search (decisions, history)
Search `_kernel/log.md` entries. Signed decisions, session summaries, what happened when. Frontmatter first (last-entry, summary), then entry bodies.

### 4. Task Search (work queue)
Search tasks across all walnuts using `tasks.py list` with the `--world` and `--search` flags:

```bash
python3 "$ALIVE_PLUGIN_ROOT/scripts/tasks.py" list --world "$WORLD_ROOT" --search "{query}"
```

Returns JSON with tasks matching the query across all walnuts. Each result includes a `walnut` field for attribution. Combine with `--status`, `--priority`, `--assignee`, `--tag` for narrower results.

### 5. Working File Search (drafts)
Scan `*/` across walnuts (bundles are flat in walnut root). Find drafts by name, version, age, squirrel attribution.

### 6. Bundle Manifest Search (captured content metadata)
Search `*/context.manifest.yaml` files across walnuts (bundles are flat in walnut root). Match on frontmatter: type, date, source, participants, subject.

### 7. Raw Reference Search (expensive)
Load actual raw files. Only on explicit request — "read me that email from Jax."

### 8. Context Source Cascade (if nothing found locally)
**If steps 1-7 return nothing and the user believes it exists**, automatically fan out to configured context sources before reporting "not found." Check `.alive/preferences.yaml` `context_sources:` for what's available — any configured MCP server, sync script, or API connection is a valid search target.

One-hop inference applies: if the user says "the setup guide I sent Sarah" and an email integration is configured, that's enough to trigger a search without being asked. The system should resolve across its full context surface — local files are not the only source of truth.

```
╭─ 🐿️ not found in local files
│  Checking configured context sources...
│  → Found in email: sent to Sarah, Mar 15, "Setup Guide v2"
│
│  ▸ What now?
│  1. Read it
│  2. Capture it to a walnut
│  3. Skip
╰─
```

**Never say "not found" if context sources haven't been checked.**

---

## Cross-Walnut Search

Find searches across ALL walnuts by default. Results show which walnut each match came from.

```
╭─ 🐿️ found 3 matches for "radiation shielding"
│
│   1. nova-station / insights.md
│      "Ceramic composites outperform aluminum at 3x the cost"
│
│   2. nova-station / _kernel/log.md — 2026-02-23
│      Decision: go with hybrid shielding approach
│
│   3. nova-station / research/
│      2026-02-23-radiation-shielding-options.md
│
│  number to load, or refine search.
╰─
```

## Connections

When a match is found, surface connected walnuts:

```
╭─ 🐿️ [[ryn-okata]] is mentioned in this entry.
│  She also appears in: nova-station, glass-cathedral
│  Load her context?
╰─
```

## Temporal Queries

"What happened last week" → filter log entries by date range, show across all active walnuts.

"What changed since Tuesday" → scan `_kernel/now.json` updated timestamps + recent log entries.

"History of nova-station" → show `_kernel/log.md` frontmatter (entry count, summary) + offer to load recent entries.
