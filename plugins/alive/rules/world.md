---
version: 1.0.1-beta
type: foundational
description: How worlds are built. Walnut anatomy, ALIVE domains, _core/ structure, archive, references, connections.
---

# World

A World is an ALIVE folder system on the human's machine. Every file has frontmatter. Every folder has purpose. Nothing gets deleted. Everything progresses.

---

## The ALIVE Framework

Five domains. The letters are the folders. The file system IS the methodology.

```
01_Archive/       A — Everything that was. Mirror paths. Graduation, not death.
02_Life/          L — Personal. Goals, people, patterns. The foundation.
03_Inputs/        I — Buffer only. Content arrives, gets routed out. Never work here.
04_Ventures/      V — Revenue intent. Businesses, clients, products.
05_Experiments/   E — Testing grounds. Ideas, prototypes, explorations.
```

**Life is the foundation.** Ventures and experiments serve life goals.

**Inputs is a buffer.** Nothing lives here permanently. Route out within 48 hours.

**Archive mirrors paths.** `04_Ventures/old-project/` → `01_Archive/04_Ventures/old-project/`. Still indexed, still searchable. Just not on the dashboard.

---

## The Walnut

A walnut is the unit of context. Any meaningful thing with its own identity, lifecycle, and history.

### Anatomy

```
nova-station/
  _core/                          ← system + workshop
    key.md                        what it is
    now.md                        where it is right now
    log.md                        where it's been
    insights.md                   what's known
    tasks.md                      what needs doing
    _capsules/                    units of work (the workshop)
      shielding-review/
        companion.md                        capsule index
        shielding-review-draft-01.md        working draft
        raw/                                source material
      launch-checklist/
        companion.md
        launch-checklist-draft-02.md
        launch-checklist-v1.md              graduated output
        raw/
  shielding-review/               ← graduated capsule (moved out of _core/_capsules/)
    companion.md                  historical record
    shielding-review-v1.md        the output
    raw/                          the sources
  engineering/                    ← live context (the human's work)
  regulatory/
  marketing/
```

**System files live in `_core/`.** key.md, now.md, log.md, insights.md, tasks.md — the five system files live inside `_core/`.

**`_core/_capsules/` is the workshop.** Active capsules — both reference and work — live here. Work capsules graduate to walnut root when they ship v1.

**Everything else is live context.** The human's actual work — documents, assets, code, creative output. Includes things graduated from capsules, things created directly, and things shared with others.

**Backward compat:** Check `_core/` first for system files. Fall back to walnut root for migrated or flat-structure walnuts.

### The Five System Files

| File | Purpose | Changes |
|------|---------|---------|
| `_core/key.md` | Identity — type, goal, people, rhythm, tags, links, references index | Rarely |
| `_core/now.md` | Current state — phase, capsule, next, updated, squirrel, context paragraph | Every save |
| `_core/log.md` | History — signed entries, prepend-only, chronological | Every save |
| `_core/insights.md` | Domain knowledge — standing facts that persist across sessions | When confirmed |
| `_core/tasks.md` | Work queue — checkboxes, priorities, attribution | Every save |

### key.md Frontmatter

```yaml
---
type: venture | person | experiment | life | project | campaign
goal: one sentence
created: 2026-01-15
rhythm: weekly
parent: [[parent-walnut]]          # if nested
people:
  - name: Ryn Okata
    role: engineering lead
    email: ryn@novastation.space
tags: [orbital, tourism, engineering]
links: [[ryn-okata]], [[glass-cathedral]]
published:
  - slug: orbital-safety-brief
    url: https://you.walnut.world/orbital-safety-brief
    date: 2026-02-23
---
```

### now.md Frontmatter

```yaml
---
phase: testing
updated: 2026-02-23T14:00:00
capsule: shielding-review
next: Review telemetry from test window
squirrel: 2a8c95e9
---
```

`capsule:` points at the active capsule being worked on. `next:` is contextualized by which capsule it belongs to. `health:` is derived (see Health Signals below), not stored.

**next: protection:** At save, the squirrel checks whether the previous `next:` was completed. If not, it surfaces the conflict. The previous `next:` is never silently dropped.

### log.md Frontmatter

```yaml
---
walnut: nova-station
created: 2026-01-15
last-entry: 2026-02-23T14:00:00
entry-count: 47
summary: Orbital test window confirmed. Shielding vendor shortlisted.
---
```

Log entries are prepend-only. Newest after frontmatter. Every entry signed.

At 50 entries or phase close → chapter. Synthesis moves to `_chapters/chapter-[nn].md`.

### insights.md

Standing domain knowledge. Updated only when the human confirms an insight as evergreen:

```
╭─ 🐿️ insight candidate
│  "Orbital test windows only available Tue-Thu"
│  Commit as evergreen, or just log it?
╰─
```

### tasks.md

```markdown
## Urgent
- [ ] Book ground control sim  @2a8c95e9

## Active
- [~] Telemetry review  @2a8c95e9

## To Do
- [ ] Vendor site visits

## Done
- [x] Confirm test window  (2026-02-20)
```

Markers: `[ ]` not started, `[~]` in progress, `[x]` done. `@session_id` for attribution.

---

## Support Folders

### _core/_capsules/

Units of work. Each capsule is a folder with a `companion.md` index, versioned drafts, and a `raw/` folder for source material. See `capsules.md` for lifecycle and management.

Three-tier access: companion frontmatter (scan) → companion body (read) → raw files (deep).

Work capsules graduate to walnut root when they ship v1 and the human confirms. The graduated folder at walnut root becomes a historical record alongside live context.

### Legacy: _working/ and _references/

Walnuts still using `_core/_working/` and `_core/_references/` are supported. Migration converts these to capsules inside `_core/_capsules/`. See `capsules.md`.

---

## Creating a New Walnut

1. Create the walnut folder under the appropriate ALIVE domain.
2. Create `_core/` inside it.
3. Write all 5 system files to `_core/`: key.md, now.md, log.md, insights.md, tasks.md.
4. Create `_core/_capsules/`.
5. Record `parent:` in `_core/key.md` if this is a sub-walnut.

---

## Walnuts Inside Walnuts

A walnut can contain sub-walnuts (folders with their own `_core/key.md` and system files). Create when:
- Independent lifecycle (can be started, paused, completed separately)
- Own team, tasks, or rhythm
- Benefits from own log history

Record the relationship in `_core/key.md`: `parent: [[nova-station]]`. The filesystem nesting is convenience; the `parent:` field is canonical.

Don't create sub-walnuts for simple folders. Use a README instead.

---

## People

Every person who matters has a walnut in `02_Life/people/`. Same system file structure (inside `_core/`). Cross-referenced via `[[name]]` wikilinks.

People don't get health signals. They show `last updated`. If someone close hasn't had a context update in a while, the squirrel nudges: "Worth reaching out?"

---

## Connections

`[[walnut-name]]` links walnuts together. Used in `_core/key.md` `links:` field and inline in log entries. `alive:find` traverses these connections.

---

## Archive

Never delete. Mirror the original path into `01_Archive/`.

Archive is graduation. The walnut served its purpose. Still indexed, still searchable, still linkable. Just not on the dashboard.

---

## Health Signals

For endeavors (ventures, experiments, campaigns). Calculated from `rhythm:` in `_core/key.md` and `updated:` in `_core/now.md`.

### Calculation

```
days_since = today - _core/now.md updated date
rhythm_days = { daily: 1, weekly: 7, biweekly: 14, monthly: 30 }

if days_since <= rhythm_days:        health = "active"
if days_since <= rhythm_days * 2:    health = "quiet"
if days_since > rhythm_days * 2:     health = "waiting"
```

| Signal | Meaning | Dashboard |
|--------|---------|-----------|
| active | Within rhythm | No flag |
| quiet | 1-2x past rhythm | Shown in tree |
| waiting | 2x+ past rhythm | ⚠ warning + days count |

### People

People don't get health signals — just `last updated` with nudges. If someone close hasn't had a context update in 2+ weeks, surface: "Worth reaching out to [name]?"

### Preference Toggle

`health_nudges: false` in `.alive/preferences.yaml` disables proactive nudging. Health is still calculated and shown on the dashboard — you just won't volunteer it unprompted.
