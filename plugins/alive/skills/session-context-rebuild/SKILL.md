---
name: alive:session-context-rebuild
description: "Rebuild a rich context window from multiple previous sessions. Dispatches a swarm of subagents to mine squirrel entries, read actual files touched, extract log history, and merge conflicting session states into one coherent working context. Essential for resuming after time away or merging parallel sessions."
user-invocable: true
---

# Session Context Rebuild

You're returning to work that spans multiple sessions — possibly conflicting ones. The context window is empty but the walnut isn't. This skill dispatches subagents to deeply reconstruct what happened, what changed, where conflicts exist, and what the merged state should be. Then you resume with full context as if you never left.

---

## When to Use

- Returning to a walnut after days/weeks away
- Context was lost to compaction mid-session
- Multiple sessions touched the same walnut in parallel and need merging
- Picking up someone else's work in a shared walnut
- "What was I doing here?" moments

---

## Process

### Phase 1: Scout (single agent)

Dispatch one subagent to map the territory. It calls `alive:session-history` to get the lay of the land, then reports back:

- Which sessions touched this walnut, when, by whom (engine)
- Which files each session worked on (`working:` field in squirrel YAML)
- Which bundles were active
- Whether sessions overlap (parallel/conflicting work)
- Unsaved stash items from crashed sessions

```
╭─ 🐿️ context rebuild — scouting
│  Found 5 sessions on this walnut (last 2 weeks)
│  3 sequential, 2 overlapping (possible conflicts)
│  12 files touched, 2 bundles active
│
│  ▸ Rebuild scope?
│  1. All 5 sessions (full rebuild)
│  2. Last 3 only
│  3. Just the 2 conflicting sessions
╰─
```

### Phase 2: Deep Read (agent swarm)

Based on scope, dispatch parallel subagents — one per session or one per domain:

**Agent assignments:**
- **Log agent** — reads `_kernel/log.md`, extracts all entries from sessions in scope. Returns decisions, rationale, timestamps.
- **File agents** (1 per bundle/area touched) — reads the actual files listed in `working:` fields. Returns current state, what changed, any draft versions.
- **Task agent** — uses `tasks.py list` to query task state. Returns what's done, what's in progress, what's blocked.
- **People agent** — extracts all person mentions from log entries and stash items. Cross-references with people walnuts if they exist.
- **Conflict agent** (if overlapping sessions detected) — reads both sessions' log entries and file changes side by side. Identifies contradictions: different decisions on the same topic, competing task states, divergent draft versions.

Each agent returns structured findings. They don't summarise — they return the actual context with file paths, timestamps, and quotes.

### Phase 3: Merge & Present

Combine all agent findings into a single reconstructed context:

```
╭─ 🐿️ rebuilt context (5 sessions, 2 conflicts resolved)
│
│  Timeline:
│  +-- 2026-03-10 (session:a8c) — Started competitor research bundle
│  +-- 2026-03-12 (session:b4f) — Shortlisted 3 vendors, shared draft-01 with Sue
│  +-- 2026-03-14 (session:c7d) — CONFLICT with session:d9e (same day)
│  │   c7d: Changed pricing to per-seat model
│  │   d9e: Kept usage-based pricing, added enterprise tier
│  │   -> Recommended merge: per-seat with enterprise tier override
│  +-- 2026-03-18 (session:e2f) — Final draft-03 written
│
│  Current state:
│  Bundle: competitor-research (draft-03)
│  Key file: competitor-research/competitor-research-draft-03.md
│
│  Open tasks (4):
│  - [ ] Vendor site visits
│  - [ ] Final pricing comparison
│  - [ ] Sue's feedback on draft-03 (shared 2026-03-12, no response logged)
│  - [ ] Enterprise tier pricing model (from conflict resolution)
│
│  People: Sue Chen (shared draft), Ryn Okata (mentioned in vendor shortlist)
│
│  Unresolved conflicts (1):
│  - Pricing model: per-seat vs usage-based (sessions c7d/d9e)
│
│  ▸ How to proceed?
│  1. Accept merged context, resume working
│  2. Resolve conflicts first
│  3. Show me the raw findings from each agent
╰─
```

### Phase 4: Resume

On confirmation:
- The squirrel now holds full reconstructed context
- Unresolved conflicts are stashed as decisions-needed
- Any conflict resolutions are logged as decisions
- Work continues normally — stash, save, the whole flow

---

## Conflict Detection

Two sessions conflict when:
- Both wrote to the same file (different versions)
- Both logged decisions on the same topic (contradicting)
- Both updated the same task (different states)
- Both modified `_kernel/now.json` with different `next` actions (path: `_kernel/now.json`)

The conflict agent presents each side with evidence and recommends a merge. The human decides.

---

## Agent Dispatch Strategy

Don't over-swarm. Scale agents to the rebuild scope:

| Scope | Agents |
|---|---|
| 1 session | 2 — log agent + file agent (no conflict possible) |
| 2-3 sessions, no overlap | 3 — log + files + tasks |
| 2+ sessions with overlap | 4-5 — log + files + tasks + people + conflict |
| Full walnut rebuild | 5+ — all of the above, split file agents by bundle |

Each agent gets the specific file paths and session IDs it needs. No broad searches — targeted reads from known locations.

---

## Difference from alive:session-history

`session-history` lists what happened. This skill **reads the actual files**, **dispatches agents to reconstruct state**, and **merges conflicts**. History is a timeline. This is a context resurrection.

## Difference from alive:mine-for-context

`mine-for-context` extracts knowledge from external source material. This skill reconstructs your own working state from your own session history and files. Different sources, different purpose, different agent strategy.
