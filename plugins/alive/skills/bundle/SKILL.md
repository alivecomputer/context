---
name: alive:bundle
description: "Create, share, and graduate bundles — the unit of focused work within a walnut. Manages the full bundle lifecycle from creation through sharing to graduation."
user-invocable: true
---

# Bundle

Manage the bundle lifecycle: create, share, graduate, status. Bundles are the unit of focused work — anything with a deliverable or a future audience.

This skill is invoked by the bundle awareness injection, by the load skill's "what are you working on?" prompt, or directly by the human via `/alive:bundle`.

---

## Detection — When to Invoke

The spectrum:

| | One-off | Bundle | Walnut |
|---|---------|--------|--------|
| Sessions | This session only | Likely >1, or worth returning to | Own ongoing lifecycle |
| Deliverable | No | Yes — ship, send, or reference later | Multiple deliverables |
| Audience | Just you, right now | Someone specific, or future-you | Has its own people |

**Trigger:** "Does this have a deliverable or a future audience?" If yes -> bundle.

---

## Bundle Types

### Outcome Bundle
The default. A body of work with a deliverable — a document, a plan, a shipped feature. Has drafts, versions, a finish line.

### Evergreen Bundle
Living reference material that grows over time. No "done" state — it accumulates. Research collections, style guides, knowledge bases. Status cycles between `active` and `maintaining` rather than progressing to `done`.

---

## Bundle Location

Bundles live flat in the walnut root. A folder is a bundle if it contains `context.manifest.yaml`.

```
nova-station/                   # walnut root
  _kernel/
  shielding-review/             # bundle
    context.manifest.yaml
    raw/
  launch-checklist/             # another bundle
    context.manifest.yaml
    raw/
```

No `bundles/` subdirectory. Every bundle sits directly in the walnut root alongside `_kernel/`.

---

## Operations

### Create

When no active bundle matches the current work:

1. Ask for the goal — one sentence. "What are you building?"
2. Determine bundle type — outcome (default) or evergreen
3. Derive the bundle name from the goal (kebab-case, descriptive)
4. Confirm:

```
╭─ 🐿️ new bundle
│
│  Name:    shielding-review
│  Type:    outcome
│  Walnut:  nova-station
│  Goal:    Evaluate radiation shielding vendors for habitat module
│  Path:    shielding-review/
│
│  ▸ Good?
│  1. Create
│  2. Change name
│  3. Make it evergreen instead
│  4. Cancel
╰─
```

5. Read `templates/bundle/context.manifest.yaml`
6. Fill placeholders: `{{name}}`, `{{goal}}`, `{{species}}`, `{{sensitivity}}`, `{{date}}`, `{{session_id}}`
7. Create `{walnut}/{name}/context.manifest.yaml`
8. Create `{walnut}/{name}/raw/` (empty directory)
9. Note: `project.py` will pick up the new bundle on next save — no manual now.json update needed
10. Stash: "Created bundle: {name}" (type: note)

Do NOT create `tasks.md` or `tasks.json` — tasks are created on demand via `tasks.py add`.
Do NOT create `observations.md` — removed in v3.

The first draft file is `{name}-draft-01.md` when the human starts writing.

---

### context.manifest.yaml

Every bundle has a `context.manifest.yaml` at its root. This is the bundle's identity and state tracker.

```yaml
name: shielding-review
goal: "Evaluate radiation shielding vendors for habitat module"
species: outcome           # outcome | evergreen
phase: draft               # draft | prototype | published | done | active | maintaining
sensitivity: normal        # normal | private | shared
version: "0.1"
created: 2026-03-28
session: abc123
parent_bundle:             # name of parent bundle, if nested

tags: []
people: []
shared: []
context_routes: []         # list of captured source file entries
discovered:                # mining extraction state
  status: none
  last_mined:
  processed: []
  unprocessed: []
```

**Key fields:**
- `species` — outcome or evergreen. Drives lifecycle behavior.
- `phase` — outcome bundles: draft -> prototype -> published -> done. Evergreen bundles: active <-> maintaining.
- `sensitivity` — controls sharing and export behavior:
  - `normal` — can be shared freely
  - `private` — excluded from walnut.world publishing, flagged on share attempts
  - `shared` — actively published or shared with specific people
- `parent_bundle` — if this bundle is nested inside another, records the parent's name
- `discovered` — populated by `alive:mine-for-context` with extraction tracking
- `sources` — paths to raw material or linked references from other bundles

---

### Share

When the human shares a bundle with someone (email, Slack, in person):

1. Check `sensitivity` — if `private`, warn before proceeding
2. Identify: who received it, how (method), which version file
3. Update the bundle's `context.manifest.yaml` `shared:` field:

```yaml
shared:
  - to: Sue Chen
    method: email
    date: 2026-03-15
    version: shielding-review-draft-02.md
```

4. Dispatch to the person's walnut at save (stash with destination tag `-> [[person-name]]`)
5. If bundle status is `draft` and it's been shared externally -> advance to `published`
6. Update sensitivity to `shared` if it was `normal`

```
╭─ 🐿️ bundle shared
│  shielding-review draft-02 -> Sue Chen via email
│  Status: draft -> published
╰─
```

#### Publishing to walnut.world

When the human wants to publish a bundle to walnut.world:

1. Check `sensitivity` — block if `private`
2. Confirm the human wants public sharing
3. Package the bundle content for walnut.world
4. Update `context.manifest.yaml` with publication record

```
╭─ 🐿️ bundle published to walnut.world
│  shielding-review v1 -> ben.walnut.world/shielding-review
│  Sensitivity: shared (public)
╰─
```

---

### Graduate

Graduation is a status flip, not a folder move. The bundle stays where it is.

**Outcome bundle graduation:**

1. Detect: scan `{walnut}/{name}/` for files matching `*-v1.md` or `*-v1.html`
2. Confirm with the human:

```
╭─ 🐿️ graduation ready
│  shielding-review has a v1. Mark as done?
│
│  ▸ Graduate?
│  1. Yes — mark done
│  2. Not yet
╰─
```

3. If confirmed:
   - Update `context.manifest.yaml` status to `done` (or `published` if shared)
   - The v1 output file stays inside the bundle folder
   - The bundle folder stays where it is in the walnut root
   - Update `_kernel/now.json` -> clear `bundle` if this was the active bundle
   - Log entry: "Bundle {name} graduated — status: done"

**Bundle -> walnut graduation** (when a bundle outgrows its container):

1. Confirm: "This bundle wants to be a walnut. Graduate it?"
2. Determine ALIVE domain and walnut name
3. Scaffold new walnut (invoke the create flow)
4. Seed `_kernel/key.md` from bundle context.manifest.yaml (goal, tags, people carry over)
5. Move bundle contents into new walnut root as the first bundle
6. Log entry in BOTH parent walnut ("Bundle {name} graduated to walnut") and new walnut ("Graduated from {parent}")
7. Add wikilink `[[new-walnut]]` to parent's `_kernel/key.md` `links:`

---

### Sub-Bundles

Bundles nest directly inside other bundle folders. No intermediate `bundles/` directory.

```
ecosystem-launch/                     # parent bundle
  context.manifest.yaml               # parent manifest (parent_bundle: blank)
  raw/
  website/                            # sub-bundle
    context.manifest.yaml             # parent_bundle: ecosystem-launch
  waitlist/                           # sub-bundle
    context.manifest.yaml             # parent_bundle: ecosystem-launch
```

Deeper nesting works the same way:

```
research/
  context.manifest.yaml
  market-analysis/
    context.manifest.yaml             # parent_bundle: research
    competitor-deep-dive/
      context.manifest.yaml           # parent_bundle: market-analysis
```

Sub-bundle rules:
- `parent_bundle:` in the manifest records the relationship
- Sub-bundles inherit `sensitivity` from parent unless overridden
- Sub-bundle status is independent of parent status
- Graduation is a status flip — sub-bundles stay nested where they are

---

### Tasks

Bundle tasks are operated exclusively through `tasks.py` CLI. Never read or write `tasks.json` directly.

- **Add a task:** `tasks.py add --walnut {path} --bundle {name} --title "Write vendor comparison"`
- **Complete a task:** `tasks.py done --walnut {path} --bundle {name} --id {task_id}`
- **List tasks:** `tasks.py list --walnut {path} --bundle {name}`

`tasks.json` is created automatically on the first `tasks.py add` call. Do not scaffold an empty tasks file during bundle creation.

---

### Status

Show the current state of bundles in the active walnut:

```
╭─ 🐿️ bundles in nova-station
│
│  Active: shielding-review (outcome, draft, draft-02)
│    Goal: Evaluate radiation shielding vendors
│    Last worked: session:a8c95e9, 2 days ago
│
│  Others:
│    launch-checklist — outcome, prototype, draft-03
│    safety-brief — outcome, done, shared with FAA (2026-03-10)
│    vendor-database — evergreen, active
│
│  ▸ Work on one?
│  1. shielding-review (continue)
│  2. launch-checklist
│  3. vendor-database
│  4. Start new bundle
╰─
```

Scan the walnut root for directories containing `context.manifest.yaml` to build this view. Show type, status, current version, goal, last session, and shares.

---

## Version File Naming

- Drafts: `{bundle-name}-draft-{nn}.md` (e.g., `shielding-review-draft-01.md`)
- Shipped: `{bundle-name}-v1.md`
- Visual versions: same pattern with `.html` extension

The bundle name is in every filename. The bundle folder is self-documenting.

---

## Integration Points

**Load** invokes this skill when prompting "what are you working on?" and the human picks "start something new."

**Save** checks bundle state in its integrity step — was a bundle worked on? Was one shared? This skill handles the actual context.manifest.yaml updates.

**Tidy** scans for `*-v1.md` in bundles still marked `draft` or `prototype` and surfaces graduation candidates (status flip, not folder move).

**Create** delegates bundle scaffolding to this skill rather than handling it inline.

**Awareness injection** triggers this skill when the squirrel detects bundle-worthy work mid-session.

**Mine** updates `context.manifest.yaml` `discovered:` field when processing raw sources within a bundle.
