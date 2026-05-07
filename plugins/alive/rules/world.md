---
version: 3.0.0
type: foundational
description: How worlds are built. Walnut anatomy, ALIVE domains, _kernel/ structure, bundles, archive, connections.
---

# World

A World is an ALIVE Context System folder on the human's machine. Every file has frontmatter. Every folder has purpose. Nothing gets deleted. Everything progresses.

The root is identified by an `.alive/` marker directory. When a session starts, walk up from the working directory until you find `.alive/`. That's the world root.

---

## The ALIVE Framework

Five domains. The letters are the folders. The file system IS the methodology.

```
01_Archive/       A — Everything that was. Mirror paths. Graduation, not death.
02_Life/          L — Personal. Goals, patterns, people. The foundation.
03_Inbox/        I — Buffer only. Content arrives, gets routed out. Never work here.
04_Ventures/      V — Revenue intent. Businesses, clients, products.
05_Experiments/   E — Testing grounds. Ideas, prototypes, explorations.

```

**Domain folders are NOT walnuts.** The five ALIVE folders are containers — they hold walnuts, they don't have `_kernel/` themselves. Never create `_kernel/` inside a domain folder. If `02_Life/_kernel/` exists, it's legacy cruft.

**Life is the foundation.** Ventures and experiments serve life goals.

**Inbox is a buffer.** Nothing lives here permanently. Route out within 48 hours.

**Archive mirrors paths.** `04_Ventures/old-project/` → `01_Archive/04_Ventures/old-project/`. Still indexed, still searchable. Just not on the dashboard.

**People live inside Life.** Every person who matters gets a walnut at `02_Life/people/`. Cross-referenced via `[[name]]` wikilinks. See People section below.

---

## The Walnut

A walnut is the unit of context. Any meaningful thing with its own identity, lifecycle, and history. A project, a person, a venture, an experiment, a life goal.

### Anatomy

```
nova-station/
  _kernel/                             ← system source files (flat)
    key.md                             what it is (identity, rarely changes)
    log.md                             where it's been (prepend-only history)
    insights.md                        what's known (standing domain knowledge)
    tasks.json                         work queue (script-operated, JSON)
    now.json                           current state snapshot (script-generated)
    completed.json                     archived completed tasks (JSON)
    links.yaml                         ← overflow: outbound connections
    people.yaml                        ← overflow: enriched people records
    history/                           ← overflow: log chapters
      chapter-01.md                    synthesized log segment
      chapter-02.md
  shielding-review/                    ← bundle (has context.manifest.yaml)
    context.manifest.yaml              bundle index
    tasks.json                         bundle-scoped work queue (JSON)
    raw/                               source material
      2026-03-12-screenshot.png
  launch-checklist/                    ← bundle (has context.manifest.yaml)
    context.manifest.yaml
    tasks.json
    raw/
  shielding-review-v1.md              ← graduated output (stays alongside bundle)
  engineering/                         ← live context (the human's work)
  regulatory/
  marketing/
```

**Identification:** A folder with `_kernel/key.md` is a walnut. A folder with `context.manifest.yaml` is a bundle. `_kernel/` is the only underscore-prefixed directory.

**System source files live in `_kernel/`.** key.md, log.md, insights.md — the three source files the squirrel reads and writes. tasks.json, now.json, completed.json — script-operated data files. All flat in `_kernel/`, no subdirectories for generated output.

**Kernel overflow files** handle growth:
- `links.yaml` — when `links:` in key.md gets long (10+ entries), move the full list here and keep key.md's `links:` as a summary pointer.
- `people.yaml` — when `people:` in key.md gets long (5+ entries with enriched records), move the full records here. key.md keeps names and roles.
- `history/` — log chapters. At 50 entries or phase close, synthesize older entries into `chapter-[nn].md` and keep log.md lean.

**Bundles live flat in the walnut root** alongside `_kernel/`. Any folder at any depth with a `context.manifest.yaml` is a bundle. Bundles can contain bundles (unlimited depth). Bundles can contain walnuts. See `bundles.md` for lifecycle and management.

**Everything else is live context.** The human's actual work — documents, assets, code, creative output. Includes things graduated from bundles, things created directly, and things shared with others.

**Backward compat:** Check `_kernel/` first for system files. Fall back to `_kernel/_generated/` (v2), then `_core/` (v1) for migrated or flat-structure walnuts. Check for `now.json` at `_kernel/now.json` first, fall back to `_kernel/_generated/now.json` (v2), fall back to `now.md` (v1). v2 `bundles/` container directory is still recognized during migration.

---

## Format Rules

| Written by | Contains | Format | Examples |
|------------|----------|--------|---------|
| Script | Data consumed by scripts/UI | JSON | tasks.json, now.json, completed.json, _index.json |
| Agent | Prose with metadata | Markdown + YAML frontmatter | log.md, insights.md, key.md |
| Agent | Structured config/identity | YAML | context.manifest.yaml, preferences.yaml |

JSON for script-operated files: built-in Python parser, zero dependencies. PyYAML deliberately avoided.

---

### The Three Source Files

| File | Purpose | Changes |
|------|---------|---------|
| `_kernel/key.md` | Identity — type, goal, people, rhythm, tags, links, repo | Rarely |
| `_kernel/log.md` | History — signed entries, prepend-only, chronological | Every save |
| `_kernel/insights.md` | Domain knowledge — standing facts that persist across sessions | When confirmed |

### Script-Operated Files

| File | Purpose | Written by |
|------|---------|------------|
| `_kernel/tasks.json` | Work queue — urgent, active, waiting tasks | Script + agent |
| `_kernel/now.json` | Current state snapshot — computed projection | `project.py` only |
| `_kernel/completed.json` | Archived completed tasks | Script |

### key.md Frontmatter

```yaml
---
type: venture | person | experiment | life | project | campaign
goal: one sentence
created: 2026-01-15
rhythm: weekly
parent: [[parent-walnut]]          # if nested
repo: github.com/org/repo          # for dev projects — links to external codebase
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

When `people:` grows beyond 5 enriched entries, move the full records to `_kernel/people.yaml` and keep key.md's list as name + role only.

When `links:` grows beyond 10 entries, move the full list to `_kernel/links.yaml` and keep key.md's list as the top connections.

**`repo:` field.** For dev projects, `repo:` points at the external codebase. The session hook can reverse-lookup: given a working directory inside a repo, find the walnut that tracks it. This bridges the walnut world and the code world.

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

At 50 entries or phase close → chapter. Synthesis moves to `_kernel/history/chapter-[nn].md`. The chapter contains a summary and the full entries. log.md's entry count resets for the active chapter but `entry-count:` in frontmatter reflects the total.

### insights.md

Standing domain knowledge. Updated only when the human confirms an insight as evergreen:

```
╭─ 🐿️ insight candidate
│  "Orbital test windows only available Tue-Thu"
│  Commit as evergreen, or just log it?
╰─
```

### now.json (Script-Generated)

now.json lives at `_kernel/now.json`. It is computed by `project.py` post-save from all source files. The agent never writes now.json — the projection script reads all source files after every save and computes it.

```json
{
  "phase": "testing",
  "updated": "2026-02-23T14:00:00",
  "bundle": "shielding-review",
  "bundles": {
    "active": [
      {
        "name": "shielding-review",
        "status": "draft",
        "goal": "Evaluate shielding vendors for orbital module",
        "tasks": {"counts": {"urgent": 1, "active": 0, "todo": 3, "blocked": 0}},
        "updated": "2026-02-23T14:00:00"
      }
    ],
    "recent": [
      {
        "name": "launch-checklist",
        "status": "draft",
        "updated": "2026-02-20T10:00:00"
      }
    ],
    "summary": {
      "total": 4,
      "active": 1,
      "draft": 2,
      "done": 1
    }
  },
  "unscoped_tasks": {
    "urgent": ["Confirm insurance renewal by March 1"],
    "active": ["Update investor deck with Q1 numbers"]
  },
  "recent_sessions": [
    {
      "id": "a8c95e9",
      "date": "2026-02-23T14:00:00",
      "summary": "Reviewed shielding vendor proposals, shortlisted two"
    }
  ],
  "children": [
    {
      "name": "glass-cathedral",
      "phase": "design",
      "health": "active"
    }
  ],
  "blockers": [
    "Waiting on vendor NDA countersign"
  ],
  "context": "Shielding vendor shortlisted after 3 rounds. Test window confirmed March 4. Two vendors remain — Orbital Systems and ThermaShield. Insurance renewal due before launch window.",
  "squirrel": "2a8c95e9"
}
```

`bundles:` has three tiers — `active` (full detail), `recent` (light), `summary` (counts). Health is derived (see Health Signals below), not stored. The urgent + active task lists in `unscoped_tasks` and per-bundle `tasks` carry the immediate-action signal — there is no separate top-level field for it.

---

## Bundles

Units of work. Each bundle is a folder with a `context.manifest.yaml` index, scoped tasks, and a `raw/` folder for source material. Bundles live flat in the walnut root alongside `_kernel/`. See `bundles.md` for full anatomy, lifecycle, species, and management.

Three-tier access: manifest frontmatter (scan) → manifest body fields (read) → raw files (deep).

### Graduation (Status Flip)

When an outcome bundle ships v1:
- `status:` changes to `done` or `published` in manifest
- Bundle folder stays where it is — no folder moves
- v1 output file lives inside the bundle folder (or alongside it at walnut root)
- No ceremony, no folder moves. The status field is the record.

### Bundle -> Walnut Graduation

When a bundle outgrows its parent walnut, it can be promoted to its own walnut. See `bundles.md` for signals and process.

### Nesting

Bundles can contain bundles (unlimited depth). Bundles can contain walnuts. Identification is by marker file: `context.manifest.yaml` = bundle, `_kernel/key.md` = walnut.

### Legacy: _core/_capsules/, _working/, _references/

Walnuts still using `_core/_capsules/` with `companion.md` files are supported. Walnuts using `_core/_working/` and `_core/_references/` are also supported. Migration converts these to bundles. See `bundles.md` for migration notes.

---

## Creating a New Walnut

1. Create the walnut folder under the appropriate ALIVE domain (or `02_Life/people/` for a person).
2. Create `_kernel/` inside it (flat, no subdirectories).
3. Write the 3 source files to `_kernel/`: key.md, log.md, insights.md.
4. Create `_kernel/tasks.json` with `{"tasks": []}`.
5. Create `_kernel/completed.json` with `{"completed": []}`.
6. Run `project.py --walnut {path}` to generate initial `_kernel/now.json`.
7. Record `parent:` in `_kernel/key.md` if this is a sub-walnut.

---

## Walnuts Inside Walnuts

A walnut can contain sub-walnuts (folders with their own `_kernel/key.md` and system files). Create when:
- Independent lifecycle (can be started, paused, completed separately)
- Own team, tasks, or rhythm
- Benefits from own log history

Record the relationship in `_kernel/key.md`: `parent: [[nova-station]]`. The filesystem nesting is convenience; the `parent:` field is canonical.

Don't create sub-walnuts for simple folders. Use a README instead.

---

## People

Every person who matters has a walnut in `02_Life/people/`. People live inside Life because your life contains your people. Same system file structure (inside `_kernel/`). Cross-referenced via `[[name]]` wikilinks.

People are a feature of the Life domain, not their own domain. They cross-cut ventures, experiments, and other life areas via wikilinks -- `[[ryn-okata]]` works from anywhere in the world. The filesystem location is `02_Life/people/`; the connection model is flat.

People don't get health signals. They show `last updated`. If someone close hasn't had a context update in a while, the squirrel nudges: "Worth reaching out?"

**Backward compat:** Legacy subfolder conventions `02_Life/people/professional/`, `02_Life/people/inner/`, `02_Life/people/orbit/` are still recognized. No forced migration.

---

## Connections

`[[walnut-name]]` links walnuts together. Used in `_kernel/key.md` `links:` field (or `_kernel/links.yaml` for overflow) and inline in log entries. `alive:search-world` traverses these connections.

---

## Archive

Never delete. Mirror the original path into `01_Archive/`.

Archive is graduation. The walnut served its purpose. Still indexed, still searchable, still linkable. Just not on the dashboard.

---

## Health Signals

For endeavors (ventures, experiments, campaigns). Calculated from `rhythm:` in `_kernel/key.md` and `updated:` in `_kernel/now.json`.

### Calculation

```
days_since = today - now.json updated date
rhythm_days = { daily: 1, weekly: 7, biweekly: 14, monthly: 30 }

if days_since <= rhythm_days:        health = "active"
if days_since <= rhythm_days * 2:    health = "quiet"
if days_since > rhythm_days * 2:     health = "waiting"
```

| Signal | Meaning | Dashboard |
|--------|---------|-----------|
| active | Within rhythm | No flag |
| quiet | 1-2x past rhythm | Shown in tree |
| waiting | 2x+ past rhythm | Warning + days count |

### People

People don't get health signals — just `last updated` with nudges. If someone close hasn't had a context update in 2+ weeks, surface: "Worth reaching out to [name]?"

### Preference Toggle

`health_nudges: false` in `.alive/preferences.yaml` disables proactive nudging. Health is still calculated and shown on the dashboard — you just won't volunteer it unprompted.

---

## Dev Projects

Walnuts that track external codebases include `repo:` in `_kernel/key.md`. This enables:

- **Reverse lookup:** The session hook checks the current working directory against known `repo:` fields to auto-detect which walnut is relevant.
- **Context bridging:** The squirrel can reference code context (branches, PRs, CI status) alongside walnut context (log, insights, tasks).
- **Multiple repos:** A walnut can track multiple repos via a list in `repo:`.

```yaml
repo: github.com/alivecontext/alive
# or multiple:
repo:
  - github.com/alivecontext/alive
  - github.com/alivecontext/alive-web
```

---

## Root Detection

The world root is identified by the `.alive/` marker directory. To find the world root:

1. Start from the current working directory.
2. Walk up the directory tree.
3. The first directory containing `.alive/` is the world root.

`.alive/` contains world-level configuration: `key.md` (world index), `preferences.yaml` (worldbuilder overrides), `_squirrels/` (session entries), `statusline.sh`, and generated indexes.
