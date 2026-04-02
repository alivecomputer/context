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
Use `tasks.py list` to query tasks across walnuts. Find tasks by status, age, attribution.

### 5. Working File Search (drafts)
Scan `*/` across walnuts (bundles are flat in walnut root). Find drafts by name, version, age, squirrel attribution.

### 6. Bundle Manifest Search (captured content metadata)
Search `*/context.manifest.yaml` files across walnuts (bundles are flat in walnut root). Match on frontmatter: type, date, source, participants, subject.

### 7. Raw Reference Search (last resort, expensive)
Load actual raw files. Only on explicit request — "read me that email from Jax."

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
│   3. nova-station / bundles/research/
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
