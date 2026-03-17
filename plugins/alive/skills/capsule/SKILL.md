---
description: Use when creating, sharing, or graduating a capsule — or when the system detects work with a deliverable or future audience. Manages the full capsule lifecycle within a walnut.
user-invocable: true
---

# Capsule

Manage the capsule lifecycle: create, share, graduate, status. Capsules are the unit of focused work — anything with a deliverable or a future audience.

This skill is invoked by the capsule awareness injection, by the load skill's "what are you working on?" prompt, or directly by the human via `/alive:capsule`.

---

## Detection — When to Invoke

The spectrum:

| | One-off | Capsule | Walnut |
|---|---------|---------|--------|
| Sessions | This session only | Likely >1, or worth returning to | Own ongoing lifecycle |
| Deliverable | No | Yes — ship, send, or reference later | Multiple deliverables |
| Audience | Just you, right now | Someone specific, or future-you | Has its own people |

**Trigger:** "Does this have a deliverable or a future audience?" If yes → capsule.

---

## Operations

### Create

When no active capsule matches the current work:

1. Ask for the goal — one sentence. "What are you building?"
2. Derive the capsule name from the goal (kebab-case, descriptive)
3. Confirm:

```
╭─ 🐿️ new capsule
│
│  Name:    shielding-review
│  Walnut:  nova-station
│  Goal:    Evaluate radiation shielding vendors for habitat module
│  Path:    _core/_capsules/shielding-review/
│
│  ▸ Good?
│  1. Create
│  2. Change name
│  3. Cancel
╰─
```

4. Read `templates/capsule/companion.md`
5. Fill placeholders: `{{goal}}`, `{{date}}`, `{{session_id}}`
6. Create `_core/_capsules/{name}/companion.md`
7. Create `_core/_capsules/{name}/raw/` (empty directory)
8. Update `_core/now.md` → set `capsule: {name}`
9. Stash: "Created capsule: {name}" (type: note)

The first draft file is `{name}-draft-01.md` when the human starts writing.

---

### Share

When the human shares a capsule with someone (email, Slack, in person):

1. Identify: who received it, how (method), which version file
2. Update the capsule companion's `shared:` frontmatter:

```yaml
shared:
  - to: Sue Chen
    method: email
    date: 2026-03-15
    version: shielding-review-draft-02.md
```

3. Dispatch to the person's walnut at save (stash with destination tag `→ [[person-name]]`)
4. If capsule status is `draft` and it's been shared externally → advance to `published`

```
╭─ 🐿️ capsule shared
│  shielding-review draft-02 → Sue Chen via email
│  Status: draft → published
╰─
```

---

### Graduate

When a capsule has a `*-v1.md` file or the human explicitly requests graduation:

**Work capsule → walnut root:**

1. Detect: scan `_core/_capsules/{name}/` for files matching `*-v1.md` or `*-v1.html`
2. Confirm with the human:

```
╭─ 🐿️ graduation ready
│  shielding-review has a v1. Graduate to walnut root?
│
│  ▸ Graduate?
│  1. Yes — move to walnut root
│  2. Not yet
╰─
```

3. If confirmed:
   - Move the entire `_core/_capsules/{name}/` folder to walnut root `{name}/`
   - Update companion status to `done`
   - Update `_core/now.md` → clear `capsule:` if this was the active capsule
   - Log entry: "Capsule {name} graduated to walnut root"

**Capsule → walnut graduation** (when a capsule outgrows its container):

1. Confirm: "This capsule wants to be a walnut. Graduate it?"
2. Determine ALIVE domain and walnut name
3. Scaffold new walnut (invoke the create flow — Step 6)
4. Seed `_core/key.md` from capsule companion (goal, tags, people carry over)
5. Move capsule contents into new walnut's `_core/_capsules/` as the first capsule
6. Log entry in BOTH parent walnut ("Capsule {name} graduated to walnut") and new walnut ("Graduated from {parent}")
7. Add wikilink `[[new-walnut]]` to parent's `_core/key.md` `links:`

---

### Status

Show the current state of capsules in the active walnut:

```
╭─ 🐿️ capsules in nova-station
│
│  Active: shielding-review (draft, draft-02)
│    Goal: Evaluate radiation shielding vendors
│    Last worked: session:a8c95e9, 2 days ago
│
│  Others:
│    launch-checklist — prototype, draft-03
│    safety-brief — done, shared with FAA (2026-03-10)
│
│  ▸ Work on one?
│  1. shielding-review (continue)
│  2. launch-checklist
│  3. Start new capsule
╰─
```

Read all capsule companion frontmatter in `_core/_capsules/` to build this view. Show status, current version, goal, last session, and shares.

---

## Version File Naming

- Drafts: `{capsule-name}-draft-{nn}.md` (e.g., `shielding-review-draft-01.md`)
- Shipped: `{capsule-name}-v1.md`
- Visual versions: same pattern with `.html` extension

The capsule name is in every filename. When graduated to walnut root, the folder is self-documenting.

---

## Integration Points

**Load** invokes this skill when prompting "what are you working on?" and the human picks "start something new."

**Save** checks capsule state in its integrity step — was a capsule worked on? Was one shared? This skill handles the actual companion updates.

**Tidy** scans for `*-v1.md` still in `_core/_capsules/` and surfaces graduation candidates.

**Create** delegates capsule scaffolding to this skill rather than handling it inline.

**Awareness injection** triggers this skill when the squirrel detects capsule-worthy work mid-session.
