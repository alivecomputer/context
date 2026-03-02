---
version: 0.1.0-beta
type: foundational
description: Frontmatter, file naming, version progression, working folder lifecycle, reference system, signing.
---

# Conventions

The boring infrastructure that prevents entropy. Every file follows these. No exceptions.

---

## Frontmatter on Everything

**This is the most important convention in the system.**

Every `.md` file in the system has YAML frontmatter. No exceptions. The frontmatter is the scannable layer — a squirrel reads frontmatter before bodies. If a file doesn't have frontmatter, it's malformed.

| File type | Required frontmatter |
|-----------|---------------------|
| System files (key, now, log, insights, tasks) | Schema defined in world.md |
| Working files | squirrel, model, version, previous, kept, changed |
| Companions | type, description, type-specific fields, squirrel, tags |
| Rules | version, type, description |
| Skills | description, triggers |

**Every companion must have `description:` in frontmatter.** This is the one-line scan that tells the squirrel what the reference contains without reading the body. It's the difference between a useful reference system and a pile of files.

---

## Signing

Every file the squirrel creates or modifies carries attribution:

- `squirrel: [session_id]` — which session created/modified it
- `model: [engine]` — which AI model was running

Log entries are additionally signed at the end: `signed: squirrel:[session_id]`

Squirrel entries carry the full metadata: session_id, runtime_id, engine, walnut, timestamps.

---

## File Naming

### Working Files (_core/_working/)

Pattern: `[context]-[name]-v0.x.md`

Anyone reading the filename knows what it is and where it belongs.

```
launch-checklist-v0.1.md
orbital-safety-brief-v0.2.md
festival-submission-v0.3.md
```

### References (_core/_references/)

Pattern: `YYYY-MM-DD-descriptive-name.ext`

```
2026-02-23-kai-shielding-review.md        ← companion
2026-02-23-kai-shielding-review.mp3        ← raw (in raw/ subfolder)
2026-02-20-vendor-proposal.pdf
```

### Raw File Renaming

Garbage filenames get renamed on import. The original name is preserved in the companion frontmatter as `original_filename:`.

| Before | After |
|--------|-------|
| `CleanShot 2026-02-23 at 14.32.07@2x.png` | `2026-02-23-competitor-pricing-screenshot.png` |
| `IMG_4892.jpg` | `2026-02-20-prototype-photo.jpg` |
| `Document (3).pdf` | `2026-02-18-vendor-proposal-v3.pdf` |

### Companion Files

Same name as the raw file, `.md` extension. Lives alongside the raw file's parent directory (not inside `raw/`).

```
_references/transcripts/
  2026-02-23-kai-shielding-review.md      ← companion
  raw/
    2026-02-23-kai-shielding-review.mp3   ← raw
```

---

## Version Progression

### The Lifecycle

```
v0.1  →  v0.2  →  v0.x  →  v1 (graduation)
draft    iterated  refined   shareable
```

- **v0.x** lives in `_core/_working/`. The squirrel's workspace. Nobody outside sees it.
- **v1** is shareable. Promoted OUT of `_core/_working/` to live context (outside `_core/`). This is the graduation moment — when a working file is ready to send to another human.
- **v1+** can be previewed/published via `walnut:publish`.

### Before Iterating

Every version after v0.1 gets version frontmatter documenting what changed:

```yaml
---
squirrel: 2a8c95e9
model: claude-opus-4-6
version: v0.3
previous: v0.2
kept: [structure, key sections, tone]
changed: [too long — cut by 40%, added regulatory section, removed speculation]
---
```

Never iterate without reflecting. The frontmatter forces the question: what worked, what didn't, what's different this time?

### Promotion

When a working file graduates to v1:

1. Move/copy from `_core/_working/` to its proper location in live context
2. Update version frontmatter to `v1`
3. Log entry: "Promoted [name] to v1"
4. Optionally: preview/publish via `walnut:publish`

---

## Working Folder Management

### _core/_working/ is a Hot Desk

Things move through it, not accumulate. It's a workspace, not a filing cabinet.

### Folder Graduation

When `_core/_working/` accumulates related files:

- **3+ related files with shared prefix** → graduate to a proper folder with README
- **Versioned files (v1, v2, v3)** → graduate to a folder

### Stale Drafts

Working files unchanged for 30+ days are surfaced by `walnut:housekeeping`:
- **Promote** → graduate to v1, move to live context
- **Archive** → move to `01_Archive/`
- **Kill** → delete (the only place deletion is acceptable — drafts are disposable)

---

## Reference System

The reference system is the three-tiered context layer. It handles ALL external content entering the walnut.

### Three Tiers

1. **Scan** — Companion frontmatter. The squirrel scans `_core/_references/**/*.md` and reads YAML headers (type, description, date, tags). This IS the index. No separate index file needed. **References do NOT go in key.md.** key.md is identity. References are captured content. Their frontmatter is the scan layer.
2. **Read** — Companion body. AI-generated structured summary. Detailed enough you rarely need the raw file.
3. **Deep** — The raw file itself. Only loaded on explicit request.

The squirrel scans tier 1 (companion frontmatter) at open or on demand. Goes to tier 2 (companion body) when specific context is needed. Goes to tier 3 (raw) only when specifically asked.

### Companion Structure

Every companion has:

**Frontmatter** (tier 1 scan):
```yaml
---
type: transcript
description: Kai shielding vendor review — shortlisted 3 options, decision pending
participants: [You, Kai Tanaka]
duration: 45m
platform: Fathom
date: 2026-02-23
squirrel: 2a8c95e9
tags: [shielding, vendors, engineering]
original_filename: recording-2026-02-23.mp3
---
```

**Body** (tier 2 read — AI-generated summary):
The body is a structured summary written by the squirrel at capture time. It should be detailed enough that reading the raw file is rarely necessary.

- `## Summary` — 2-5 sentences on what this is and why it matters
- `## Key Points` — specific facts, data, claims
- `## Action Items` — tasks, commitments, deadlines (also stashed)
- `## Source` — pointer to raw file path

### Type-Specific Frontmatter

| Type | Required fields |
|------|----------------|
| email | from, to, subject, date, description |
| transcript | participants, duration, platform, date, description |
| screenshot | source, date, description |
| document | author, source, date, description |
| message | from, platform, date, description |
| article | author, publication, url, date, description |
| research | topic, sources, squirrel, date, description |

All companions also include: `type`, `description`, `squirrel`, `tags`, and optionally `original_filename`.

### Reference Organisation

```
_core/_references/
  transcripts/
    raw/
    [companions]
  emails/
    raw/
    [companions]
  documents/
    raw/
    [companions]
  screenshots/
    raw/
    [companions]
  research/
    [companions only — no raw for in-session research]
```

---

## Wikilinks

`[[walnut-name]]` is the syntax for linking walnuts together. It's a text convention — not rendered, not enforced by tooling. It's a marker that says "this references another walnut."

**Where to use them:**
- `key.md` frontmatter `links:` field — the canonical list of connections
- `key.md` body — in the Connections and Key People sections
- `log.md` entries — inline when referencing other walnuts ("discussed with [[ada-chen]]")
- `insights.md` — when an insight relates to another walnut

**Rules:**
- If you add a `[[link]]` inline, also add it to key.md `links:` frontmatter
- `walnut:find` searches for these across all walnuts
- Person walnuts use `[[first-last]]` format (e.g., `[[ada-chen]]`)
- Walnut names use kebab-case (e.g., `[[nova-station]]`, `[[glass-cathedral]]`)

---

## Archiving

Nothing gets deleted. Archive is graduation — the walnut served its purpose.

**When to archive:**
- Walnut is complete (phase: complete, no open tasks)
- Walnut is abandoned (no activity for 3+ months, conductor confirms)
- Walnut has graduated to something else (experiment → venture)

**How to archive:**
1. Mirror the original path into `01_Archive/`
   - `04_Ventures/old-project/` → `01_Archive/04_Ventures/old-project/`
2. Move the entire folder (including `_core/`)
3. The path tells you where it came from
4. Log a final entry: "Archived. Reason: [why]"
5. Update any walnuts that linked to this one (their `[[old-project]]` links still resolve — archived walnuts are still searchable)

**What survives archiving:**
- `walnut:find` still searches archived walnuts
- `[[wikilinks]]` still resolve
- `walnut:recall` still finds sessions that worked on it
- The walnut just doesn't show on `walnut:world` dashboard

**The archive enforcer hook** prevents deletion inside ALIVE folders. If someone tries `rm` on anything in the system, the hook blocks it and suggests archiving instead.

---

## Third-Party Skill Overrides

External plugins (superpowers, etc.) assume standard code project layouts — `docs/plans/`, `src/`, `tests/`. These paths don't exist in a World. The squirrel must intercept and reroute.

**Before any skill creates a file, ask:** "Am I in a walnut? Where does this belong?"

| Skill output | Route to instead |
|-------------|-----------------|
| `docs/plans/*.md` | `{active-walnut}/_core/_working/plans/` |
| `docs/*.md` | `{active-walnut}/_core/_working/` |
| Any file at World root | Determine the walnut it belongs to, route to `_core/_working/` or `_core/_references/` |

**No orphan files at World root.** The only things at root level are the 5 ALIVE domain folders, `.claude/`, and dotfiles. Everything else belongs inside a walnut.

If no walnut is active and a skill wants to write a file, ask the conductor where it should go before writing.

---

## Creating a New Walnut

When a new walnut needs to be created (from save routing, capture, or explicit request):

1. Determine the ALIVE domain: Life (`02_Life/`), Venture (`04_Ventures/`), Experiment (`05_Experiments/`)
2. Create the folder with kebab-case name
3. Create `_core/` with all 5 files from templates (key, now, log, insights, tasks)
4. Create `_core/_squirrels/`, `_core/_working/`, `_core/_references/`
5. Fill key.md frontmatter: type, goal, created, rhythm, tags
6. Fill key.md body: description, key people, context
7. Write first log entry: "Walnut created. [goal]"
8. If it's a sub-walnut, set `parent: [[parent-name]]` in key.md frontmatter
9. Add `[[new-walnut-name]]` to parent's key.md `links:` field

