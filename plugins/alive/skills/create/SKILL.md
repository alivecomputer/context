---
description: "Something new is emerging. A venture, an experiment, a person entering the orbit, a life area getting serious. It needs its own walnut — its own identity, history, and future. Scaffolds the full structure, maps existing context sources, and optionally migrates files across."
user-invocable: true
---

# Create

Scaffold a new walnut — any type, any ALIVE domain. Understand it first, map where its context lives, then scaffold. Optionally bring in existing content (Step 7 — only if the human has files to migrate).

Not a setup wizard (that's `world/setup.md` — first-time only). Not opening an existing walnut (that's `alive:load`).

---

## Template Locations

The squirrel MUST read these templates before writing — not reconstruct from memory. Templates live relative to the plugin install path.

```
templates/walnut/key.md            → key.md
templates/walnut/now.md            → now.md
templates/walnut/log.md            → log.md
templates/walnut/insights.md       → insights.md
templates/walnut/tasks.md          → tasks.md
templates/squirrel/entry.yaml      → .alive/_squirrels/{session_id}.yaml
templates/capsule/companion.md     → _core/_capsules/{name}/companion.md
```

### Placeholders

| Placeholder | Source |
|------------|--------|
| `{{type}}` | Step 1 selection |
| `{{goal}}` | Extracted from Step 2 free text |
| `{{name}}` | Kebab-case slug derived from Step 2 |
| `{{date}}` | Current ISO date (YYYY-MM-DD) |
| `{{next}}` | Set to the goal initially |
| `{{session_id}}` | Current session ID |
| `{{engine}}` | Current model (e.g. `claude-opus-4-6`) |
| `{{walnut}}` | Same as `{{name}}` |
| `{{description}}` | The human's free text from Step 2, lightly cleaned |

---

## Domain Routing

| Type | ALIVE folder | Notes |
|------|-------------|-------|
| venture | `04_Ventures/{name}/` | |
| experiment | `05_Experiments/{name}/` | |
| life | `02_Life/{name}/` | Under `goals/` if it's a goal |
| person | `02_Life/people/{name}/` | Always under `people/` |
| project | Inside parent walnut folder | Requires parent selection |
| campaign | Inside parent walnut folder | Requires parent selection |

---

## Flow

### Step 1 — Type Selection

```
→ AskUserQuestion: "What type of walnut?"
- Venture — revenue intent (business, client, product)
- Experiment — testing ground (idea, prototype, exploration)
- Life area — personal (goal, habit, health, identity)
- Person — someone who matters
- Project — scoped work inside a venture or experiment
- Campaign — time-bound push with a deadline
```

### Step 2 — Describe It

Free text prompt: "Describe it in a sentence or two — what is it and what's the goal?"

The squirrel infers `name` (kebab-case slug), `goal`, and `domain` from the response.

If type is project or campaign:

```
→ AskUserQuestion: "Which walnut does this belong under?"
- [list active ventures/experiments by scanning _core/key.md frontmatter across ALIVE folders]
- Standalone (no parent)
```

To build the parent list: scan `04_Ventures/*/_core/key.md` and `05_Experiments/*/_core/key.md` — read frontmatter only (type, goal). Check `_core/key.md` first, fall back to walnut root. Present as options with goal as description.

### Step 2b — Codebase Detection

If the description mentions code, a repo, a website, an app, or anything that lives in a git repository:

```
╭─ 🐿️ sounds like this has a codebase. That right?
│
│  ▸ Does this walnut track a code repo?
│  1. Yes
│  2. No — context only
╰─
```

If **yes**, ASK explicitly for each required field — do not infer from the description:

```
╭─ 🐿️ codebase details
│
│  ▸ What's the repo? (e.g., github.com/org/repo)
│  ▸ Where's it cloned locally? (e.g., ~/code/my-project)
│  ▸ Any of these apply?
│    - GitHub account (if you use multiple)
│    - Deploy platform (Vercel, Netlify, etc.)
│    - Database (Supabase, Postgres, etc.)
╰─
```

Even if the human gave repo/path in their description, confirm them here. This step is the only place codebase info is collected — don't skip it.

Set `has_codebase: true` — Step 6 will use `templates/walnut/key-codebase.md` instead of the standard template.

### Step 3 — Pull the Thread

Five minutes of conversation here saves ten sessions of backfilling. Don't rush past this.

Ask follow-ups adapted to the type. Use natural conversation, not an interrogation — if they gave a rich answer in Step 2, skip questions you can already infer.

| Type | Ask about |
|------|-----------|
| Venture | Who's involved — names, roles, how you work with them? What's the business model or revenue path? What phase — idea, building, launched? Any hard deadlines or external commitments? |
| Experiment | What exactly are you testing? Who else is involved? What would make you kill it vs double down? |
| Person | How do you know them? What do you work on together? How do you usually communicate? |
| Life area | Why now? What does progress look like? Who else is involved or affected? |
| Project / Campaign | Who's on it? What's the deadline? What does done look like? |

From the answers, extract and hold for scaffolding:

- **People** — names, roles, contact info if offered → `_core/key.md` `people:` frontmatter + `## Key People`
- **Phase** — starting, planning, building, testing, launched → `_core/now.md` `phase:`
- **Rhythm** — how often they'll touch this (override weekly default if appropriate)
- **Tags** — inferred from domain, industry, tools, people mentioned
- **Description** — 2-3 sentence identity paragraph → `_core/key.md` body

### Step 4 — Context Map

Most new walnuts already have context scattered across tools. Map it now so the squirrel knows where to look later.

```
╭─ 🐿️ context map
│  Where does context for this currently live outside
│  your head? Knowing this helps me find things later
│  and reminds the human what to bring in.
│
│  Think about:
│  · Email threads (Gmail, Outlook)
│  · Chat history (Slack, WhatsApp, iMessage, Discord)
│  · Documents (Google Drive, Notion, Dropbox)
│  · Meeting recordings (Otter, Fathom, Zoom recordings)
│  · Notes (Apple Notes, Obsidian, paper notebook)
│  · Code or repos (GitHub, local projects)
│  · Social / public (Twitter, LinkedIn, website)
│  · Anything else — spreadsheets, Figma, Trello, etc.
╰─
```

→ AskUserQuestion (multiSelect): "Email (Gmail/Outlook)" / "Chat (Slack/WhatsApp/Discord)" / "Docs (Drive/Notion/Dropbox)" / "Other (tell me)"

Follow up briefly on each selected source — which channels, which folders, key threads. Not exhaustive, just enough to know where shadow context lives.

This becomes a `## Context Map` section in `_core/key.md`:

```markdown
## Context Map

| Source | Details | Status |
|--------|---------|--------|
| Gmail | Thread with jax@example.com re: shielding specs | Not captured |
| Slack | #nova-engineering channel | Ongoing |
| Google Drive | "Nova Station" shared folder | Not captured |
| Fathom | 3 recorded calls with engineering team | Not captured |
```

Status: `Not captured` · `Partially captured` · `Ongoing` (live channels) · `Captured`

This map is a living reminder. When the squirrel opens this walnut later, it can prompt: "Your context map shows 3 Fathom recordings marked 'Not captured' — want to bring those in?"

### Step 5 — Confirm Before Writing

Present everything gathered so far:

```
╭─ 🐿️ new walnut
│
│  Name:    flux-engine
│  Type:    experiment
│  Goal:    Build a propulsion flux simulator for orbital testing
│  Path:    05_Experiments/flux-engine/
│  Parent:  none
│  Rhythm:  weekly (default)
│  People:  2 (Jax Stellara, Dr. Elara Voss)
│  Repo:    github.com/org/flux-engine → ~/code/flux-engine  (if codebase)
│  Context: 4 sources mapped (Gmail, Slack, Drive, Fathom)
│
│  → create / change name / change type / change rhythm / cancel
╰─
```

If the human picks "change name" / "change type" / "change rhythm", ask once, then re-present the confirmation.

### Step 6 — Scaffold

Follow the process from `world.md § Creating a New Walnut` exactly:

1. Domain already determined (Step 1-2 above)
2. Create folder at the resolved path (kebab-case name)
3. Read each template from `templates/walnut/`, fill `{{placeholders}}`, write to `_core/` inside the walnut. If `has_codebase: true`, use `templates/walnut/key-codebase.md` instead of `templates/walnut/key.md` — fill the `dev:` block with repo, local_path, and any optional fields collected in Step 2b
4. Create empty directory: `_core/_capsules/`
5. Fill `_core/key.md` frontmatter: type, goal, created (today), rhythm, people (from Step 3), tags (from Step 3)
6. Fill `_core/key.md` body: description from Step 3, `## Key People` with roles, `## Context Map` from Step 4, `## Connections` with any wikilinks to existing walnuts
7. Write first log entry: "Walnut created. {goal}" — signed with session_id
8. If sub-walnut: set `parent: [[parent-name]]` in `_core/key.md` frontmatter
9. Add `[[new-walnut-name]]` to parent's `_core/key.md` `links:` frontmatter field

```
╭─ 🐿️ scaffolding...
│
│  ▸ 05_Experiments/flux-engine/
│  ▸   _core/key.md — type: experiment, goal set, 2 people, 4 context sources
│  ▸   _core/now.md — phase: starting
│  ▸   _core/log.md — first entry signed
│  ▸   _core/insights.md — empty, ready
│  ▸   _core/tasks.md — empty, ready
│  ▸   _core/_capsules/
│
│  Walnut is alive.
╰─
```

### Step 7 — Existing Content Check

**DO NOT read `create/migrate.md` until the human answers "yes" below. It is a large file that must not be loaded into context unless needed. Most walnut creations will skip this step entirely.**

```
╭─ 🐿️ got files to bring in?
│
│  If you have docs, notes, or a project folder on your
│  computer for this — I can pull them in now.
│
│  Tip: if you've got a bunch of scattered files, throw
│  them all into one folder first and point me at it.
│  I'll work through each one — companions, frontmatter,
│  the lot.
╰─
```

→ AskUserQuestion: "Yes — I have files to bring across" / "No — starting fresh"

If **"No"** — done. Offer: "Say `open {name}` to start working."

If **"Yes"** — NOW read `create/migrate.md` and follow it from the Entry Point section. The migrate skill handles everything from here: asking what kind of content, detecting whether it's a light seed or full project migration, and running the appropriate flow.

---

---

## Capsule Creation

Capsule scaffolding is handled by `alive:capsule`. When the human says "create a capsule" or when stash routing identifies a new body of work, invoke the capsule skill — it handles the full create flow including companion template, raw/ directory, and now.md update.

---

## Capsule → Walnut Graduation

When a capsule grows too big for its container (needs its own sessions, log, lifecycle):

1. Confirm with the human: "This capsule wants to be a walnut. Graduate it?"
2. Determine ALIVE domain and name
3. Scaffold new walnut (Step 6 above)
4. Seed `_core/key.md` from capsule companion (goal, tags, people carry over)
5. Move capsule contents into new walnut's `_core/_capsules/` as the first capsule
6. Log entry in BOTH parent walnut ("Capsule {name} graduated to walnut") and new walnut ("Graduated from {parent}")
7. Add wikilink `[[new-walnut]]` to parent's `_core/key.md` `links:`

---

## Files Created

| File | Template source |
|------|----------------|
| `{domain}/{name}/_core/key.md` | `templates/walnut/key.md` |
| `{domain}/{name}/_core/now.md` | `templates/walnut/now.md` |
| `{domain}/{name}/_core/log.md` | `templates/walnut/log.md` |
| `{domain}/{name}/_core/insights.md` | `templates/walnut/insights.md` |
| `{domain}/{name}/_core/tasks.md` | `templates/walnut/tasks.md` |
| `{domain}/{name}/_core/_capsules/` | Empty directory |
| Parent's `_core/key.md` | Updated `links:` field (if sub-walnut) |

## Files Read

| File | Why |
|------|-----|
| `templates/walnut/key.md` | Template — read before writing key.md |
| `templates/walnut/now.md` | Template — read before writing now.md |
| `templates/walnut/log.md` | Template — read before writing log.md |
| `templates/walnut/insights.md` | Template — read before writing insights.md |
| `templates/walnut/tasks.md` | Template — read before writing tasks.md |
| `templates/capsule/companion.md` | Template — read before writing capsule companion |
| `templates/squirrel/entry.yaml` | Schema for squirrel entry |
| `rules/world.md § Creating a New Walnut` | The walnut scaffolding process — follow exactly |
| Active walnuts' `_core/key.md` frontmatter | For parent list (project/campaign types only) |
