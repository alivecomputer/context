---
name: alive:capture-context
description: "Use when external content arrives in the session — emails, transcripts, screenshots, documents, files, or in-session research worth keeping. Also use when there's nothing obvious to capture — the skill checks 03_Inbox/ for unrouted files and enters inbox scan mode. Stores raw content, routes to bundles, extracts tasks and insights into the stash."
user-invocable: true
---

# Capture

Bring context in. The most important skill in the system.

If capture is hard, people skip it. If people skip it, the system dies. Capture must feel instant.

---

## When It Fires

- File pasted or dropped into conversation
- Email forwarded or pulled via API
- Transcript from a call or meeting
- Screenshot shared
- Content pasted from another app
- API context pulled (Gmail, Slack, Calendar)
- Research completed during a session
- The human says "capture this" or "add this"
- Squirrel detects external content and offers to capture
- The human says "check inbox", "clear inputs", or "what's in my inbox"
- Capture is invoked with no obvious in-conversation content (triggers inbox scan — see below)

**Capture is one of two operations that writes mid-session** (the other is bundle drafts). Everything else waits for save.

---

## Three Stages

### Stage 0 — Detect and Classify (instant)

No processing. Just identification.

- **ref_type** — email, transcript, screenshot, document, message, article, research
- **sensitivity** — public, private, restricted (default: private)
- **source** — gmail, slack, web, manual, in-session

### Stage 1 — Store Raw + Route to Bundle (always happens, non-negotiable)

Even if extraction fails or is skipped, the raw content is preserved.

**Pre-capture check:** Before proposing a destination, grep the target walnut's bundle manifests and `brief.md` files for the inbox filename. If found, the file is already referenced — route into that bundle's `raw/` with a note "file was already cited by this bundle" rather than creating a duplicate reference elsewhere.

**Routing decision:** Check `_kernel/now.json` for an active `bundle:` field.

**If active bundle exists — all three steps are mandatory, not optional:**

1. **Write raw file** → `{bundle}/raw/[name].[ext]` with frontmatter added (type, date, source, squirrel — see type-specific templates below)
2. **Update manifest** → add entry to `context_routes:` list in `{bundle}/context.manifest.yaml` with `path:`, `description:`, `type:`, `date:` (see Manifest Enrichment below)
3. **Add inline summary** → append structured summary to manifest body (## Summary, ## Key Points, ## Action Items) so the raw file rarely needs to be re-read

**Do not skip steps 2 or 3.** A raw file without a manifest entry is invisible to the context system. A manifest entry without a summary means every future session has to re-read the raw file. Both waste tokens and lose context.

**If no active bundle:**
```
╭─ 🐿️ captured content — where does it go?
│
│   1. Create new bundle for this
│   2. Add to existing bundle: [list bundle names from walnut root]
│   3. Capture standalone (legacy _references/ style)
╰─
```

If creating a new bundle: scaffold `{name}/context.manifest.yaml` from `templates/bundle/context.manifest.yaml`, then route raw into its `raw/`.

If standalone (backward compat or no bundle fits): write to `_inbox/raw/[name].[ext]` with a minimal manifest entry. The `_inbox` bundle is a holding pen — route out at next save.

**Manifest enrichment.** Whether updating a bundle manifest or creating a standalone manifest, always write:

**Frontmatter** (scan tier) — the `context_routes:` entry in the bundle manifest includes:
- `path:` — relative path to raw file
- `description:` — **one-line summary of what this contains**
- `type:` — email, transcript, screenshot, document, message, article, research
- `date:` — when captured

**Inline summary** (read tier) — if the raw content is rich enough, add a structured summary to the bundle manifest body. Default sections:
- `## Summary` — 2-5 sentences on what this is and why it matters
- `## Key Points` — specific facts, data, claims
- `## Action Items` — tasks, commitments, deadlines

Type-specific manifest templates (in `templates/manifest/`) can guide the summary structure. Use the template sections when one exists.

The summary should be **detailed enough that you rarely need the raw file.** Write it like someone who has 30 seconds to understand what this reference contains.

**File naming:** `YYYY-MM-DD-descriptive-name.ext`
**Garbage filenames** (CleanShot timestamps, IMG_xxxx) get renamed on import.

**Backward compat:** If the walnut still uses `_kernel/_references/`, route there instead. The bundle routing is for walnuts that have bundles (detected by `context.manifest.yaml` files).

### Stage 2 — Extract, Stash, Route (bounded, optional)

Extract actionable content. Bounded by content type — don't over-extract.

| Type | Extract |
|------|---------|
| Email | Tasks, commitments, deadlines, people mentioned |
| Transcript | Decisions, action items, named entities, key quotes |
| Screenshot | Visual analysis summary |
| Document | Key claims, relevant sections, metadata |
| Message | Action items, people, context |
| Article | Key arguments, relevant quotes, source credibility |
| Research | Synthesis, sources consulted, open questions |

Extracted items become stash items tagged with destination walnuts. They route at save, not immediately.

**Stash insights from references.** When extracting, actively look for powerful phrases, domain knowledge, and standing truths. These should be stashed as insight candidates — bold, quotable, evergreen. Not everything — just the stuff that would change how the squirrel operates in this walnut. Example: a transcript reveals "we always lose 2 weeks to regulatory review" → stash as insight candidate.

---

## Two Speeds

**Fast capture** (default): Stage 0 + 1 only. Store raw, create manifest entry, index. Done. Instant.

**Deep capture** (on request or for rich content): Stage 0 + 1 + 2. Full extraction and routing.

The squirrel offers deep capture for content that's clearly rich:

```
╭─ 🐿️ captured — transcript from Jax (45 min)
│  Stored: bundles/shielding-review/raw/2026-02-23-jax-shielding-review.md
│  Bundle manifest updated (sources + summary)
│
│  This looks rich. Deep extract for decisions + tasks?
╰─
```

---

## Inbox Scan Mode

When capture is invoked with **no content in the conversation** — no pasted text, no dropped file, no "capture this" — fall back to checking `$WORLD_ROOT/03_Inbox/`.

If empty: "Nothing to capture, and your inbox is clear."

If items exist, enter inbox scan:

1. **List** — scan `$WORLD_ROOT/03_Inbox/` for non-system files (exclude `.DS_Store`, `.gitkeep`). Present numbered list with detected type and filename.
2. **Process** — for each selected item: read the file, suggest destination walnut + bundle, rename garbage filenames per conventions, present for confirmation or redirect.
3. **Capture** — route raw to the chosen bundle's `raw/`, update bundle `context.manifest.yaml` `context_routes:`, stash insights/tasks, remove original from `$WORLD_ROOT/03_Inbox/`. If no bundle fits, create one or use `_inbox/`.
4. **Continue or stop** — after each item: "N remaining. Next, or done for now?" Partial clearing is fine.

```
╭─ 🐿️ inbox (3 items)
│
│  1. quarterly-report.pdf          document
│  2. IMG_4892.jpg                  screenshot
│  3. meeting-notes-kai.txt         transcript
│
│  Pick one, several (1,3), or "all".
╰─
```

Explicit inbox triggers (skip content check): "check inbox", "clear inputs", "what's in my inbox".

The `alive-inbox-check` hook also nudges after every save if `03_Inbox/` has items — passive reminder without requiring the human to invoke capture.

---

## Type-Specific Manifest Frontmatter

```yaml
# Email
---
type: email
from: jax@novastation.space
to: you@example.com
subject: Shielding vendor shortlist
date: 2026-02-23
squirrel: 2a8c95e9
---

# Transcript
---
type: transcript
participants: [[name], Jax Stellara, Dr. Elara Voss]
duration: 45m
platform: Fathom
date: 2026-02-23
squirrel: 2a8c95e9
---

# Screenshot
---
type: screenshot
source: Competitor orbital pricing page
analysis: Three tiers, lowest at $450K per seat, no group discount visible
date: 2026-02-23
squirrel: 2a8c95e9
---

# Document
---
type: document
author: Dr. Elara Voss
source: Internal engineering team
date: 2026-02-20
squirrel: 2a8c95e9
---

# Research (in-session)
---
type: research
topic: Radiation shielding options for LEO tourism
sources: [NASA technical reports, SpaceX Crew Dragon specs, ESA safety standards]
squirrel: 2a8c95e9
date: 2026-02-23
---
```

---

## In-Session Research Capture

**This is critical.** When the squirrel does significant research during a session — web searches, code analysis, system exploration, competitor analysis, architecture research, API investigation — that knowledge MUST NOT die with the conversation. It cost tokens, time, and thinking to produce. It is a first-class reference.

The squirrel should proactively offer to capture when:
- Significant research was done (10+ minutes of searching/reading/synthesizing)
- A complex topic was explored with multiple sources
- You asked the squirrel to investigate something
- The squirrel produced a synthesis, comparison, or analysis worth keeping

```
╭─ 🐿️ we just spent 30 minutes mapping radiation shielding options.
│  3 sources consulted, 4 key findings, 2 open questions.
│  Capture as a reference so the next session has it?
╰─
```

### What Gets Created

A full reference — the same as any other captured content. Not a summary note. A proper manifest file with frontmatter and structured body.

**1. Route to active bundle** (or create a research bundle). Raw goes to `{bundle}/raw/YYYY-MM-DD-topic.md`, manifest updated with source entry and structured summary:

```yaml
---
type: research
description: Radiation shielding options for LEO tourism — 3 vendors compared, hybrid approach recommended
topic: Radiation shielding options for LEO tourism
sources:
  - NASA Technical Reports Server — LEO radiation exposure data
  - SpaceX Crew Dragon safety specs (public documentation)
  - ESA human spaceflight safety standards (ECSS-E-ST-10-04C)
  - Interview notes from Dr. Elara Voss (Feb 20)
date: 2026-02-23
squirrel: 2a8c95e9
tags: [radiation, shielding, engineering, vendors, safety]
---

## Summary

Three shielding approaches evaluated for the Nova Station habitat module:
aluminium (proven, heavy, cheap), ceramic composite (lighter, 3x cost),
and hybrid (aluminium primary + ceramic secondary for crew quarters).
Hybrid recommended — meets NASA exposure limits at acceptable weight
penalty. Decision pending vendor pricing from Jax's shortlist.

## Key Findings

- LEO radiation exposure: 0.5-1.0 mSv/day (NASA data)
- Aluminium alone requires 10cm thickness → 2,400kg per module
- Ceramic composite at 4cm achieves same protection → 800kg but $2.1M premium
- Hybrid approach: 6cm aluminium + 2cm ceramic for crew areas only → 1,600kg, $900K premium
- FAA Part 450 requires demonstration of <50 mSv annual exposure for passengers

## Open Questions

- Does Jax's vendor shortlist include ceramic composite suppliers?
- What's the weight budget from SpaceVentures? (impacts which approach is viable)
- Has Dr. Voss reviewed the hybrid approach?

## Sources Consulted

- NASA TRS: "Radiation Exposure in Low Earth Orbit" (2024)
- SpaceX Crew Dragon User Guide, Section 4.3 (radiation protection)
- ECSS-E-ST-10-04C: Space Environment Standard
- Session notes from Feb 20 call with Dr. Voss

## Implications

If weight budget allows hybrid approach, it's the clear winner — 33% cheaper
than full ceramic, 33% lighter than full aluminium, meets all regulatory
requirements. The vendor pricing from Jax is the decision gate.
```

**2. Stash insights** — any standing truths discovered during research get stashed as insight candidates:

```
╭─ 🐿️ +2 stash (7)
│  "LEO radiation exposure: 0.5-1.0 mSv/day" → insight candidate
│  "FAA Part 450 requires <50 mSv annual for passengers" → insight candidate
│  → drop?
╰─
```

**3. Stash action items** — any tasks that emerged from the research:

```
╭─ 🐿️ +1 stash (8)
│  Check weight budget with SpaceVentures → [[nova-station]]
│  → drop?
╰─
```

### When NOT to Capture Research

- Quick lookups (one search, one answer) — just answer, don't create a reference
- Obvious facts the squirrel already knew — don't create a file for common knowledge
- Research that led nowhere — unless the dead end is itself useful ("we looked into X, it doesn't work because Y")

The test: **would the next squirrel waste time rediscovering this?** If yes, capture it. If no, let it go.

---

## Bundle-Aware Capture Summary

| Scenario | Raw goes to | Manifest update |
|----------|-------------|-----------------|
| Active bundle in now.json | `{bundle}/raw/` | Add to manifest `context_routes:` + summary |
| No active bundle, the human picks existing | `{bundle}/raw/` | Add to manifest `context_routes:` + summary |
| No active bundle, the human creates new | Scaffold bundle, then `raw/` | New manifest with source |
| Standalone / no bundles folder | `_inbox/raw/` | Minimal manifest, route at save |
| Legacy walnut (has `_kernel/`) | `_kernel/_references/[type]/raw/` | Legacy manifest file |
