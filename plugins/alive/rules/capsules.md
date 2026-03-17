---
version: 1.0.1-beta
type: foundational
description: The capsule system. Lifecycle, graduation, collaboration, references, routing.
---

# Capsules

A capsule is a self-contained unit of work or reference inside a walnut. Lives in `_core/_capsules/`. Contains a companion.md index, versioned drafts, and a raw/ folder for source material.

---

## Two Species

Same anatomy, different destiny.

**Reference capsules** — accumulate related context. Goal describes a collection ("Collect and index all MerchGirls call transcripts"). Companion IS the value — synthesizes what's in raw/. Status stays `active` (ongoing) or `done` (retired). Never graduates. Lives in `_core/_capsules/` permanently.

Reference capsules CAN have non-versioned documents inside (synthesis.md, patterns.md) — these are derived context, not v0.x progression. If a synthesis grows big enough to need its own drafts, it spawns a work capsule.

**Work capsules** — produce something specific. Goal describes a deliverable ("Rebuild hypha.nz from TWD strategy deck"). Iterates through v0.x drafts. Graduates to walnut root when v1 ships.

No `capsule_type:` field needed — the `goal:` field tells you which kind it is.

---

## Capsule Anatomy

```
_core/_capsules/website-rebuild/
  companion.md                          <- The scannable index
  website-rebuild-draft-01.md           <- Working drafts
  website-rebuild-draft-02.md
  raw/                                  <- Source material
    2026-03-12-screenshot.png
```

---

## Companion Structure

### Frontmatter

```yaml
---
type: capsule
goal: One sentence
status: draft | prototype | published | done
version: v0.2
sensitivity: private | public | restricted
pii: false
created: 2026-03-11
updated: 2026-03-11
squirrels: [bc96e49c, a3f7c2d1]
active_sessions:
  - session: a8c95e9
    engine: claude-opus-4-6
    started: 2026-03-12T14:00:00
    working_on: "v0.3 — restructuring intro section"
sources:
  - path: raw/2026-02-23-doc.pdf
    description: Vendor proposal
    type: document
    date: 2026-02-23
linked_capsules: [[website], [brand-brief]]
tags: [engineering, vendors]
---
```

### Body Sections

- `## Context` — what this capsule is about, current state
- `## Tasks` — capsule-scoped checkboxes with @session_id attribution. Same syntax as walnut tasks.md.
- `## Changelog` — every version after v0.1 gets a brief note about what changed
- `## Work Log` — append-only. Each session adds its entry at the bottom. Never edit previous entries.

---

## Capsule Lifecycle

Capsules have a status-based lifecycle. Versions are files inside the capsule.

```
draft       -> prototype   -> published   -> done
started       has visual     shared        outputs graduated
```

- **draft** — actively being worked on. Markdown only.
- **prototype** — has a visual (HTML), maybe shared with 1-2 people.
- **published** — shared externally. Companion tracks `published:` metadata.
- **done** — outputs graduated to live context. Capsule is historical record.

Version files inside the capsule use the capsule name for self-documentation:
- `{capsule-name}-draft-{nn}.md` — working drafts (e.g., `shielding-review-draft-01.md`)
- `{capsule-name}-draft-{nn}.html` — visual versions (optional)
- `{capsule-name}-v1.md` / `{capsule-name}-v1.html` — the graduated version

### Before Iterating

Every version after v0.1 should have a brief note in the capsule companion's `## Changelog` about what changed. The companion forces the question: what worked, what didn't, what's different this time?

---

## Graduation

### Work capsule -> walnut root

- Mechanical signal: a `*-v1.md` (or `*-v1.html`) file gets written
- Squirrel notices and asks: "v1 exists. Graduate this capsule?"
- Human confirms -> folder moves from `_core/_capsules/` to walnut root
- Status flips to `done` or `published` in companion
- Two keys to turn: v1 exists + human says yes.

### Capsule -> walnut graduation

- Pure judgment. Squirrel surfaces when capsule gets heavy.
- Signals: too many sources, needs own sessions/log/people, own rhythm
- Companion.md seeds the new key.md
- Three levels: raw material -> capsule -> graduated capsule -> walnut

---

## Capsule Routing Heuristic

When content arrives or work begins:

```
Does an active capsule match this goal?
|-- Yes, exact match -> add to it
|-- Related but different goal -> new capsule, link to existing
|-- Ambiguous -> ask once
+-- No match -> new capsule
```

The core heuristic is **goal alignment**:
- Same goal -> same capsule
- Related goal -> linked capsules (not merge)
- Different goal -> new capsule
- Goal outgrew the capsule -> capsule -> walnut graduation

When ambiguous, ask once:

```
╭─ 🐿️ this relates to [[existing-capsule]]
│  Add to it, or start a fresh capsule?
│
│  ▸ Which one?
│  1. Add to existing
│  2. Start fresh
│  3. Link them
╰─
```

### Merge is Rare

If two capsules overlap, link them or spawn a third that synthesizes both. Only merge when "these should never have been separate" — pick primary, move other's raw/ in, archive empty shell. Never merge silently.

---

## Multi-Agent Collaboration

### 1. Active session claim

`active_sessions:` in companion frontmatter (spec above). When opening a capsule for work, add yourself. When saving/closing, remove yourself. Others see who's working and what they're touching.

### 2. Capsule-scoped tasks

`## Tasks` in companion body with checkbox + @session_id. `[~]` means actively being worked on.

### 3. Append-only work log

`## Work Log` in companion body. Each session adds at bottom. Never edit previous entries. Includes session_id, engine, bullet points of what was done.

### 4. Immutable version files

Create v0.4.md, don't edit v0.3.md. If concurrent agents work on different aspects, they write different version files. Merge in next version.

---

## Cross-Capsule References

References live inside capsules. The capsule companion IS the index.

### Three Tiers

1. **Scan** — Capsule companion frontmatter. The squirrel scans `_core/_capsules/*/companion.md` and reads `sources:` list. Each source has `path:`, `description:`, `type:`, `date:`. This IS the index.
2. **Read** — Capsule companion body. Structured summaries of captured content live here alongside decisions, current state, and changelog.
3. **Deep** — Raw files in `_core/_capsules/{name}/raw/`. Only loaded on explicit request.

The squirrel scans tier 1 (companion frontmatter sources) at open or on demand. Goes to tier 2 (companion body) when specific context is needed. Goes to tier 3 (raw) only when specifically asked.

### Shared References

When a source feeds multiple capsules:
- The raw file lives where it was first captured
- Other capsules link to it via `sources:` path in companion frontmatter
- One source of truth, multiple consumers
- Path references are relative: `../other-capsule/raw/filename.md`

---

## Stale Capsules

Capsules in `draft` status unchanged for 30+ days are surfaced by `alive:tidy`:
- **Advance** -> move to prototype or published
- **Archive** -> set status to done, note reason
- **Kill** -> delete (capsules in draft are disposable)

---

## Legacy Support

Walnuts with `_core/_working/` and `_core/_references/` still work. The three-tier concept is the same — just the storage location differs. Legacy companions (`_core/_references/**/*.md`) are scanned the same way. Migration converts them to capsules.
